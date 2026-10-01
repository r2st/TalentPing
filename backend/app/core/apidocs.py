"""What the API answers, derived from the API rather than described beside it.

FastAPI already publishes a schema for every request and response body, and
every route in this app carries a tag and a summary. What the generated
document said nothing about was failure: across 170 operations it declared
``200``, ``201``, ``204`` and the validation ``422``, and not one ``401``,
``403``, ``404``, ``409`` or ``429``. A client integrating against it learned
the shape of success and had to discover the rest by causing it.

Writing those in by hand is the obvious fix and the wrong one. There are 170
operations and roughly 130 deliberate failures among them; a hand-kept list is
a second copy of the routing table, and the copy is what goes stale. So this
module derives the failure half from the same two sources the behaviour comes
from:

**The dependency graph**, for the failures that are a property of *how a route
is mounted*. A route depending on ``get_current_user`` answers 401 to a missing
or revoked token — always, mechanically, with no way for the handler to opt out.
``get_current_admin`` adds 403. A route carrying a limiter answers 429, and
:func:`app.core.rate_limit.limit_of` knows the actual numbers, so the published
description carries them instead of restating a constant somebody has to
remember to update.

**The syntax tree**, for the failures the handler chooses. ``raise
HTTPException(status_code=status.HTTP_409_CONFLICT, detail="...")`` is a fact
about the handler that can be read without running it, and following the plain
function calls the handler makes catches the ones factored into helpers
(``_application_or_404``, ``require_supported_upload``) that every route in a
router shares.

**Where this is deliberately incomplete.** Static resolution follows direct
calls to functions this process can find the source of, to a bounded depth. A
failure raised behind a dynamic dispatch, inside a service class, or from a
library is not found, so the catalogue under-reports rather than inventing.
That is the safe direction — a documented code is always really raised — but it
is a direction, so :mod:`tests.test_api_catalog` pins the total per status
against the raise sites the routers actually contain, and a new one that lands
somewhere unreachable fails the suite rather than going quietly missing.
"""
from __future__ import annotations

import ast
import inspect
import logging
import sys
import textwrap
from dataclasses import dataclass
from functools import cache
from typing import Any

from fastapi import FastAPI, status
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.security.base import SecurityBase

from app.core.errors import GENERIC_DETAIL
from app.core.rate_limit import limit_of

logger = logging.getLogger(__name__)

# The body every error in this app returns. FastAPI renders `HTTPException` as
# `{"detail": ...}` and `ServerErrorEnvelopeMiddleware` matches that shape for
# the 500, adding the request id — so one schema covers every failure a client
# can receive, and a handler written for a 404 works unchanged on a 500.
ERROR_SCHEMA_NAME = "ErrorResponse"

ERROR_SCHEMA: dict[str, Any] = {
    "title": ERROR_SCHEMA_NAME,
    "type": "object",
    "required": ["detail"],
    "properties": {
        "detail": {
            "title": "Detail",
            "type": "string",
            "description": (
                "Human-readable explanation of the failure. Safe to show a "
                "user for 4xx codes; for 500 it is always the constant "
                f'"{GENERIC_DETAIL}", because an exception message is written '
                "for a developer and routinely names a query or a row."
            ),
        },
        "request_id": {
            "title": "Request Id",
            "type": "string",
            "description": (
                "Present on 500. The same value as the X-Request-ID response "
                "header and the id bound to the server-side log record — quote "
                "it in a bug report and the traceback can be found."
            ),
        },
    },
}

_ERROR_REF = {"$ref": f"#/components/schemas/{ERROR_SCHEMA_NAME}"}


def _error_response(description: str, headers: dict | None = None) -> dict[str, Any]:
    body = {
        "description": description,
        "content": {"application/json": {"schema": _ERROR_REF}},
    }
    if headers:
        body["headers"] = headers
    return body


# Headers a 429 carries, named so a client can render a countdown rather than
# parsing the sentence in `detail`. See `rate_limit._refuse`.
#
# The three `RateLimit-*` headers are on *served* responses from a metered route
# too, which is the point of publishing them: a client that only learns its
# balance from the 429 has to trip the limit to find it, and on the credential
# scopes tripping it is what gets an address refused for the rest of the window.
# Only `Retry-After` is exclusive to the refusal.
_RATE_LIMIT_HEADERS = {
    "Retry-After": {
        "description": "Whole seconds until the oldest hit leaves the window. "
        "Rounded up: a retry computed from a rounded-down value is still "
        "inside the window and is refused again. Sent on the refusal only.",
        "schema": {"type": "integer"},
    },
    "RateLimit-Limit": {
        "description": "The budget, in requests per window. Also sent on every "
        "served response from this route.",
        "schema": {"type": "integer"},
    },
    "RateLimit-Remaining": {
        "description": "Requests left in the current window. Always 0 on a "
        "refusal; on a served response it is the balance after that request, so "
        "a client can pace itself without ever reaching this status.",
        "schema": {"type": "integer"},
    },
    "RateLimit-Reset": {
        "description": "Seconds until the budget next frees up — the oldest "
        "live hit leaving the window. Same value as Retry-After on a refusal, "
        "and sent on served responses too.",
        "schema": {"type": "integer"},
    },
}

_WWW_AUTH_HEADER = {
    "WWW-Authenticate": {
        "description": 'Always "Bearer". Present on the credential failures, '
        "absent on a revoked-session 401 raised after the token parsed.",
        "schema": {"type": "string"},
    }
}


# ---------------------------------------------------------------------------
# Tag groups
# ---------------------------------------------------------------------------

# Ordered as a reader should meet them: authenticate, describe yourself, then
# the two halves of the product (find work, handle the mail it produces), then
# the reporting surfaces, then operations. `misc` and `tracking` sit last
# because neither is part of the product's own API — one is liveness, the other
# is what a recipient's mail client hits.
TAG_DESCRIPTIONS: list[tuple[str, str]] = [
    (
        "auth",
        "Registration, login, the bearer token everything else needs, and "
        "account closure. `POST /auth/login` and `POST /auth/register` are the "
        "only endpoints rate-limited by client address rather than by user — "
        "they are the ones with no user to key on.",
    ),
    (
        "profiles",
        "The candidate profile the whole product reasons from: roles wanted, "
        "locations, salary floor, skills. Fit scoring, tailoring and every "
        "draft read from here.",
    ),
    (
        "resumes",
        "Resume upload, parsing and per-application tailoring. Uploads are "
        "size- and type-checked before parsing, so 413 and 415 are ordinary "
        "answers here and nowhere else.",
    ),
    (
        "jobs",
        "Job discovery and the saved searches that drive it, plus per-posting "
        "intelligence. The endpoints that run a search or research a posting "
        "call a model or crawl a site on the request thread, and are metered "
        "accordingly.",
    ),
    (
        "smart-apply",
        "Fit scoring and application-material generation for one posting. "
        "Deterministic scoring first, model-written prose second, and never "
        "the other way round.",
    ),
    (
        "form-apply",
        "Browser-driven application submission. A run is dispatched, not "
        "performed inline; the artifacts it captures are fetched back through "
        "these routes and expire, which is what 410 means here.",
    ),
    (
        "linkedin",
        "LinkedIn Easy Apply, sharing the browser-run budget with `form-apply` "
        "because it is the same machinery behind a different door.",
    ),
    (
        "campaigns",
        "Outreach campaigns: create, launch, pause, resume, and read their "
        "results. The state machine refuses illegal transitions with 409 — see "
        "docs/api/ERRORS.md for which transitions exist.",
    ),
    (
        "recruiters",
        "Recruiter contacts and the discovery that finds them.",
    ),
    (
        "recruiter-inbox",
        "Inbound recruiter mail: scanning, classification, and reply drafting. "
        "One of the two pipelines that write reply drafts.",
    ),
    (
        "inbox",
        "Conversation threads with recruiters, and the replies sent into them. "
        "The other draft-writing pipeline.",
    ),
    (
        "review",
        "The queue of drafts waiting on a human before they send.",
    ),
    (
        "follow-ups",
        "Scheduled follow-up steps and the suggestions behind them. A follow-up "
        "stops when a reply arrives.",
    ),
    (
        "tracker",
        "Application tracking — the record of what was sent where, and what "
        "came back.",
    ),
    (
        "board",
        "The kanban view over tracked applications: stages, ordering and "
        "movement between them.",
    ),
    (
        "autopilot",
        "Unattended operation: preferences, the reputation gate that stands in "
        "front of every send, and the manual run trigger.",
    ),
    (
        "interview-prep",
        "Model-generated interview preparation for a specific application.",
    ),
    (
        "digest",
        "The weekly digest: preview it, or send it now.",
    ),
    (
        "notifications",
        "In-app notifications and their read state.",
    ),
    (
        "dashboard",
        "Aggregate views over everything above, plus the pipeline exports.",
    ),
    (
        "analytics",
        "Effectiveness reporting: subject-line tests, stage velocity, reply "
        "rates, feature usage.",
    ),
    (
        "gmail",
        "Per-user Gmail connection. The OAuth authorization-code flow lives "
        "here, including the callback Google redirects to.",
    ),
    (
        "admin",
        "Platform administration. Every route requires an administrator and "
        "answers 403 to a signed-in user who is not one.",
    ),
    (
        "misc",
        "Liveness and readiness. `/health` reports degraded dependencies with "
        "a 200 on purpose — see docs/api/API-REFERENCE.md.",
    ),
    (
        "tracking",
        "Public endpoints hit by a recipient's mail client or browser: the "
        "open pixel, click redirects and one-click unsubscribe. No "
        "authentication, by necessity.",
    ),
]


# ---------------------------------------------------------------------------
# Static extraction of deliberate failures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RaisedError:
    """One ``raise HTTPException(...)`` a route can reach."""

    status_code: int
    detail: str
    source: str
    """``module:lineno`` of the raise site, so the catalogue is checkable."""


# `status.HTTP_404_NOT_FOUND` -> 404, for every name fastapi.status exports.
_STATUS_NAMES = {
    name: value
    for name, value in vars(status).items()
    if name.startswith("HTTP_") and isinstance(value, int)
}

_MAX_DEPTH = 3

# FastAPI's `OAuth2PasswordBearer` refuses a request with no `Authorization`
# header itself, before `get_current_user` is ever called — so this is the 401
# a client meets most often, and the one no amount of walking *our* source can
# find. Named here rather than derived, and pinned by
# `test_a_real_401_matches_what_the_catalogue_promises`, which provokes the
# real refusal: if a FastAPI upgrade rewords it, that test fails rather than
# the catalogue quietly disagreeing with the server.
_UNAUTHENTICATED_DETAIL = "Not authenticated"


def _status_value(node: ast.expr | None) -> int | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute):
        return _STATUS_NAMES.get(node.attr)
    if isinstance(node, ast.Name):
        return _STATUS_NAMES.get(node.id)
    return None


def _detail_text(node: ast.expr | None) -> str:
    """Render a ``detail=`` argument as documentation prose.

    An f-string becomes its template with the interpolations named — the shape
    of the message is the documentable part, and the values in it belong to one
    request.
    """
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                out.append("{" + ast.unparse(part.value) + "}")
        return "".join(out)
    if isinstance(node, (ast.BinOp, ast.Call)):
        return f"(varies — {ast.unparse(node)})"
    return ast.unparse(node)


@cache
def _module_index(module_name: str) -> dict[str, ast.AST]:
    """Module-level functions and ``HTTPException`` constants, by name.

    Both matter. The functions are how a router factors "load it or 404" out of
    twenty handlers; the constants are how ``deps`` declares its two 401s once
    and raises the same object from three places.
    """
    module = sys.modules.get(module_name)
    if module is None:
        return {}
    try:
        tree = ast.parse(inspect.getsource(module))
    except (OSError, TypeError, SyntaxError):
        return {}
    index: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            index[node.name] = node
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            called = node.value.func
            if getattr(called, "id", None) == "HTTPException":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        index[target.id] = node.value
    return index


def _from_call(call: ast.Call, module_name: str, lineno: int) -> list[RaisedError]:
    """One ``HTTPException(...)`` construction, read as a documented failure."""
    code = _status_value(
        next(
            (kw.value for kw in call.keywords if kw.arg == "status_code"),
            call.args[0] if call.args else None,
        )
    )
    if code is None:
        return []
    # `detail` is usually a keyword, and occasionally positional —
    # `HTTPException(status.HTTP_409_CONFLICT, "PDF rendering is not
    # available…")`. Reading only keywords published that one as "(no detail)"
    # in the catalogue while the server sent a perfectly good sentence.
    detail = _detail_text(
        next(
            (kw.value for kw in call.keywords if kw.arg == "detail"),
            call.args[1] if len(call.args) > 1 else None,
        )
    )
    return [RaisedError(code, detail, f"{module_name}:{lineno}")]


def _raises_in(
    node: ast.AST, module_name: str, depth: int, seen: set[tuple[str, str]]
) -> list[RaisedError]:
    """Every ``HTTPException`` reachable from *node*, following plain calls."""
    module = sys.modules.get(module_name)
    found: list[RaisedError] = []
    index = _module_index(module_name)

    for child in ast.walk(node):
        # `raise _credentials_exc` — the exception is built once at module
        # scope and raised by name from several places. `deps` declares both of
        # its 401s that way, so a walker that only recognised a call found the
        # code on 157 operations and the message on none of them.
        if isinstance(child, ast.Raise) and isinstance(child.exc, ast.Name):
            constant = index.get(child.exc.id)
            if isinstance(constant, ast.Call):
                found.extend(
                    _from_call(constant, module_name, getattr(child, "lineno", 0))
                )
            continue

        if isinstance(child, ast.Call) and getattr(child.func, "id", "") == (
            "HTTPException"
        ):
            found.extend(
                _from_call(child, module_name, getattr(child, "lineno", 0))
            )
            continue

        if depth >= _MAX_DEPTH or not isinstance(child, ast.Call):
            continue

        # A plain `helper(...)` or `module.helper(...)` call. Resolve the name
        # against the calling module's own globals, which is how an import
        # from another `app.` module is followed.
        name = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
        if not name or name.startswith(("self", "db")):
            continue
        target_module, target_node = _resolve(name, module, module_name, index)
        if target_node is None:
            continue
        key = (target_module, name)
        if key in seen:
            continue
        found.extend(
            _raises_in(target_node, target_module, depth + 1, seen | {key})
        )

    return found


def _resolve(
    name: str, module: Any, module_name: str, index: dict[str, ast.AST]
) -> tuple[str, ast.AST | None]:
    """Find *name*'s definition, in this module or the ``app.`` one it came from."""
    if name in index:
        return module_name, index[name]
    obj = getattr(module, name, None) if module is not None else None
    origin = getattr(obj, "__module__", None)
    if not origin or not origin.startswith("app."):
        return module_name, None
    return origin, _module_index(origin).get(getattr(obj, "__name__", name))


@cache
def _route_errors(endpoint: Any) -> tuple[RaisedError, ...]:
    module_name = getattr(endpoint, "__module__", "")
    module = sys.modules.get(module_name)
    if module is None:
        return ()
    try:
        source, first_line = inspect.getsourcelines(endpoint)
        tree = ast.parse(textwrap.dedent("".join(source)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return ()
    # `getsourcelines` returns the function's own text, so the tree's line
    # numbers start at 1 and every raise site reads as being near the top of
    # its module. Shifted back onto the file's own numbering, because a
    # `module:lineno` that does not point at the raise is worse than no
    # citation — it invites a reader to check it and find something else.
    ast.increment_lineno(tree, first_line - 1)
    name = getattr(endpoint, "__name__", "?")
    return tuple(_raises_in(tree, module_name, 0, {(module_name, name)}))


# ---------------------------------------------------------------------------
# Route facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteDoc:
    """One operation, with everything the reference needs to say about it."""

    method: str
    path: str
    name: str
    tag: str
    summary: str
    description: str
    authenticated: bool
    admin_only: bool
    rate_limit: dict[str, Any] | None
    success_codes: tuple[int, ...]
    errors: tuple[RaisedError, ...]

    @property
    def status_codes(self) -> tuple[int, ...]:
        """Every documented code, success and failure, ascending."""
        codes = set(self.success_codes)
        codes.update(e.status_code for e in self.errors)
        codes.update(implied_codes(self))
        return tuple(sorted(codes))


def implied_codes(route: RouteDoc) -> set[int]:
    """Codes that follow from how the route is mounted, not from its body."""
    codes = {500}
    if route.authenticated:
        codes.add(401)
    if route.admin_only:
        codes.add(403)
    if route.rate_limit:
        codes.add(429)
    return codes


def _dependency_calls(dependant: Any) -> list[Any]:
    """Every dependency callable reachable from *dependant*."""
    calls: list[Any] = []
    stack = [dependant]
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if call is not None:
            calls.append(call)
        stack.extend(getattr(dep, "dependencies", []))
    return calls


def _dependency_names(dependant: Any) -> set[str]:
    """Every dependency function reachable from *dependant*, by name."""
    return {getattr(call, "__name__", "") for call in _dependency_calls(dependant)}


def _limiter(dependant: Any) -> dict[str, Any] | None:
    stack = [dependant]
    while stack:
        dep = stack.pop()
        found = limit_of(getattr(dep, "call", None))
        if found is not None:
            return found
        stack.extend(getattr(dep, "dependencies", []))
    return None


def route_docs(app: FastAPI) -> list[RouteDoc]:
    """Every operation the app serves, one entry per method/path pair.

    Walked through ``iter_route_contexts`` rather than ``app.routes`` directly:
    since FastAPI 0.139 an ``include_router`` call leaves one ``_IncludedRouter``
    object in ``app.routes`` instead of splicing its routes in, so the obvious
    ``isinstance(route, APIRoute)`` filter over ``app.routes`` finds a single
    route out of 170 — and finds it without the prefix, because the prefix
    lives on the include rather than on the route. The context objects carry
    the effective path and the effective dependant, which is what makes "is
    this route authenticated?" answerable at all: the auth dependency is
    usually declared on the router, not on the endpoint.

    Sorted by tag then path so the generated reference groups by resource,
    which is how somebody integrating reads it — and stable, so a regenerated
    document diffs against the last one instead of reshuffling.
    """
    schema_ops = _operations(app)
    docs: list[RouteDoc] = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        path = context.path or route.path
        deps = _dependency_names(context.dependant)
        authenticated = bool(deps & {"get_current_user", "get_current_admin"})
        for method in sorted((context.methods or set()) - {"HEAD", "OPTIONS"}):
            operation = schema_ops.get((path, method.lower()), {})
            tags = operation.get("tags") or ["misc"]
            docs.append(
                RouteDoc(
                    method=method,
                    path=path,
                    name=context.name or route.name,
                    tag=str(tags[0]),
                    # Read back off the generated document rather than off the
                    # route: FastAPI derives a summary from the function name
                    # when none is given, so `route.summary` is None for every
                    # endpoint in this app while the published one is not.
                    summary=operation.get("summary", ""),
                    description=(operation.get("description") or "").strip(),
                    authenticated=authenticated,
                    admin_only="get_current_admin" in deps,
                    rate_limit=_limiter(context.dependant),
                    success_codes=_success_codes(operation, route),
                    errors=_errors_for(route.endpoint, context.dependant),
                )
            )
    docs.sort(key=lambda d: (d.tag, d.path, d.method))
    return docs


def _errors_for(endpoint: Any, dependant: Any) -> tuple[RaisedError, ...]:
    """Failures the handler chooses, plus the ones its dependencies choose.

    A dependency's refusals are the route's refusals — the caller cannot tell
    the difference and neither should the reference. It matters most for the
    two 401s: ``get_current_user`` raises "Could not validate credentials" and
    "Session has been revoked; sign in again" from module-level constants, and
    those are the exact strings a client has to branch on, since only the
    second one means "clear the stored token and show the login screen". Read
    from the handler bodies alone, a 401 was published on 157 operations whose
    message appeared nowhere in the document.

    Deduplicated by ``(code, detail, source)``: a limiter is reached twice —
    once through the decorator, which ``inspect.getsource`` includes in the
    handler's own source, and once as a dependency — and both are the same
    raise site in ``rate_limit._refuse``.
    """
    found = list(_route_errors(endpoint))
    for call in _dependency_calls(dependant):
        if call is endpoint:
            continue
        if isinstance(call, SecurityBase):
            # A security scheme is an instance, not a function; there is no
            # source to walk. Ask it what it does instead.
            if getattr(call, "auto_error", False):
                found.append(
                    RaisedError(
                        401,
                        _UNAUTHENTICATED_DETAIL,
                        f"{type(call).__module__}:{type(call).__name__}",
                    )
                )
            continue
        found.extend(_route_errors(call))
    seen: set[tuple[int, str, str]] = set()
    unique: list[RaisedError] = []
    for err in found:
        key = (err.status_code, err.detail, err.source)
        if key in seen:
            continue
        seen.add(key)
        unique.append(err)
    return tuple(unique)


def _success_codes(operation: dict[str, Any], route: APIRoute) -> tuple[int, ...]:
    codes = sorted(
        int(code)
        for code in operation.get("responses", {})
        if code.isdigit() and int(code) < 400
    )
    return tuple(codes) or (route.status_code or 200,)


def _operations(app: FastAPI) -> dict[tuple[str, str], dict[str, Any]]:
    """``(path, method)`` -> the operation object FastAPI generated for it."""
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    return {
        (path, method): operation
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if isinstance(operation, dict)
    }


# ---------------------------------------------------------------------------
# Spec enrichment
# ---------------------------------------------------------------------------

# What each status means in this API, in the words a client integrator needs.
# The per-route `detail` strings sharpen these; this is the fallback and the
# entry the error catalogue is built from.
STATUS_GUIDE: dict[int, tuple[str, str]] = {
    400: (
        "Bad Request",
        "The request was understood and refused on its contents — a value that "
        "parsed but cannot be acted on. Not retryable unchanged.",
    ),
    401: (
        "Unauthorized",
        "No bearer token, an expired or malformed one, or a session revoked "
        "since the token was issued (changing a password revokes every "
        "outstanding token). Obtain a new token via POST /auth/login.",
    ),
    403: (
        "Forbidden",
        "Authenticated, and not permitted. Either the account is inactive or "
        "the route requires an administrator.",
    ),
    404: (
        "Not Found",
        "No such resource, or none belonging to the authenticated user — the "
        "two are deliberately indistinguishable, so an id belonging to someone "
        "else cannot be confirmed to exist.",
    ),
    409: (
        "Conflict",
        "The resource exists and is in a state that refuses this operation: an "
        "illegal state-machine transition, a duplicate, or a concurrent writer "
        "that got there first. Re-read the resource before retrying.",
    ),
    410: (
        "Gone",
        "The resource existed and was deliberately expired — a run artifact "
        "past its retention window. Retrying never succeeds.",
    ),
    413: (
        "Content Too Large",
        "The upload exceeds the configured size ceiling (MAX_UPLOAD_MB).",
    ),
    415: (
        "Unsupported Media Type",
        "The upload's content type is not one this endpoint parses.",
    ),
    422: (
        "Unprocessable Entity",
        "Request validation failed. The body names the offending field and "
        "location. Also raised by hand where a value is well-formed but "
        "semantically impossible.",
    ),
    429: (
        "Too Many Requests",
        "A rate limit refused the request. Retry-After is the earliest second "
        "at which a retry can succeed.",
    ),
    500: (
        "Internal Server Error",
        "An unhandled exception. The body carries request_id, which is also in "
        "the X-Request-ID header and on the server-side log record.",
    ),
    502: (
        "Bad Gateway",
        "An upstream this route depends on failed or answered unusably.",
    ),
    503: (
        "Service Unavailable",
        "A dependency this route requires is not configured or not reachable — "
        "no model provider, or no connected mailbox.",
    ),
}


def _describe(code: int, details: list[str]) -> str:
    title, guide = STATUS_GUIDE.get(code, ("Error", ""))
    text = f"{title}. {guide}" if guide else title
    unique = sorted({d for d in details if d})
    if unique:
        rendered = "; ".join(f"`{d}`" for d in unique)
        text = f"{text}\n\nRaised as: {rendered}"
    return text


def error_responses(route: RouteDoc) -> dict[str, dict[str, Any]]:
    """The failure half of one operation's ``responses`` object."""
    by_code: dict[int, list[str]] = {}
    for err in route.errors:
        by_code.setdefault(err.status_code, []).append(err.detail)
    for code in implied_codes(route):
        by_code.setdefault(code, [])

    out: dict[str, dict[str, Any]] = {}
    for code in sorted(by_code):
        if code < 400:
            # A `status.HTTP_204_NO_CONTENT` mentioned in a handler body is a
            # success constant, not a failure. It is already in `responses`.
            continue
        headers: dict[str, Any] | None = None
        if code == 429:
            headers = _RATE_LIMIT_HEADERS
        elif code == 401:
            headers = _WWW_AUTH_HEADER
        description = _describe(code, by_code[code])
        if code == 429 and route.rate_limit:
            limit = route.rate_limit
            description += (
                f"\n\nBudget: {limit['max_requests']} requests per "
                f"{limit['window_seconds']}s, keyed by {limit['keyed_by']}, "
                f"scope `{limit['scope']}`."
            )
        out[str(code)] = _error_response(description, headers)
    return out


def enrich(app: FastAPI) -> dict[str, Any]:
    """Build the OpenAPI document with the failure half filled in.

    Idempotent and cached by FastAPI's own ``app.openapi_schema``, so this runs
    once per process no matter how often ``/openapi.json`` is fetched.
    """
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {}).setdefault("schemas", {})[
        ERROR_SCHEMA_NAME
    ] = ERROR_SCHEMA

    known = {tag for tag, _ in TAG_DESCRIPTIONS}
    schema["tags"] = [
        {"name": tag, "description": text} for tag, text in TAG_DESCRIPTIONS
    ]

    for route in route_docs(app):
        operation = schema["paths"].get(route.path, {}).get(route.method.lower())
        if operation is None:
            continue
        if route.tag not in known:
            # A new router landed without a group description. Surfaced rather
            # than silently producing a reference with an unexplained section.
            logger.warning("no tag description for %s", route.tag)
        operation.setdefault("responses", {}).update(error_responses(route))
        if route.rate_limit:
            operation["x-rate-limit"] = route.rate_limit
        operation["x-authenticated"] = route.authenticated

    app.openapi_schema = schema
    return schema
