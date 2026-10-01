"""Reading an Office document without believing what it says about itself.

A .docx is a zip of XML, and a resume upload hands both halves of that sentence
to a stranger. Two modules here read one — :mod:`app.services.resume_parser` for
the text layer, :mod:`app.services.document_preview` for the on-screen
rendering — and both did it the direct way::

    archive = zipfile.ZipFile(io.BytesIO(data))
    root = ElementTree.fromstring(archive.read("word/document.xml"))

which is correct for every document Word has ever written and unbounded for the
three a stranger writes on purpose:

**The decompression bomb.** ``ZipFile.read`` inflates an entry to whatever the
entry inflates to. A 10 MB upload — inside the size limit, accepted by the
router, a valid zip with a valid ``word/document.xml`` — holds a few gigabytes
of compressed nulls without difficulty. The upload limit bounds the bytes on the
wire; it says nothing about the bytes after the ``deflate``.

**The part explosion.** Both readers glob ``word/header*`` out of the archive's
own name list and read each match. The number of matches is whatever the
attacker put in the central directory.

**The entity expansion.** ``xml.etree.ElementTree`` expands internal entities,
so the classic billion-laughs DTD works against it verbatim — a kilobyte of XML
that resolves to a gigabyte of text, from a part that passed every size check
above because compressed *and* uncompressed it is tiny. Word never writes a DTD
into a document part. Nothing legitimate is lost by refusing every one.

So the reads go through here instead. :class:`SafeArchive` inflates an entry
under a ceiling and keeps a running budget for the document as a whole, and
:func:`parse_xml` refuses a part whose prolog declares a doctype. Every limit
raises :class:`OfficeDocumentError`, which is a ``ValueError`` — the contract
both readers already give their routers for "this file cannot be read" — so a
hostile document comes back as a 4xx about the file rather than as an outage.

The ceilings are deliberately far above any real resume. A 12 MB part is a Word
document with several hundred pages of text in it; the point is not to be tight,
it is to be finite.
"""
from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

#: The most one entry may inflate to.
MAX_PART_BYTES = 12 * 1024 * 1024

#: The most every entry of one document may inflate to, added up. Bounds the
#: reader that legitimately visits many parts — headers, footers, numbering,
#: relationships, and an image apiece — against an archive where each part is
#: individually reasonable and there are five hundred of them.
MAX_DOCUMENT_BYTES = 48 * 1024 * 1024

#: The most entries a document's central directory may list. Word writes a few
#: dozen; a few thousand is already not a document.
MAX_ENTRIES = 4096


class OfficeDocumentError(ValueError):
    """This document is not one we are willing to finish reading.

    A ``ValueError`` on purpose: both readers already promise their callers that
    an unreadable document raises one, and every router above them already turns
    that into a status about the file.
    """


def _declares_doctype(xml: bytes) -> bool:
    """Whether *xml*'s prolog carries a ``<!DOCTYPE``.

    Only the prolog is examined, because that is the only place a DTD can
    legally appear — and because scanning the whole part would flag a resume
    that merely *writes about* ``<!DOCTYPE html>``, which arrives escaped and is
    perfectly innocent.

    Everything before the root element is whitespace, an XML declaration or
    processing instruction, or a comment. Walking those is a handful of lines
    and leaves no argument about what was matched.
    """
    index = 0
    end = len(xml)
    if xml.startswith(b"\xef\xbb\xbf"):  # a BOM, which Word does write
        index = 3
    while index < end:
        if xml[index : index + 1] in b" \t\r\n":
            index += 1
            continue
        if not xml.startswith(b"<", index):
            # Not XML at all. Let the parser be the one to say so.
            return False
        if xml.startswith(b"<?", index):  # declaration or processing instruction
            close = xml.find(b"?>", index)
            if close < 0:
                return False
            index = close + 2
            continue
        if xml.startswith(b"<!--", index):
            close = xml.find(b"-->", index)
            if close < 0:
                return False
            index = close + 3
            continue
        # The first thing that is neither: either the doctype or the root.
        return xml[index : index + 9].upper() == b"<!DOCTYPE"
    return False


def parse_xml(data: bytes) -> ElementTree.Element:
    """*data* as an element tree, refusing anything that declares a doctype.

    Raises :class:`OfficeDocumentError` for a DTD and ``ElementTree.ParseError``
    for malformed XML — the second is what every caller already handles when a
    part is unreadable, and it stays that way.
    """
    if _declares_doctype(data):
        raise OfficeDocumentError(
            "a document type declaration isn't accepted in an Office part"
        )
    return ElementTree.fromstring(data)


class SafeArchive:
    """A zip whose entries inflate under a ceiling, with a budget for the whole.

    Deliberately shaped like the ``zipfile.ZipFile`` it replaces — ``namelist``,
    ``read``, and a context manager — so the readers above it kept the code they
    had. ``read`` still raises ``KeyError`` for a name the archive doesn't hold,
    which is the signal both of them already branch on.
    """

    def __init__(
        self, archive: zipfile.ZipFile, *, budget: int = MAX_DOCUMENT_BYTES
    ) -> None:
        self._archive = archive
        self._remaining = budget

    def __enter__(self) -> SafeArchive:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._archive.close()

    def namelist(self) -> list[str]:
        return self._archive.namelist()

    @property
    def remaining(self) -> int:
        """Bytes this document may still inflate. Exposed for tests and logs."""
        return self._remaining

    def read(self, name: str, *, limit: int = MAX_PART_BYTES) -> bytes:
        """Entry *name*, inflated, capped at *limit* and at the document budget.

        The cap is enforced by asking for one byte more than is allowed and
        seeing whether it arrives, rather than by trusting the size the central
        directory declares — an attacker writes that field. ``ZipExtFile.read``
        inflates incrementally, so a bomb costs the cap and stops there.
        """
        cap = min(limit, self._remaining)
        with self._archive.open(name) as handle:
            data = handle.read(cap + 1)
        if len(data) > cap:
            raise OfficeDocumentError(
                f"{name} inflates past the {cap} bytes left for this document"
            )
        self._remaining -= len(data)
        return data


def open_document(data: bytes, *, budget: int = MAX_DOCUMENT_BYTES) -> SafeArchive:
    """*data* as a readable Office package, or :class:`OfficeDocumentError`."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = len(archive.namelist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise OfficeDocumentError("the file isn't a zip archive") from exc
    if entries > MAX_ENTRIES:
        archive.close()
        raise OfficeDocumentError(
            f"the archive lists {entries} parts, more than the {MAX_ENTRIES} "
            "a document may have"
        )
    return SafeArchive(archive, budget=budget)
