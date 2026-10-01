"""Reading an upload without letting the sender choose how much memory it costs.

Both upload endpoints in this product were written the same way, and both were
wrong in the same way::

    data = await file.read()
    if len(data) > MAX:
        raise HTTPException(413, ...)

The check is real. It is also too late: ``read()`` with no argument returns the
*whole* body as one ``bytes``, so by the time the limit is consulted the process
has already paid for it. A stranger with an unauthenticated-shaped request and a
4 GB file gets 4 GB of resident memory out of a box with 4 GB of it, and the 413
that would have refused the upload never renders because the kernel takes the
API down first. The size limit protected the database, not the server.

Starlette spools the incoming body to a temporary file rather than holding it,
so the damage is done at exactly one instruction — the unbounded ``read()`` that
pulls it back. :func:`read_capped` is that instruction, done in chunks, refusing
the moment the total passes the limit. Nothing downstream changes: the caller
still gets ``bytes`` and still gets a 413, and the peak cost of the refusal is
now one chunk rather than the whole file.

The size of an upload is not the only thing about it the sender chooses. Its
*name* and its declared *type* are two more client-written strings, and both
were stored verbatim into columns narrower than they are — see
:func:`safe_upload_filename` and :func:`safe_content_type`, which live here for
the same reason ``read_capped`` does: there are two upload endpoints in this
product, and every time one of them has been written by hand it has been
written wrong in the same way as the other.
"""
from __future__ import annotations

import re

from fastapi import HTTPException, UploadFile, status

#: How much is pulled off the spool at a time. Large enough that a legitimate
#: 10 MB resume costs a few dozen reads, small enough that the overshoot past
#: the limit before the refusal is noise.
CHUNK_BYTES = 256 * 1024


def describe_limit(limit: int) -> str:
    """*limit* as the round number a user was told about, e.g. ``"10 MB"``."""
    megabytes = limit / (1024 * 1024)
    rounded = f"{megabytes:.1f}".rstrip("0").rstrip(".")
    return f"{rounded} MB"


async def read_capped(upload: UploadFile, limit: int, *, filename: str) -> bytes:
    """The upload's bytes, or a 413 raised before *limit* of them are held.

    *filename* is only for the message — the caller has usually already cleaned
    it up, and re-deriving it here would risk saying something different from
    the rest of the endpoint's errors.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                # Same 413, current spelling. Starlette deprecated
                # ``HTTP_413_REQUEST_ENTITY_TOO_LARGE`` in favour of this name
                # and warns on every refusal, which put a deprecation notice in
                # the logs on a path a stranger can trigger at will.
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"{filename}: exceeds {describe_limit(limit)}",
            )
        chunks.append(chunk)
    return b"".join(chunks)


#: What every ``filename`` column in this schema is: ``String(255)``.
#: ``resumes.filename``, ``email_attachments.filename`` and — because
#: :mod:`app.tasks.email_tasks` copies the first into it at send time —
#: ``emails.attachment_filename``. One number because one number is true.
MAX_FILENAME_CHARS = 255

#: ``email_attachments.content_type`` is ``String(120)``.
MAX_CONTENT_TYPE_CHARS = 120

#: Anything a terminal, a header parser or Postgres treats as structure rather
#: than as a letter. ``\x00`` is the one that matters most: a text column in
#: Postgres cannot hold it at all, so a name carrying one is not a mangled row
#: but a raised ``ValueError`` from the driver — a 500 on the upload endpoint,
#: on a deployment whose tests all pass because SQLite stores it happily.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Both separator conventions, whichever platform we happen to be on. A name
#: is only ever the last segment: ``os.path.basename`` would leave a Windows
#: path untouched on Linux, which is the half of the problem that travels.
_PATH_SEPARATORS = re.compile(r"[\\/]+")

#: A MIME type as RFC 2045 spells one — ``type/subtype``, tokens either side.
#: Parameters (``; charset=utf-8``) are deliberately not accommodated: nothing
#: downstream reads one, and keeping the stored value to a bare type means the
#: length below is the only thing that can ever need checking.
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+-]{1,64}$")

#: What an unusable declared type becomes. A browser interprets none of it.
OPAQUE_CONTENT_TYPE = "application/octet-stream"


def safe_upload_filename(
    raw: str | None, *, fallback: str, limit: int = MAX_FILENAME_CHARS
) -> str:
    """*raw* reduced to something that is only ever a filename.

    ``UploadFile.filename`` is a string the *client* writes into a multipart
    header. Nothing on the way in constrains it: not its length, not its
    alphabet, not whether it is a name at all rather than a path. Both upload
    endpoints stored it verbatim, and every column it lands in is
    ``String(255)`` — so a 600-character name was an unhandled
    ``StringDataRightTruncation`` and a name holding a NUL byte was an
    unhandled driver ``ValueError``. Both are a 500 the suite could not see,
    because SQLite ignores ``VARCHAR(n)`` and stores ``\\x00`` without
    complaint. Both are reachable by any account with an upload form.

    Three reductions, and the order they happen in is the point:

    * **Control characters go first.** A NUL is what Postgres refuses, and it
      is also the classic way to make two readers disagree about a name:
      ``resume.pdf\\x00.exe`` ends with ``.exe`` to an extension check and with
      ``.pdf`` to anything that stops at the NUL. Removing them before the
      extension is read means there is only ever one reading.
    * **Then the path is discarded.** ``../../../etc/passwd.pdf`` is stored as
      ``passwd.pdf``. Nothing in this codebase joins a stored filename onto a
      directory today — :func:`app.services.email_attachments.header_safe`
      covers the response header, and the artifact endpoints match names
      against a list rather than joining them — but "no caller does the
      dangerous thing" is a property of every caller, re-established with each
      new one. A value that is not a path cannot be traversed with.
    * **Then it is cut to fit**, keeping the extension. Truncating to 255 by
      slicing would drop it, and the extension is not decoration here: it is
      what :func:`app.services.email_attachments.media_type_for` reads, what
      decides whether the preview offers a viewer or a download, and what the
      recipient's machine uses to open the file.

    *fallback* is what comes back when nothing survives — an empty name, a
    lone ``..``, a string of slashes. A bare ``""`` would travel into a MIME
    part some clients render as an unnamed attachment the recipient cannot
    save.
    """
    name = _CONTROL_CHARS.sub("", raw or "")
    name = _PATH_SEPARATORS.split(name)[-1].strip()
    # The two names that are directories rather than files. Any *other* leading
    # dot is left alone: it is a legitimate, if unusual, filename.
    if name in {".", ".."}:
        name = ""
    if not name:
        return fallback
    if len(name) > limit:
        stem, dot, extension = name.rpartition(".")
        # An "extension" longer than this is not one — it is a long name that
        # happens to contain a dot, and cutting to preserve it would keep the
        # wrong half.
        if dot and stem and len(extension) <= 16:
            name = f"{stem[: limit - len(extension) - 1]}.{extension}"
        else:
            name = name[:limit]
    return name or fallback


def safe_content_type(raw: str | None, *, fallback: str) -> str:
    """*raw* if it is a MIME type we could store and serve, else *fallback*.

    Same door as :func:`safe_upload_filename` and the same 500: the attachment
    endpoint stored ``UploadFile.content_type`` — another client-written header
    — straight into a ``String(120)``, so a long enough ``Content-Type`` on the
    multipart part took the request down before the file was ever looked at.

    A type that isn't shaped like one is refused rather than trimmed. There is
    nothing useful to keep from ``application/`` + 300 characters, and half a
    MIME type is a claim about the file rather than an absence of one.
    """
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > MAX_CONTENT_TYPE_CHARS:
        return fallback
    return candidate if _MIME_TYPE.match(candidate) else fallback
