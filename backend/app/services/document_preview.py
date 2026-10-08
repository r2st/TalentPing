"""A viewable rendering of a document the browser refuses to draw.

A browser renders a PDF and an image in place. Pointed at a .docx it draws
nothing at all — an Office document is a zip of XML, and "nothing at all" on
screen is indistinguishable from a broken preview. So the preview said so and
offered a download, which is honest but leaves the candidate's commonest resume
format — the .docx sitting in Documents — as the one document the product will
send under their name and cannot show them. This module converts it.

Two hard rules shape everything here:

* **The conversion is for reading only.** Nothing in this module is ever
  attached to an email. The sender resolves its bytes through
  :func:`app.services.email_attachments.document_for_resume`, which returns the
  candidate's upload verbatim, and no code path leads from here to there. A
  recruiter receives the .docx the candidate uploaded, byte for byte, whatever
  this module made of it for the screen.
* **The rendering is built, not passed through.** Every tag in the output HTML
  is one this module emitted; every piece of text from the document is escaped
  on the way in. There is no sanitiser to get wrong because untrusted markup is
  never in the output to begin with — the input is WordprocessingML, and only
  the text nodes of it survive.

Formats, and why each is handled the way it is:

``.docx``
    Converted here, with no dependency at all: a .docx is a zip of XML and the
    part of it a reader cares about is a dozen element names wide.
    :mod:`app.services.resume_parser` already reads the text layer this way,
    deliberately, because the alternative is a new runtime dependency on a box
    this product deploys to natively. The same reasoning applies with more
    force to a preview, which must work on every deployment or it is a feature
    that exists in development only.

``.doc``
    Word 97-2003, a binary format with no relation to the zip above and no
    honest way to read it in-process. Converted by LibreOffice **to a PDF**
    when ``soffice`` happens to be on the box, and not at all when it isn't —
    the UI falls back to naming the format and offering the download. A PDF
    rather than LibreOffice's HTML on purpose: the browser draws a PDF itself,
    and it keeps the promise above that no markup this module didn't write ever
    reaches the page. Install ``libreoffice-writer`` to turn this path on; the
    product is correct without it.

Anything else (a spreadsheet, a deck, an unknown binary) has no preview. That
is a fact about the file, and the caller says so rather than framing a blank.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from html import escape
from xml.etree import ElementTree

from app.core import office_xml
from app.core.office_xml import OfficeDocumentError, SafeArchive
from app.services import email_attachments

logger = logging.getLogger(__name__)

# Namespaces. Word writes these on every part it produces.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_PKG_R = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_BODY = "word/document.xml"

#: What a rendering is allowed to do, as a header and as a ``<meta>`` inside the
#: document itself. Belt-and-braces rather than the defence — the body contains
#: no markup this module didn't write and every text node came through
#: ``escape`` — but this response is a whole HTML document served from the API's
#: origin, and "no scripts, no network, images only from this document" costs
#: nothing to be right about. The ``<meta>`` matters more than the header in
#: practice: the page is read through a blob URL, which carries no headers.
CONTENT_SECURITY_POLICY = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"

#: What the produced HTML is served as. The charset is not optional: Word
#: documents are full of typographic punctuation, and a preview that renders a
#: candidate's name with a mojibake apostrophe looks like our bug.
HTML_MEDIA_TYPE = "text/html; charset=utf-8"

#: Formats a browser will not draw in a frame, and therefore the files a preview
#: has to convert or refuse. Mirrors ``UNRENDERABLE`` in the frontend's
#: ``FilePreview`` — the UI decides whether to *ask* for a preview, this decides
#: what to do about it, and the two must agree on which files are in question.
_UNRENDERABLE = re.compile(r"\.(docx?|xlsx?|pptx?|od[tsp]|rtf|pages)$", re.I)

#: Pictures worth embedding, and what they are. An unrecognised part is skipped
#: rather than guessed at: the value goes into a ``data:`` URI, and a wrong type
#: there draws a broken-image icon where the document has a photograph.
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

#: Base64 inflates by a third and the whole preview is held in memory twice over
#: — once as HTML, once as the blob the browser keeps. A resume's photograph is
#: tens of kilobytes; anything past this budget is a scanned page or a stock
#: background, and the text is what the user came to check.
_MAX_EMBEDDED_IMAGE_BYTES = 2 * 1024 * 1024

#: English Metric Units per CSS pixel — Word stores picture sizes in EMU.
_EMU_PER_PX = 9525

#: How long LibreOffice gets to convert one .doc. It is a full office suite
#: starting cold; it is also a preview nobody will wait a minute for.
_SOFFICE_TIMEOUT_SECONDS = 45

_LINK_SCHEMES = ("http://", "https://", "mailto:")


@dataclass(frozen=True)
class Preview:
    """A document as something the browser can draw.

    ``converted`` is the flag the UI needs and the reason this is a dataclass
    rather than a pair of bytes: a preview of a Word document is a rendering,
    not the file, and the screen has to say so. The user is looking at this
    frame to decide whether the right document goes out under their name, and
    "close enough to read" is a different promise from "this is the file".

    ``filename`` is what the rendering is called — the original name plus the
    new extension, so a header, a title bar or a save dialog can't imply the
    conversion is the document itself.
    """

    content: bytes
    media_type: str
    filename: str
    converted: bool
    #: What the original was, for a UI that wants to name it. ``None`` when this
    #: preview *is* the original.
    source_filename: str | None = None


def _suffix(filename: str) -> str:
    return os.path.splitext((filename or "").lower())[1]


def needs_conversion(filename: str) -> bool:
    """Whether a file of this name has to be converted to be readable in a page.

    Answered from the name rather than the bytes for the same reason the
    uploader accepts or refuses on the extension: three MIME spellings of
    .docx are in the wild plus ``application/octet-stream`` from a
    drag-and-drop, and the extension is the one thing the user can also see.
    """
    return bool(_UNRENDERABLE.search(filename or ""))


# --------------------------------------------------------------------------- #
# WordprocessingML -> HTML                                                     #
# --------------------------------------------------------------------------- #


def _val(node: ElementTree.Element | None, attr: str = f"{_W}val") -> str | None:
    return None if node is None else node.get(attr)


def _is_on(properties: ElementTree.Element | None, name: str) -> bool:
    """Whether a run property like ``w:b`` is set.

    Presence means on, which is why this isn't a simple ``find`` — Word also
    writes the element *off* (``<w:b w:val="0"/>``) to override a style, and
    reading that as bold would embolden exactly the runs the author unbolded.
    """
    if properties is None:
        return False
    node = properties.find(f"{_W}{name}")
    if node is None:
        return False
    return _val(node) not in ("0", "false", "off")


def _paragraph_style(paragraph: ElementTree.Element) -> str:
    style = _val(paragraph.find(f"{_W}pPr/{_W}pStyle")) or ""
    return re.sub(r"[^a-z0-9]", "", style.lower())


def _heading_tag(style: str) -> str | None:
    """The HTML heading a Word paragraph style corresponds to, if any.

    Only three levels: a resume has a name, section headings and body text, and
    an ``<h6>`` in a preview is smaller than the text it introduces.
    """
    if style in ("title", "subtitle"):
        return "h1"
    match = re.fullmatch(r"heading(\d)", style)
    if match is None:
        return None
    return f"h{min(int(match.group(1)) + 1, 4)}"


class _Rels:
    """One part's relationships: the ids its XML points at, resolved to targets.

    A hyperlink and a picture both carry an ``r:id`` rather than a URL or a
    filename, and the mapping lives in a sibling ``_rels`` part. Missing rels
    are not an error — a document with no links and no images has no rels file
    to read.
    """

    def __init__(self, archive: SafeArchive, part: str) -> None:
        self._targets: dict[str, str] = {}
        self._external: set[str] = set()
        folder, _, name = part.rpartition("/")
        rels_part = f"{folder}/_rels/{name}.rels"
        try:
            xml = archive.read(rels_part)
        except KeyError:
            return
        try:
            root = office_xml.parse_xml(xml)
        except ElementTree.ParseError:
            logger.info("unreadable relationships part %s — links dropped", rels_part)
            return
        for rel in root.iter(f"{_PKG_R}Relationship"):
            rel_id = rel.get("Id")
            target = rel.get("Target")
            if not rel_id or not target:
                continue
            self._targets[rel_id] = target
            if (rel.get("TargetMode") or "").lower() == "external":
                self._external.add(rel_id)

    def url(self, rel_id: str | None) -> str | None:
        """An external URL this id points at, restricted to schemes worth linking.

        A relationship target can be a path inside the package or a URL, and it
        can name any scheme at all — ``javascript:`` among them. Only the three
        that mean "somewhere a reader might go" survive.
        """
        if not rel_id:
            return None
        target = self._targets.get(rel_id)
        if not target:
            return None
        lowered = target.strip().lower()
        if not lowered.startswith(_LINK_SCHEMES):
            return None
        return target.strip()

    def part(self, rel_id: str | None, *, relative_to: str) -> str | None:
        """The zip entry this id points at, or ``None`` for an external target."""
        if not rel_id or rel_id in self._external:
            return None
        target = self._targets.get(rel_id)
        if not target:
            return None
        folder = relative_to.rpartition("/")[0]
        return os.path.normpath(os.path.join(folder, target)).replace(os.sep, "/")


class _Numbering:
    """Which numbering definitions are bullets and which are counted.

    Word does not record "this is a bulleted list" on the paragraph. The
    paragraph names a ``numId``, that resolves to an abstract definition, and
    that definition's level says whether it draws a glyph or a number. Two
    lookups to tell ``<ul>`` from ``<ol>`` — worth it, because a numbered list
    of publications rendered as bullets loses the author's meaning, and the
    lookup is the only place that information exists.
    """

    def __init__(self, archive: SafeArchive) -> None:
        self._formats: dict[tuple[str, str], str] = {}
        self._abstract_for: dict[str, str] = {}
        try:
            root = office_xml.parse_xml(archive.read("word/numbering.xml"))
        except (KeyError, ElementTree.ParseError):
            return
        for num in root.iter(f"{_W}num"):
            num_id = num.get(f"{_W}numId")
            abstract = _val(num.find(f"{_W}abstractNumId"))
            if num_id and abstract:
                self._abstract_for[num_id] = abstract
        for abstract in root.iter(f"{_W}abstractNum"):
            abstract_id = abstract.get(f"{_W}abstractNumId")
            if not abstract_id:
                continue
            for level in abstract.iter(f"{_W}lvl"):
                ilvl = level.get(f"{_W}ilvl") or "0"
                fmt = _val(level.find(f"{_W}numFmt"))
                if fmt:
                    self._formats[(abstract_id, ilvl)] = fmt.lower()

    def tag_for(self, num_id: str | None, ilvl: str) -> str:
        """``ul`` or ``ol`` for one list paragraph. Unknown definitions bullet.

        A list whose definition we couldn't read is still a list, and a bullet
        is the answer that invents the least — an ``<ol>`` would print numbers
        the document may never have had.
        """
        abstract = self._abstract_for.get(num_id or "")
        fmt = self._formats.get((abstract or "", ilvl)) if abstract else None
        if fmt is None or fmt in ("bullet", "none"):
            return "ul"
        return "ol"


class _Blocks:
    """Block-level HTML, with the list nesting Word only implies.

    WordprocessingML has no list element. It has paragraphs that each name a
    numbering definition and an indent level, and a reader infers the ``<ul>``
    around them. This does that inference in one place: every block goes
    through :meth:`block` or :meth:`item`, so a heading arriving mid-list can't
    leave an unclosed tag behind it.
    """

    def __init__(self) -> None:
        self._out: list[str] = []
        # (tag, level, whether opening it reopened the <li> above it)
        self._open: list[tuple[str, int, bool]] = []

    def block(self, html: str) -> None:
        self._close_to(-1)
        self._out.append(html)

    def item(self, tag: str, level: int, html: str) -> None:
        self._close_to(level)
        if not self._open or self._open[-1][1] < level:
            nested = bool(self._open) and self._take_back_item()
            self._out.append(f"<{tag}>")
            self._open.append((tag, level, nested))
        elif self._open[-1][0] != tag:
            # Same level, different kind of list: the author ended one list and
            # began another, and nesting them would indent the second.
            previous = self._open.pop()
            self._out.append(f"</{previous[0]}>")
            self._out.append(f"<{tag}>")
            self._open.append((tag, level, previous[2]))
        self._out.append(f"<li>{html}</li>")

    def _take_back_item(self) -> bool:
        """Put a nested list *inside* the item it hangs off, not beside it.

        A ``<ul>`` as a direct child of a ``<ul>`` is the most direct
        translation of the flat paragraph stream Word gives us, and browsers do
        indent it — but it isn't valid, and a sub-bullet belongs to the bullet
        above it. So the enclosing ``</li>`` is taken back off here and
        re-emitted when the nested list closes. Returns whether one is owed.
        """
        if self._out and self._out[-1].endswith("</li>"):
            self._out[-1] = self._out[-1][: -len("</li>")]
            return True
        return False

    def _close_to(self, level: int) -> None:
        while self._open and self._open[-1][1] > level:
            tag, _, owes_item = self._open.pop()
            self._out.append(f"</{tag}>" + ("</li>" if owes_item else ""))

    def html(self) -> str:
        self._close_to(-1)
        return "".join(self._out)

    def __bool__(self) -> bool:
        return bool(self._out)


class _Converter:
    """One .docx, being turned into one HTML document."""

    def __init__(self, archive: SafeArchive) -> None:
        self._archive = archive
        self._names = set(archive.namelist())
        self._numbering = _Numbering(archive)
        self._image_budget = _MAX_EMBEDDED_IMAGE_BYTES

    # -- inline ------------------------------------------------------------- #

    def _image(self, drawing: ElementTree.Element, rels: _Rels, part: str) -> str:
        """A picture, inlined as a ``data:`` URI, or nothing.

        Inlined rather than served from an endpoint of its own because the frame
        is fed a blob: a preview assembled from a dozen authenticated sub-
        requests would need every one of them to carry a bearer token an
        ``<img src>`` cannot send.
        """
        blip = drawing.find(f".//{_A}blip")
        target = rels.part(_val(blip, f"{_R}embed"), relative_to=part)
        if target is None or target not in self._names:
            return ""
        mime = _IMAGE_TYPES.get(_suffix(target))
        if mime is None:
            return ""
        try:
            # The budget is the read's ceiling, not a check after it: an entry
            # that declares 4 KB and inflates to 4 GB would otherwise be in
            # memory by the time its size was consulted.
            data = self._archive.read(target, limit=self._image_budget)
        except KeyError:  # pragma: no cover - guarded by the membership test
            return ""
        except OfficeDocumentError:
            # One oversized picture is not a reason to refuse the document. The
            # reader wants the text either way, and the frame says nothing is
            # missing that the alt-less <img> wouldn't have said anyway.
            logger.info("skipping %s in preview: past the image budget", target)
            return ""
        if not data:
            return ""
        self._image_budget -= len(data)

        # Word writes the on-page size twice: once on the anchor (``wp:extent``)
        # and once on the shape's transform (``a:ext``). The anchor's is the one
        # that survives cropping, so it leads.
        extent = drawing.find(f".//{_WP}extent")
        if extent is None:
            extent = drawing.find(f".//{_A}ext")
        style = ""
        try:
            width = int(extent.get("cx", "")) // _EMU_PER_PX if extent is not None else 0
        except ValueError:
            width = 0
        if width > 0:
            style = f' style="width:{width}px"'
        encoded = base64.b64encode(data).decode("ascii")
        return f'<img src="data:{mime};base64,{encoded}" alt=""{style}>'

    def _run(self, run: ElementTree.Element, rels: _Rels, part: str) -> str:
        properties = run.find(f"{_W}rPr")
        pieces: list[str] = []
        for node in run.iter():
            tag = node.tag
            if tag == f"{_W}t":
                pieces.append(escape(node.text or ""))
            elif tag == f"{_W}tab":
                pieces.append('<span class="tab"></span>')
            elif tag in (f"{_W}br", f"{_W}cr"):
                pieces.append("<br>")
            elif tag == f"{_W}drawing":
                pieces.append(self._image(node, rels, part))
            elif tag == f"{_W}sym":
                # A symbol-font bullet or dingbat. The character lives in a
                # private-use area under Wingdings and means nothing in a
                # browser font, so it becomes the bullet it was standing in for.
                pieces.append("&bull;")
        html = "".join(pieces)
        if not html:
            return ""
        for name, wrapper in (("b", "strong"), ("i", "em"), ("u", "u")):
            if _is_on(properties, name):
                html = f"<{wrapper}>{html}</{wrapper}>"
        if _is_on(properties, "strike") or _is_on(properties, "dstrike"):
            html = f"<s>{html}</s>"
        return html

    def _inline(self, parent: ElementTree.Element, rels: _Rels, part: str) -> str:
        """The inline content of a paragraph, in document order.

        Walks the paragraph's own children rather than ``iter()`` so a
        hyperlink's runs are rendered once, inside the anchor, instead of twice.
        """
        pieces: list[str] = []
        for child in parent:
            if child.tag == f"{_W}r":
                pieces.append(self._run(child, rels, part))
            elif child.tag == f"{_W}hyperlink":
                inner = self._inline(child, rels, part)
                url = rels.url(child.get(f"{_R}id"))
                if not inner:
                    continue
                if url is None:
                    pieces.append(inner)
                else:
                    # Opened in a new tab because the frame is sandboxed: a
                    # same-frame navigation would replace the document the user
                    # is in the middle of reading with a website.
                    pieces.append(
                        f'<a href="{escape(url, quote=True)}" target="_blank" '
                        f'rel="noopener noreferrer">{inner}</a>'
                    )
            elif child.tag in (f"{_W}smartTag", f"{_W}ins", f"{_W}sdt", f"{_W}sdtContent"):
                # Wrappers around ordinary runs: a smart tag, an accepted
                # tracked insertion, a content control. Their text is the
                # author's text and belongs in the preview.
                pieces.append(self._inline(child, rels, part))
        return "".join(pieces)

    # -- blocks ------------------------------------------------------------- #

    def _paragraph(
        self, paragraph: ElementTree.Element, blocks: _Blocks, rels: _Rels, part: str
    ) -> None:
        html = self._inline(paragraph, rels, part)
        if not html.strip():
            # Word documents are full of empty paragraphs used as spacing. They
            # are not content, and CSS margins already space what is.
            return
        style = _paragraph_style(paragraph)
        numbering = paragraph.find(f"{_W}pPr/{_W}numPr")
        if numbering is not None:
            ilvl = _val(numbering.find(f"{_W}ilvl")) or "0"
            num_id = _val(numbering.find(f"{_W}numId"))
            try:
                level = int(ilvl)
            except ValueError:
                level = 0
            blocks.item(self._numbering.tag_for(num_id, ilvl), level, html)
            return

        heading = _heading_tag(style)
        if heading is not None:
            blocks.block(f"<{heading}>{html}</{heading}>")
        elif style.startswith("listparagraph") and html.lstrip().startswith(("•", "-", "‣", "◦")):
            # A bullet typed by hand inside Word's list style, with no numbering
            # definition behind it. Common in resumes exported from other tools.
            blocks.item("ul", 0, html.lstrip(" •-‣◦"))
        else:
            blocks.block(f"<p>{html}</p>")

    def _table(
        self, table: ElementTree.Element, blocks: _Blocks, rels: _Rels, part: str
    ) -> None:
        """A table, which in a resume is usually the layout rather than data.

        Rendered as a table anyway: two-column resumes put dates in one column
        and roles in the other, and flattening that reads as a list of years
        followed by a list of jobs.
        """
        rows: list[str] = []
        for row in table.findall(f"{_W}tr"):
            cells: list[str] = []
            for cell in row.findall(f"{_W}tc"):
                inner = _Blocks()
                self._body(cell, inner, rels, part)
                cells.append(f"<td>{inner.html()}</td>")
            if cells:
                rows.append(f"<tr>{''.join(cells)}</tr>")
        if rows:
            blocks.block(f"<table>{''.join(rows)}</table>")

    def _body(
        self, parent: ElementTree.Element, blocks: _Blocks, rels: _Rels, part: str
    ) -> None:
        """Paragraphs and tables of one container, in the order they appear."""
        for child in parent:
            if child.tag == f"{_W}p":
                self._paragraph(child, blocks, rels, part)
            elif child.tag == f"{_W}tbl":
                self._table(child, blocks, rels, part)
            elif child.tag in (f"{_W}sdt", f"{_W}sdtContent", f"{_W}txbxContent"):
                # A content control or a text box. Resumes built from templates
                # keep whole sections inside these, and skipping them drops the
                # section without anything looking wrong.
                self._body(child, blocks, rels, part)

    def _part_html(self, part: str) -> str:
        try:
            root = office_xml.parse_xml(self._archive.read(part))
        except (KeyError, ElementTree.ParseError):
            logger.info("unreadable part %s — dropped from the preview", part)
            return ""
        rels = _Rels(self._archive, part)
        blocks = _Blocks()
        body = root.find(f"{_W}body")
        self._body(body if body is not None else root, blocks, rels, part)
        return blocks.html()

    def run(self) -> str:
        """The document's HTML: headers, then the body, then footers.

        The same order :mod:`app.services.resume_parser` reads the parts in, and
        for the same reason — a resume routinely puts the candidate's name and
        contact details in a Word header, and a preview that dropped them would
        be missing the first thing the reader looks for. Word repeats a header
        on every page; once, at the top, is the closest a single scrolling frame
        gets.
        """
        chunks = [self._part_html(part) for part in self._parts()]
        return "".join(chunk for chunk in chunks if chunk)

    def _parts(self) -> list[str]:
        headers = sorted(n for n in self._names if n.startswith("word/header"))
        footers = sorted(n for n in self._names if n.startswith("word/footer"))
        return [*headers, _BODY, *footers]


# Deliberately plain, and deliberately light. A document is a white page — the
# app's dark surface behind one reads as a rendering fault, which is the exact
# confusion this whole feature exists to remove. Sized like a page rather than
# stretched to the frame so long lines stay readable.
_PAGE_CSS = """
:root { color-scheme: light; }
html { background: #f1f1f4; }
body {
  margin: 0; padding: 32px 16px;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  color: #1a1a1c;
}
.page {
  max-width: 46rem; margin: 0 auto; padding: 3rem 3.25rem;
  background: #fff; border-radius: 3px;
  box-shadow: 0 1px 3px rgba(0,0,0,.12), 0 8px 24px rgba(0,0,0,.08);
}
h1, h2, h3, h4 { line-height: 1.25; margin: 1.4em 0 .4em; font-weight: 650; }
h1 { font-size: 1.6rem; margin-top: 0; }
h2 { font-size: 1.2rem; letter-spacing: .01em; }
h3 { font-size: 1.03rem; }
h4 { font-size: .95rem; }
p { margin: 0 0 .6em; }
ul, ol { margin: .3em 0 .8em; padding-left: 1.5em; }
li { margin: .18em 0; }
a { color: #1a4fd6; }
img { max-width: 100%; height: auto; }
.tab { display: inline-block; width: 2em; }
table { border-collapse: collapse; width: 100%; margin: .6em 0; }
td { padding: .35em .6em; vertical-align: top; border: 1px solid #e6e6ea; }
/* A single-column table is layout, not data — the borders would draw a grid
   the document doesn't have. */
tr:only-child td:only-child, table tr td:only-child { border: none; padding-left: 0; }
"""


def _document(title: str, body: str) -> bytes:
    """Wrap rendered content in a page the browser can show on its own."""
    return (
        "<!doctype html><html lang=\"en\"><head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        f'content="{CONTENT_SECURITY_POLICY}">'
        f"<title>{escape(title)}</title>"
        f"<style>{_PAGE_CSS}</style>"
        f'</head><body><div class="page">{body}</div></body></html>'
    ).encode()


def html_from_docx(data: bytes) -> str:
    """The readable content of a .docx as HTML, or ``""`` if it has none.

    Raises ``ValueError`` for anything that isn't a Word document — the same
    contract :func:`app.services.resume_parser.extract_text_from_docx` gives its
    caller, so a router can answer with a clean status rather than a traceback.
    A document that is a zip of XML and a hostile one raises the same way; see
    :mod:`app.core.office_xml` for what "hostile" is bounded to mean.
    """
    try:
        archive = office_xml.open_document(data)
    except OfficeDocumentError as exc:
        raise ValueError(f"not a Word document — {exc}") from exc
    with archive:
        if _BODY not in archive.namelist():
            # A zip with no document part is some other Office format: a .doc
            # renamed, a .pages or an .odt export.
            raise ValueError("not a Word document (no word/document.xml)")
        return _Converter(archive).run()


# --------------------------------------------------------------------------- #
# Word 97-2003, via LibreOffice when there is one                              #
# --------------------------------------------------------------------------- #


def soffice_path() -> str | None:
    """LibreOffice's binary, if this box has one.

    A function rather than a module constant so a test can turn the path off
    and on, and so a box that gains LibreOffice doesn't need the workers
    restarted to start using it.
    """
    return shutil.which("soffice") or shutil.which("libreoffice")


def pdf_from_legacy_doc(data: bytes) -> bytes | None:
    """A .doc converted to PDF, or ``None`` if this box can't do it.

    To PDF rather than to HTML on purpose. The browser draws a PDF itself, so
    the conversion needs no markup from us and — more importantly — none from
    LibreOffice: nothing this module returns is ever HTML it didn't write. The
    fidelity is also simply better, which matters most for exactly the format
    we cannot read ourselves.

    Never raises. A conversion that fails, hangs or isn't possible is a preview
    the caller reports as unavailable, which is what the UI already handles.
    """
    binary = soffice_path()
    if binary is None:
        return None
    with tempfile.TemporaryDirectory(prefix="doaide-doc-") as workdir:
        source = os.path.join(workdir, "source.doc")
        with open(source, "wb") as handle:
            handle.write(data)
        try:
            subprocess.run(
                [
                    binary,
                    "--headless",
                    # Its own profile inside the temp dir: a shared one is
                    # locked by the first conversion and every later one then
                    # exits silently having written nothing.
                    f"-env:UserInstallation=file://{workdir}/profile",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    workdir,
                    source,
                ],
                check=True,
                capture_output=True,
                timeout=_SOFFICE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning("LibreOffice timed out converting a .doc for preview")
            return None
        except (subprocess.CalledProcessError, OSError):
            logger.warning("LibreOffice failed converting a .doc", exc_info=True)
            return None
        try:
            with open(os.path.join(workdir, "source.pdf"), "rb") as handle:
                pdf = handle.read()
        except OSError:
            logger.warning("LibreOffice reported success but wrote no PDF")
            return None
    return pdf or None


# --------------------------------------------------------------------------- #
# What a caller asks for                                                       #
# --------------------------------------------------------------------------- #


def preview_for(filename: str, content: bytes, media_type: str | None = None) -> Preview | None:
    """Something the browser can draw for this file, or ``None`` if there isn't.

    A PDF or an image comes back as itself — a preview endpoint that refused
    the formats needing no conversion would be a trap for every caller. A Word
    document comes back converted, flagged as a conversion. Anything else comes
    back ``None``, and the caller says so rather than framing a blank.

    Never raises: a document that cannot be converted is a fact to report, not
    an error, and it is the same answer whether the file is a spreadsheet or a
    corrupt .docx.

    The passthrough is the branch to be careful about, and it was not careful
    enough. "Needs no conversion" was decided by :func:`needs_conversion`, which
    only ever asked whether the name ends in an Office extension — so a
    ``brief.html`` a recruiter emailed was "already renderable", and its bytes
    came back out of this function unread, under the ``mimeType`` its sender
    wrote. That is exactly the pass-through the module docstring promises does
    not exist here. A file may now leave by that branch only under a type this
    module is willing to have a browser draw.
    """
    if not content:
        return None
    if not needs_conversion(filename):
        passthrough = email_attachments.served_media_type(media_type, filename)
        if passthrough == email_attachments.OPAQUE_MEDIA_TYPE:
            # Not convertible and not safe to draw. That is the same answer as a
            # spreadsheet — there is no preview — and the caller already knows
            # how to say so.
            return None
        return Preview(
            content=content,
            media_type=passthrough,
            filename=filename,
            converted=False,
        )

    suffix = _suffix(filename)
    stem = os.path.splitext(filename)[0] or "document"

    if suffix == ".docx":
        try:
            body = html_from_docx(content)
        except ValueError:
            # Named .docx and isn't one. Nothing to show, and a made-up
            # rendering of a file we couldn't read would be worse than saying
            # so — the user is here to check *this* document.
            logger.info("preview: %s is not a readable .docx", filename)
            return None
        except Exception:  # noqa: BLE001 - a preview must never take a page down
            logger.warning("preview: converting %s failed", filename, exc_info=True)
            return None
        if not body.strip():
            return None
        return Preview(
            content=_document(stem, body),
            media_type=HTML_MEDIA_TYPE,
            filename=f"{stem}.html",
            converted=True,
            source_filename=filename,
        )

    if suffix == ".doc":
        pdf = pdf_from_legacy_doc(content)
        if pdf is None:
            return None
        return Preview(
            content=pdf,
            media_type="application/pdf",
            filename=f"{stem}.pdf",
            converted=True,
            source_filename=filename,
        )

    # A spreadsheet, a deck, an .odt. Convertible in principle and not worth
    # pretending to: nothing in this product sends one, and the honest answer
    # costs the user one download.
    return None


#: What an endpoint says when there is no preview to be had. Written to be shown
#: to the user: the file is fine, it is on file, and it will be sent — the only
#: thing missing is a way to draw it in a browser.
UNAVAILABLE_DETAIL = (
    "This file can't be shown in the browser. Download it to read it — "
    "it is attached to emails exactly as it is."
)


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "HTML_MEDIA_TYPE",
    "UNAVAILABLE_DETAIL",
    "Preview",
    "html_from_docx",
    "needs_conversion",
    "pdf_from_legacy_doc",
    "preview_for",
    "soffice_path",
]
