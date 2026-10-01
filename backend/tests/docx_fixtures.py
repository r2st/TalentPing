"""Real .docx bytes, built here rather than committed as a binary.

A test that needs a Word document has two options: a checked-in file nobody can
review in a diff, or the zip of XML a .docx actually is. This is the second. It
is also the only honest way to test the converter — the interesting cases are
specific structures (a numbered list, a header part, a picture, a hyperlink to
``javascript:``) and none of them can be produced on demand from a fixture file.

Everything here mirrors what Word itself writes, down to the namespace
declarations, because the converter reads namespaced tags and a document that
drops them would pass tests while failing on every real upload.
"""
from __future__ import annotations

import io
import zipfile

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
WP_NS = (
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
)

#: A 1x1 transparent GIF. The smallest thing that is genuinely an image.
TINY_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
)


def paragraph(text: str, *, style: str | None = None, bold: bool = False) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    run_properties = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return (
        f"<w:p>{properties}<w:r>{run_properties}<w:t>{text}</w:t></w:r></w:p>"
    )


def bullet(text: str, *, num_id: str = "3", level: str = "0") -> str:
    return (
        "<w:p><w:pPr><w:numPr>"
        f'<w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/>'
        f"</w:numPr></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def table(rows: list[list[str]]) -> str:
    cells = "".join(
        "<w:tr>"
        + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row)
        + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{cells}</w:tbl>"


def hyperlink(text: str, rel_id: str) -> str:
    return (
        f'<w:p><w:hyperlink r:id="{rel_id}">'
        f"<w:r><w:t>{text}</w:t></w:r></w:hyperlink></w:p>"
    )


def picture(rel_id: str, *, width_emu: int = 952500) -> str:
    """An inline picture, shaped the way Word shapes one."""
    return (
        "<w:p><w:r><w:drawing>"
        f'<wp:inline><wp:extent cx="{width_emu}" cy="{width_emu}"/>'
        f'<a:graphic><a:graphicData><a:blip r:embed="{rel_id}"/>'
        "</a:graphicData></a:graphic></wp:inline>"
        "</w:drawing></w:r></w:p>"
    )


#: numId 3 bullets, numId 7 counts. Two lookups behind each: the paragraph names
#: a numId, that resolves to an abstract definition, and the definition's level
#: says which glyph it draws.
NUMBERING = f"""<?xml version="1.0"?>
<w:numbering {W_NS}>
  <w:abstractNum w:abstractNumId="1">
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>
    <w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="2">
    <w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="3"><w:abstractNumId w:val="1"/></w:num>
  <w:num w:numId="7"><w:abstractNumId w:val="2"/></w:num>
</w:numbering>"""


def rels(entries: dict[str, tuple[str, bool]]) -> str:
    """``{id: (target, external)}`` as a relationships part."""
    items = "".join(
        f'<Relationship Id="{rel_id}" Type="x" Target="{target}"'
        + (' TargetMode="External"' if external else "")
        + "/>"
        for rel_id, (target, external) in entries.items()
    )
    return (
        '<?xml version="1.0"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'relationships">{items}</Relationships>'
    )


def build_docx(
    body: str = "",
    *,
    header: str | None = None,
    footer: str | None = None,
    numbering: bool = True,
    relationships: dict[str, tuple[str, bool]] | None = None,
    media: dict[str, bytes] | None = None,
    omit_document: bool = False,
) -> bytes:
    """A .docx holding *body*, which is WordprocessingML block content."""
    document = (
        f'<?xml version="1.0"?><w:document {W_NS} {R_NS} {A_NS} {WP_NS}>'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        if not omit_document:
            archive.writestr("word/document.xml", document)
        if numbering:
            archive.writestr("word/numbering.xml", NUMBERING)
        if relationships:
            archive.writestr("word/_rels/document.xml.rels", rels(relationships))
        for name, content in (media or {}).items():
            archive.writestr(f"word/media/{name}", content)
        if header is not None:
            archive.writestr(
                "word/header1.xml",
                f'<?xml version="1.0"?><w:hdr {W_NS} {R_NS}>{header}</w:hdr>',
            )
        if footer is not None:
            archive.writestr(
                "word/footer1.xml",
                f'<?xml version="1.0"?><w:ftr {W_NS} {R_NS}>{footer}</w:ftr>',
            )
    return buffer.getvalue()


#: A resume-shaped document, used wherever a test needs "a real .docx" and not a
#: specific structure.
RESUME_DOCX = build_docx(
    paragraph("Jordan Candidate", style="Title")
    + paragraph("jordan@example.com", style="Normal")
    + paragraph("Experience", style="Heading1")
    + paragraph("Acme — Staff Backend Engineer", bold=True)
    + bullet("Cut checkout p99 latency by 40%")
    + bullet("Ran the on-call rotation for 12 engineers"),
)
