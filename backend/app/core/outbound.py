"""Where an outbound fetch is allowed to land.

Two endpoints hand a caller's own string to ``requests.get`` and give the
response body back: Smart Apply's ``job_url`` (``jd_parser.fetch_job_text``,
whose text is parsed, stored on the tailoring run and rendered) and the career
scraper, which follows anchors it finds on somebody else's page. Both were
unrestricted, which makes the API a proxy into its own network::

    POST /api/v1/fit-score  {"job_url": "http://169.254.169.254/metadata/v1"}
    POST /api/v1/tailor     {"job_url": "http://127.0.0.1:6379/"}
    POST /api/v1/tailor     {"job_url": "http://127.0.0.1:8000/api/v1/..."}

The deployment is a single VPS running the API, Postgres, Redis and Caddy on
the loopback interface (see the deployment notes), so "the network the server
can reach" is precisely the set of things nothing else can. The first URL is
the cloud metadata endpoint, the second is an unauthenticated Redis, and the
third is this application talking to itself from inside its own trust boundary.

Three rules, and the third is the one that is usually missed:

* **Scheme allowlist.** ``http``/``https`` only. ``file://``, ``gopher://`` and
  friends never reach a socket.
* **The resolved address must be public.** Not the hostname — the *addresses*
  it resolves to, all of them. ``localtest.me`` and a thousand other public
  names resolve to 127.0.0.1, so a hostname denylist catches nothing.
* **Every redirect hop is checked, not just the first.** A public URL that
  answers ``302 Location: http://169.254.169.254/`` defeats a check that only
  looks at what the caller typed, and ``allow_redirects=True`` follows it
  without ever showing the caller's code the new address. So redirects are
  followed here, one hop at a time, with the guard applied to each.

A fourth rule is about the response rather than the destination: **no body is
read without a ceiling, on bytes and on seconds alike**. Passing the address
check says where the bytes come from and nothing about how many there are or
how long they take, and ``timeout`` bounds neither — it limits how long one
read may stall, so a server trickling a chunk at a time resets it forever. See
:data:`MAX_RESPONSE_BYTES` and :data:`MAX_BODY_SECONDS`; a body can be innocent
by one and abusive by the other.

What this does **not** claim: it is not proof against DNS rebinding. The name is
resolved for the check and resolved again by the socket, and a record with a
one-second TTL can differ between the two. Closing that needs the connection
pinned to the address that was validated, which means reaching under
``requests``' transport adapter. The exposure left is a narrow race rather than
"paste a URL, read the metadata service", and it is recorded here rather than
papered over.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

#: Schemes that may reach a socket. Everything else is refused before DNS.
ALLOWED_SCHEMES = ("http", "https")

#: How many ``3xx`` hops to follow before giving up. ``requests``' own default.
MAX_REDIRECTS = 10

#: The most of any one response body that is read into memory.
#:
#: ``timeout`` bounds how long a *socket read* may stall, not how much a server
#: may send: a response that keeps trickling bytes resets the clock on every
#: chunk and never times out. So a caller who hands us a URL — Smart Apply's
#: ``job_url`` is a request field, and the career scraper follows anchors it
#: found on somebody else's page — could name an endpoint that streams for as
#: long as the worker has memory, and ``resp.text`` would buffer all of it.
#:
#: 8 MiB is far past any job posting. The parsers downstream cut to 20k
#: characters anyway, so this ceiling is only ever reached by something that is
#: not a job posting.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: The longest one response body may take to arrive, however small it is.
#:
#: :data:`MAX_RESPONSE_BYTES` bounds how much memory a response can cost. It
#: does not bound how much *time* one costs, and those are different attacks
#: with different remedies. ``timeout`` limits how long a single socket read may
#: stall; a server that answers every read just before it expires never trips
#: it, and the ceiling above is only reached after however long it takes to
#: trickle eight mebibytes. At 64 bytes a read, eleven seconds apart, under the
#: twelve-second ``scraper_timeout_seconds`` this deployment passes, that is
#: about seventeen days holding one worker on one response.
#:
#: Which is not hypothetical in the way SSRF is. Every one of these fetches goes
#: somewhere nobody here controls — five ATS vendors, four job aggregators, and
#: whatever careers page the scraper followed an anchor to — and a load balancer
#: having a bad day trickles bytes exactly like this on purpose.
#:
#: 30 seconds is far past any of these endpoints' honest worst case; the slowest
#: real answer measured here is the SerpAPI search, which gets ``timeout * 2``
#: and returns well inside it.
MAX_BODY_SECONDS = 30.0

#: Read granularity. Small enough that the ceiling is enforced promptly, large
#: enough not to make a syscall per kilobyte.
_CHUNK_BYTES = 64 * 1024


class BlockedURLError(ValueError):
    """Raised when a URL resolves somewhere an outbound fetch may not go.

    A ``ValueError`` because that is what it is — the caller passed a URL that
    is not usable — and because both call sites already translate a bad URL
    into a message for the user rather than a 500.
    """


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether *ip* is a routable internet address we are willing to fetch.

    ``is_global`` is the single check that covers loopback, RFC 1918, link-local
    (169.254/16 — the cloud metadata range), carrier-grade NAT, multicast and
    the reserved blocks, in both address families. It is written out rather than
    used bare because IPv4-mapped IPv6 (``::ffff:127.0.0.1``) is *not* global in
    its own right on every Python version, and a v6 literal wrapping a private
    v4 address has to be judged as the v4 address it is.
    """
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped  # type: ignore[union-attr]
    return bool(ip.is_global) and not ip.is_multicast


def _resolved_addresses(host: str) -> list[str]:
    """Every address *host* resolves to, or the literal it already is."""
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    except socket.gaierror as exc:
        raise BlockedURLError(f"could not resolve {host}") from exc


def check_url(url: str) -> None:
    """Raise :class:`BlockedURLError` unless *url* may be fetched.

    Every address the host resolves to has to be public, not merely one of
    them: a name with an A record for a real server and a second for
    ``127.0.0.1`` would otherwise pass the check and then connect to whichever
    the resolver handed the socket.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedURLError(
            f"only {' and '.join(ALLOWED_SCHEMES)} URLs can be fetched, not "
            f"{parsed.scheme or 'a scheme-less URL'!r}"
        )

    host = parsed.hostname
    if not host:
        raise BlockedURLError("the URL has no host")

    addresses = _resolved_addresses(host)
    if not addresses:  # pragma: no cover - getaddrinfo raises instead
        raise BlockedURLError(f"could not resolve {host}")

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - getaddrinfo returns literals
            raise BlockedURLError(f"{host} resolved to something unreadable") from None
        if not _is_public(ip):
            raise BlockedURLError(
                f"{host} resolves to {address}, which is not a public address"
            )


def _cap_body(
    response: requests.Response,
    limit: int = MAX_RESPONSE_BYTES,
    deadline_seconds: float = MAX_BODY_SECONDS,
) -> bool:
    """Read at most *limit* bytes of *response*, for at most *deadline_seconds*.

    The response comes back streamed so that nothing is buffered before this
    runs; the body is then read chunk by chunk and stored the same way
    ``requests`` stores it, so ``.text``, ``.content`` and ``.json()`` all keep
    working — and keep decoding with the charset the headers declared. Which is
    the wrong charset for a page whose headers declare none: see
    :func:`page_text`, which is what the HTML callers read instead of ``.text``.

    Truncated rather than refused. Everything downstream already cuts the text
    far shorter than this ceiling, so cutting a body that reaches it costs a
    caller nothing they would have used, while refusing would turn one
    over-large page into a failed scrape. Returns whether it cut.

    Both ceilings are checked between chunks, because they answer different
    questions and a body can be innocent by one and abusive by the other: a
    response that trickles sixty-four bytes at a time is nowhere near the byte
    limit and is still holding this worker indefinitely. See
    :data:`MAX_BODY_SECONDS`.

    A truncated read leaves bytes on the socket, so the connection is closed
    rather than returned to the pool — a half-read response put back in a
    keep-alive pool desynchronises the next request on it.
    """
    body = bytearray()
    truncated = False
    # Monotonic: a body read must not be lengthened or cut short by the clock
    # being stepped underneath it.
    started = time.monotonic()
    try:
        for chunk in response.iter_content(_CHUNK_BYTES):
            if not chunk:
                continue
            body += chunk
            if len(body) >= limit:
                del body[limit:]
                truncated = True
                logger.warning(
                    "outbound: truncated %s at %s bytes", response.url, limit
                )
                break
            if time.monotonic() - started >= deadline_seconds:
                truncated = True
                logger.warning(
                    "outbound: truncated %s after %.1fs with %s bytes read",
                    response.url,
                    deadline_seconds,
                    len(body),
                )
                break
    finally:
        if truncated:
            response.close()

    # What `requests` sets when it consumes a body itself; assigning both is
    # what makes `.text` and `.content` read from here instead of trying to
    # stream a socket that has already been drained.
    response._content = bytes(body)  # noqa: SLF001
    response._content_consumed = True  # type: ignore[attr-defined]  # noqa: SLF001
    return truncated


def capped_get(
    session: requests.Session | None,
    url: str,
    **kwargs,
) -> requests.Response:
    """``GET`` *url* with the body ceiling, but no address check.

    For the fetches that go to a **fixed, known host** — the job aggregators in
    ``job_search_service``, the ATS board APIs in ``ats_boards``, the search
    endpoint in ``career_scraper``. None of those take a caller's URL, so the
    SSRF rules :func:`safe_get` enforces have nothing to bite on: the
    destination is a literal in the source, or a template filled with a token
    matched against ``^[A-Za-z0-9][A-Za-z0-9._-]{0,98}$``.

    The *volume* rule still applies, and did not used to. "Nobody can choose the
    host" is an argument about where the bytes come from, and says nothing about
    how many of them there are — the ceiling is the one guarantee in this module
    that does not depend on trusting the destination. These call sites all end in
    ``resp.json()`` or ``resp.text``, which buffers whatever arrives, and a
    third-party API having a bad day is a far more ordinary event than an
    attacker: an aggregator answering a paginated query with its entire corpus,
    or a hijacked domain, is an unbounded read into a worker the deployment runs
    exactly one of.

    Redirects are followed by ``requests`` as usual — there is no address to
    re-check between hops, so there is no reason to walk them by hand here.
    """
    kwargs.pop("stream", None)
    get = session.get if session is not None else requests.get
    response = get(url, stream=True, **kwargs)
    _cap_body(response)
    return response


def safe_get(
    session: requests.Session | None,
    url: str,
    **kwargs,
) -> requests.Response:
    """``GET`` *url*, checking the destination of every hop.

    Drop-in for ``session.get(url, allow_redirects=True, ...)`` — the redirect
    chain is walked here instead, because ``requests`` resolves and connects to
    each ``Location`` without giving the caller a chance to veto it. The
    response returned is the final one, with ``history`` populated the same way
    ``requests`` would.

    Every body is read under :data:`MAX_RESPONSE_BYTES`. A destination that
    passes the address check can still answer with an unbounded stream, and the
    timeout does not cover that — see the constant.

    *session* may be ``None``, in which case a plain ``requests.get`` is used;
    that keeps the one caller that has no session from having to invent one.
    """
    kwargs.pop("allow_redirects", None)
    kwargs.pop("stream", None)
    get = session.get if session is not None else requests.get

    history: list[requests.Response] = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        check_url(current)
        response = get(current, allow_redirects=False, stream=True, **kwargs)
        _cap_body(response)
        if not response.is_redirect or not response.headers.get("location"):
            response.history = history
            return response

        history.append(response)
        # `urljoin` via requests' own resolver, so a relative `Location` (which
        # is legal, and common) is resolved against the URL it came from rather
        # than being handed to `check_url` as a path with no host.
        current = urljoin(response.url or current, response.headers["location"])
        logger.debug("outbound: following redirect to %s", current)

    raise BlockedURLError(f"too many redirects from {url}")


# --------------------------------------------------------------------------- #
# Reading a fetched page                                                       #
# --------------------------------------------------------------------------- #

#: The charset named on the response itself. Authoritative when present — a
#: document's own declaration does not get to overrule the server's.
_HEADER_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?\s*([\w:.+-]+)""", re.I)

#: The document's own declaration, in either of the two spellings HTML allows.
#: Read from the head of the body, because that is where it is required to be
#: and because the pattern must not go hunting through a page that quotes the
#: word in its prose.
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]*?charset\s*=\s*["']?\s*([\w:.+-]+)""", re.I
)
_META_SNIFF_BYTES = 4096

#: What a page that declares nothing is read as when its bytes are valid UTF-8.
_UTF8 = "utf-8"

#: And when they are not. Never raises under ``errors="replace"``, and a
#: superset of Latin-1 over the printable range, which is what the pages that
#: declare nothing and are not UTF-8 are written in.
_FALLBACK_PAGE_CHARSET = "cp1252"


def _header_charset(response: requests.Response) -> str | None:
    match = _HEADER_CHARSET_RE.search(response.headers.get("Content-Type", ""))
    return match.group(1) if match else None


def _meta_charset(raw: bytes) -> str | None:
    match = _META_CHARSET_RE.search(raw[:_META_SNIFF_BYTES])
    if match is None:
        return None
    try:
        return match.group(1).decode("ascii")
    except UnicodeDecodeError:  # pragma: no cover - the class is ASCII already
        return None


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode(_UTF8)
    except UnicodeDecodeError:
        return False
    return True


def page_text(response: requests.Response) -> str:
    """A fetched page's HTML as text, decoded the way a browser would.

    ``requests`` decides ``.text`` from the ``Content-Type`` header alone, and
    for a ``text/*`` response that names no charset it falls back to ISO-8859-1
    — RFC 2616's default, which HTML5 abandoned and which no browser has
    honoured for years. Serving HTML with no charset parameter and declaring it
    in a ``<meta>`` tag instead is entirely ordinary: it is nginx's default, and
    it is how a good share of the careers pages and job boards this fetches are
    served.

    Every one of those came back mojibaked — the UTF-8 bytes read one at a time
    as Latin-1, so "Développeur" arrives as "DÃ©veloppeur" and "55 000 €" as
    "55 000 â¬". That is not a display problem, because nothing displays this
    text: it is the job description the parsers read.
    :func:`app.services.jd_parser.extract_salary` finds no currency symbol and
    so no band; :func:`app.services.places.fold_diacritics` cannot fold "Ã©"
    back to "e", so the location, the title and every skill comparison in
    :mod:`app.services.fit_scorer` are matching a word that is no longer the
    word; and the mangled text is what the tailoring prompt is handed and what
    the anti-invention check compares its output against.

    Precedence is the browser's: the header decides when it says anything, then
    the document's own ``<meta charset>``. A page that declares nothing anywhere
    is UTF-8 if its bytes are valid UTF-8 — non-ASCII bytes that decode cleanly
    essentially never do so by accident — and cp1252 if they are not, which is
    what the undeclared pages that are not UTF-8 are written in.
    """
    raw = response.content
    if not raw:
        return ""
    charset = (
        _header_charset(response)
        or _meta_charset(raw)
        or (_UTF8 if _is_utf8(raw) else _FALLBACK_PAGE_CHARSET)
    )
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        logger.warning("outbound: unknown page charset %r on %s", charset, response.url)
        return raw.decode(_FALLBACK_PAGE_CHARSET, errors="replace")


__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_BODY_SECONDS",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "BlockedURLError",
    "capped_get",
    "check_url",
    "page_text",
    "safe_get",
]
