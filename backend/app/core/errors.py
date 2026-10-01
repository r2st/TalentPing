"""The last line of the API: an unhandled exception, rendered as JSON.

Every deliberate failure in this app already answers in JSON — FastAPI renders
``HTTPException`` as ``{"detail": ...}`` and a validation error as a 422 with a
body. The one response that did not was the one nobody writes on purpose. An
exception escaping a handler reached Starlette's ``ServerErrorMiddleware``, the
outermost thing in the stack, and became ``text/plain`` with the body
``Internal Server Error``. Three things follow from that, and they compound:

* **The client cannot parse it.** A frontend that does ``await resp.json()`` on
  a failed call — which is every error path it has, because every other error
  *is* JSON — raises a parse error instead of reporting the 500. The real
  failure is replaced by a second, misleading one at the point of reporting.
* **The request id is not on it.** ``RequestContextMiddleware`` attaches the id
  from inside its own ``send``; ``ServerErrorMiddleware`` sits outside it and
  answers over its head, so the header never gets added. The id exists, it is
  on the log line, and the one person who needs it — the user who just saw the
  error and is about to describe it — is the one person who never receives it.
* **The browser cannot read it either.** ``ServerErrorMiddleware`` is outside
  ``CORSMiddleware`` too, so the 500 carries no ``Access-Control-Allow-Origin``.
  Cross-origin — which is the deployment, and localhost:5173 in development —
  the browser refuses the whole response and the SPA sees a bare network error.
  Not "the server failed", which is actionable: "fetch failed", which is not.

So this middleware catches the exception *below* CORS and above the router,
which is the only place where all three are fixable at once. CORS decorates the
response on the way out, ``RequestContextMiddleware`` stamps the id on it, and
the body is the same ``{"detail": ...}`` shape as every other error the client
handles — plus the request id, in the body as well as the header, so it can be
shown on screen without reading headers at all.

**What the client is told is deliberately nothing.** The detail is the constant
string, never ``str(exc)``: an exception message is written for a developer and
routinely contains a query, a path, or a row someone else owns. The traceback
goes to the log, at ERROR, with the request id already bound — which is what
makes the id in the response worth handing over. That also means a
``debug=True`` app cannot leak a traceback through this path even by accident;
the setting is still refused outside development by ``Settings``, and this is
the second lock on the same door.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable

from app.core.logging import request_id_var

logger = logging.getLogger("app.error")

# The body a client gets. Matches FastAPI's own `{"detail": ...}` so an error
# handler written for a 404 works unchanged on a 500.
GENERIC_DETAIL = "Internal Server Error"


class ServerErrorEnvelopeMiddleware:
    """Convert an unhandled exception into a JSON 500 carrying the request id.

    Raw ASGI, for the same reason ``RequestContextMiddleware`` is: it has to
    know whether the response has already started, and ``BaseHTTPMiddleware``
    puts the downstream app in another task where that is harder to be sure of.

    Once the first byte is out the door there is nothing to be done — the status
    line is already sent and the client is reading a body. In that case the
    exception is logged and re-raised, which lets the server tear the connection
    down rather than append an error object to a half-written success.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            request_id = request_id_var.get()
            logger.exception(
                "unhandled exception",
                extra={
                    "http_method": scope.get("method", ""),
                    # Path only. The query string is where the Gmail OAuth
                    # callback carries a live grant, same as in the access log.
                    "http_path": scope.get("path", ""),
                },
            )
            if response_started:
                # Too late to say anything the client will believe. Let it
                # propagate so the connection fails loudly instead of quietly
                # delivering a truncated body that parses.
                raise
            await _send_error(send, request_id)


async def _send_error(send: Callable, request_id: str | None) -> None:
    body: dict[str, object] = {"detail": GENERIC_DETAIL}
    if request_id:
        # In the body as well as the header: a user reporting this is reading a
        # screen, not devtools, so the id has to be somewhere the UI can render.
        body["request_id"] = request_id
    payload = json.dumps(body).encode()

    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
