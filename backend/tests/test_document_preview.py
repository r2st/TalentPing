"""Showing a Word document, without ever sending a converted one.

The preview could open a PDF and not a .docx, which is the format most people's
resume is actually in: a browser pointed at a zip of XML draws nothing, and
nothing on screen is indistinguishable from a broken feature. So the document is
converted for reading.

The load-bearing property is the one that is easy to lose. A conversion exists to
be looked at and must never travel: what a recruiter receives is the candidate's
upload, byte for byte, and the day the send path picks up a rendering instead is
the day a candidate's carefully formatted CV arrives as our approximation of it.
These tests pin both halves — that the rendering is faithful enough to check a
document by, and that no code path leads from it to an email.
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest

from app.services import document_preview
from tests.docx_fixtures import (
    RESUME_DOCX,
    TINY_GIF,
    build_docx,
    bullet,
    hyperlink,
    paragraph,
    picture,
    table,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def html_of(*args, **kwargs) -> str:
    """The converted body of a document built from *args*."""
    return document_preview.html_from_docx(build_docx(*args, **kwargs))


# --------------------------------------------------------------------------- #
# What the reader sees                                                         #
# --------------------------------------------------------------------------- #


class TestTheDocumentIsRecognisable:
    """A preview is only useful if the user can tell which document it is.

    Not pixel fidelity — structure. The name at the top, the section headings,
    the bullets under each role: those are what someone checks when they are
    deciding whether the file about to go out under their name is the right one.
    """

    def test_keeps_the_text(self):
        html = document_preview.html_from_docx(RESUME_DOCX)

        assert "Jordan Candidate" in html
        assert "Cut checkout p99 latency by 40%" in html

    def test_a_title_and_headings_become_headings(self):
        html = html_of(
            paragraph("Jordan Candidate", style="Title")
            + paragraph("Experience", style="Heading1")
            + paragraph("Acme", style="Heading2")
        )

        assert "<h1>Jordan Candidate</h1>" in html
        assert "<h2>Experience</h2>" in html
        assert "<h3>Acme</h3>" in html

    def test_body_text_becomes_paragraphs(self):
        assert "<p>Available immediately</p>" in html_of(paragraph("Available immediately"))

    def test_emphasis_survives(self):
        html = html_of(paragraph("Acme", bold=True))

        assert "<strong>Acme</strong>" in html

    def test_bold_switched_off_is_not_bold(self):
        # Word writes the element *off* to override a style. Reading presence as
        # "on" would embolden exactly the runs the author unbolded.
        html = document_preview.html_from_docx(
            build_docx(
                '<w:p><w:r><w:rPr><w:b w:val="0"/></w:rPr>'
                "<w:t>plain</w:t></w:r></w:p>"
            )
        )

        assert "<strong>" not in html
        assert "plain" in html

    def test_bullets_become_a_list(self):
        html = html_of(bullet("First") + bullet("Second"))

        assert "<ul><li>First</li><li>Second</li></ul>" in html

    def test_a_numbered_list_is_numbered(self):
        # The only place this information exists is numbering.xml, two lookups
        # deep. A numbered list of publications rendered as bullets loses what
        # the author meant by numbering it.
        html = html_of(bullet("One", num_id="7") + bullet("Two", num_id="7"))

        assert "<ol><li>One</li><li>Two</li></ol>" in html

    def test_a_list_with_no_definition_still_bullets(self):
        # A definition we couldn't read is still a list, and a bullet invents
        # the least — an <ol> would print numbers the document never had.
        html = html_of(bullet("Orphan", num_id="99"), numbering=False)

        assert "<ul><li>Orphan</li></ul>" in html

    def test_sub_bullets_nest_inside_the_bullet_they_belong_to(self):
        html = html_of(bullet("Role") + bullet("Detail", level="1") + bullet("Next role"))

        assert "<ul><li>Role<ul><li>Detail</li></ul></li><li>Next role</li></ul>" in html

    def test_a_heading_after_a_list_closes_it(self):
        html = html_of(bullet("Shipped it") + paragraph("Education", style="Heading1"))

        assert "</ul><h2>Education</h2>" in html

    def test_a_two_column_table_stays_two_columns(self):
        # In a resume a table is usually the layout: dates in one column, roles
        # in the other. Flattened, it reads as a list of years followed by an
        # unrelated list of jobs.
        html = html_of(table([["2019", "Staff Engineer"], ["2016", "Senior Engineer"]]))

        assert html.count("<tr>") == 2
        assert "<td><p>2019</p></td><td><p>Staff Engineer</p></td>" in html

    def test_the_word_header_comes_first(self):
        # Resumes routinely put the name and contact details in a Word header,
        # which is the first thing a reader looks for and the easiest thing for a
        # converter to drop silently.
        html = html_of(
            paragraph("Experience", style="Heading1"),
            header=paragraph("jordan@example.com | 555-0100"),
            footer=paragraph("References available"),
        )

        assert html.index("555-0100") < html.index("Experience") < html.index("References")

    def test_empty_paragraphs_are_not_content(self):
        # Word documents are full of empty paragraphs used as spacing. CSS
        # margins already space what is actually there.
        html = html_of(paragraph("One") + "<w:p/>" + paragraph("Two"))

        assert "<p></p>" not in html

    def test_line_breaks_and_tabs_are_kept(self):
        html = document_preview.html_from_docx(
            build_docx(
                "<w:p><w:r><w:t>Acme</w:t><w:tab/><w:t>2019</w:t>"
                "<w:br/><w:t>Initech</w:t></w:r></w:p>"
            )
        )

        assert '<span class="tab"></span>' in html
        assert "<br>" in html

    def test_text_boxes_and_content_controls_are_read(self):
        # Template-built resumes keep whole sections inside these. Skipping them
        # drops the section with nothing looking wrong.
        section = paragraph("Skills", style="Heading1")
        html = document_preview.html_from_docx(
            build_docx(f"<w:sdt><w:sdtContent>{section}</w:sdtContent></w:sdt>")
        )

        assert "<h2>Skills</h2>" in html


class TestLinksAndPictures:
    def test_a_link_keeps_its_destination(self):
        html = html_of(
            hyperlink("LinkedIn", "rId9"),
            relationships={"rId9": ("https://linkedin.com/in/jordan", True)},
        )

        assert 'href="https://linkedin.com/in/jordan"' in html
        assert 'rel="noopener noreferrer"' in html
        assert 'target="_blank"' in html

    def test_a_link_to_a_scheme_nobody_should_follow_becomes_plain_text(self):
        # A relationship target can name any scheme at all. Only the three that
        # mean "somewhere a reader might go" are linked; the text still shows.
        html = html_of(
            hyperlink("Click me", "rId9"),
            relationships={"rId9": ("javascript:alert(1)", True)},
        )

        assert "javascript:" not in html
        assert "Click me" in html

    def test_a_link_whose_relationship_is_missing_still_shows_its_text(self):
        html = html_of(hyperlink("Portfolio", "rId404"))

        assert "<a " not in html
        assert "Portfolio" in html

    def test_a_picture_is_inlined(self):
        # Inlined rather than served from an endpoint of its own: the frame is
        # fed a blob, and an <img src> cannot carry a bearer token.
        html = html_of(
            picture("rId5"),
            relationships={"rId5": ("media/photo.gif", False)},
            media={"photo.gif": TINY_GIF},
        )

        assert 'src="data:image/gif;base64,' in html
        assert "width:100px" in html

    def test_a_picture_too_big_to_inline_is_dropped_not_embedded(self, monkeypatch):
        # Base64 inflates by a third and the preview is held in memory twice
        # over. Past the budget it is a scanned page or a stock background, and
        # the text is what the user came to check.
        monkeypatch.setattr(document_preview, "_MAX_EMBEDDED_IMAGE_BYTES", 8)
        html = html_of(
            picture("rId5") + paragraph("Experience", style="Heading1"),
            relationships={"rId5": ("media/photo.gif", False)},
            media={"photo.gif": TINY_GIF},
        )

        assert "<img" not in html
        assert "<h2>Experience</h2>" in html

    def test_an_unrecognised_image_type_is_skipped(self):
        # The type goes into a data: URI, and a wrong one draws a broken-image
        # icon where the document has a photograph.
        html = html_of(
            picture("rId5"),
            relationships={"rId5": ("media/photo.emf", False)},
            media={"photo.emf": b"vector nonsense"},
        )

        assert "<img" not in html


class TestNothingFromTheDocumentBecomesMarkup:
    """The document is data. Every tag in the output is one we wrote.

    There is no sanitiser here to get wrong, which is the point: only the text
    nodes of the input survive, and they are escaped on the way through. These
    are the tests that would fail if someone ever "improved" that into a
    pass-through.
    """

    def test_angle_brackets_in_the_text_are_escaped(self):
        html = html_of(paragraph("Wrote &lt;script&gt;alert(1)&lt;/script&gt; parsers"))

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_ampersands_survive_as_ampersands(self):
        html = html_of(paragraph("Mergers &amp; Acquisitions"))

        assert "Mergers &amp; Acquisitions" in html

    def test_a_quote_in_a_link_cannot_close_the_attribute(self):
        # Written as an XML entity because that is the only way a quote reaches
        # us: Word escapes it in the relationships part, we unescape it parsing,
        # and it lands in an HTML attribute we build.
        html = html_of(
            hyperlink("Site", "rId9"),
            relationships={
                "rId9": ("https://x.test/&quot;onmouseover=&quot;alert(1)", True)
            },
        )

        assert 'onmouseover="alert(1)"' not in html
        assert "&quot;onmouseover=&quot;" in html

    def test_the_wrapper_forbids_scripts_and_network_access(self):
        # Belt-and-braces given the above, and free: this is a whole HTML
        # document served from the API's origin.
        preview = document_preview.preview_for("resume.docx", RESUME_DOCX, DOCX_MIME)

        assert b"default-src 'none'" in preview.content
        assert b'<meta charset="utf-8">' in preview.content


# --------------------------------------------------------------------------- #
# What a caller gets back                                                      #
# --------------------------------------------------------------------------- #


class TestWhichFilesNeedConverting:
    def test_the_formats_a_browser_will_not_draw(self):
        assert document_preview.needs_conversion("cv.docx")
        assert document_preview.needs_conversion("CV.DOC")
        assert document_preview.needs_conversion("rates.xlsx")
        assert document_preview.needs_conversion("deck.pptx")

    def test_the_formats_it_will(self):
        assert not document_preview.needs_conversion("cv.pdf")
        assert not document_preview.needs_conversion("headshot.png")
        assert not document_preview.needs_conversion("")


class TestPreviewFor:
    def test_a_pdf_comes_back_as_itself(self):
        # A preview endpoint that refused the formats needing no conversion
        # would be a trap for every caller.
        preview = document_preview.preview_for("cv.pdf", b"%PDF-1.4", "application/pdf")

        assert preview.content == b"%PDF-1.4"
        assert preview.media_type == "application/pdf"
        assert preview.filename == "cv.pdf"
        assert preview.converted is False

    def test_a_docx_comes_back_as_html_flagged_as_a_conversion(self):
        preview = document_preview.preview_for("jordan.docx", RESUME_DOCX, DOCX_MIME)

        assert preview.media_type == document_preview.HTML_MEDIA_TYPE
        assert preview.converted is True
        # The rendering carries its own extension: a save dialog offering
        # "jordan.docx" for a page of HTML would be a lie about which is which.
        assert preview.filename == "jordan.html"
        assert preview.source_filename == "jordan.docx"
        assert b"Jordan Candidate" in preview.content

    def test_a_file_named_docx_that_is_not_one_has_no_preview(self):
        # A made-up rendering of a file we couldn't read is worse than none: the
        # user is here to check *this* document.
        assert document_preview.preview_for("fake.docx", b"not a zip at all") is None

    def test_a_docx_with_nothing_readable_in_it_has_no_preview(self):
        assert document_preview.preview_for("blank.docx", build_docx("")) is None

    def test_a_zip_that_is_some_other_office_format_has_no_preview(self):
        assert document_preview.preview_for("export.docx", build_docx(omit_document=True)) is None

    def test_an_empty_file_has_no_preview(self):
        assert document_preview.preview_for("cv.docx", b"") is None

    def test_a_spreadsheet_has_no_preview(self):
        # Convertible in principle and not worth pretending to: nothing in this
        # product sends one, and the honest answer costs the user one download.
        assert document_preview.preview_for("rates.xlsx", b"PK\x03\x04") is None

    def test_a_broken_converter_reports_no_preview_rather_than_raising(self, monkeypatch):
        def _boom(_data):
            raise RuntimeError("converter exploded")

        monkeypatch.setattr(document_preview, "html_from_docx", _boom)

        assert document_preview.preview_for("cv.docx", RESUME_DOCX) is None


class TestLegacyWordDocuments:
    """.doc: a binary format with no relation to the zip above.

    Converted by LibreOffice when the box has one, and honestly refused when it
    doesn't. To a *PDF* rather than to LibreOffice's HTML, so the browser draws
    it and nothing this product didn't write reaches the page.
    """

    @pytest.fixture()
    def fake_soffice(self, tmp_path, monkeypatch):
        """A stand-in that honours the contract the real one is called with."""
        script = tmp_path / "soffice"
        script.write_text(
            "#!/bin/sh\n"
            # --outdir is the last flag before the source path; write the PDF
            # where a real conversion would put it.
            'for arg in "$@"; do case "$arg" in --outdir) next=1;; *)'
            ' if [ "$next" = 1 ]; then outdir="$arg"; next=0; fi;; esac; done\n'
            'printf "%%PDF-1.4 converted" > "$outdir/source.pdf"\n'
        )
        script.chmod(0o755)
        monkeypatch.setattr(document_preview, "soffice_path", lambda: str(script))
        return script

    def test_converted_to_a_pdf_when_libreoffice_is_there(self, fake_soffice):
        preview = document_preview.preview_for("old.doc", b"\xd0\xcf\x11\xe0 legacy")

        assert preview is not None
        assert preview.content.startswith(b"%PDF")
        assert preview.media_type == "application/pdf"
        assert preview.filename == "old.pdf"
        assert preview.converted is True
        assert preview.source_filename == "old.doc"

    def test_no_preview_when_the_box_has_no_libreoffice(self, monkeypatch):
        monkeypatch.setattr(document_preview, "soffice_path", lambda: None)

        assert document_preview.preview_for("old.doc", b"\xd0\xcf\x11\xe0") is None

    def test_a_conversion_that_writes_nothing_is_not_a_preview(self, tmp_path, monkeypatch):
        silent = tmp_path / "soffice"
        silent.write_text("#!/bin/sh\nexit 0\n")
        silent.chmod(0o755)
        monkeypatch.setattr(document_preview, "soffice_path", lambda: str(silent))

        assert document_preview.pdf_from_legacy_doc(b"\xd0\xcf\x11\xe0") is None

    def test_a_failing_conversion_is_reported_not_raised(self, tmp_path, monkeypatch):
        failing = tmp_path / "soffice"
        failing.write_text("#!/bin/sh\nexit 1\n")
        failing.chmod(0o755)
        monkeypatch.setattr(document_preview, "soffice_path", lambda: str(failing))

        assert document_preview.pdf_from_legacy_doc(b"\xd0\xcf\x11\xe0") is None

    def test_a_hanging_conversion_gives_up(self, monkeypatch):
        # LibreOffice is a whole office suite starting cold. It is also a
        # preview nobody will wait a minute for.
        def _hang(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd="soffice", timeout=1)

        monkeypatch.setattr(document_preview, "soffice_path", lambda: "/usr/bin/soffice")
        monkeypatch.setattr(subprocess, "run", _hang)

        assert document_preview.pdf_from_legacy_doc(b"\xd0\xcf\x11\xe0") is None

    def test_the_document_is_not_left_on_disk(self, fake_soffice, monkeypatch):
        # A resume is the most identifying document this product holds, and this
        # is the one code path that writes one to the filesystem.
        seen: list[str] = []
        real_run = subprocess.run

        def _watch(args, **kwargs):
            seen.append(args[-1])
            return real_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _watch)
        document_preview.pdf_from_legacy_doc(b"\xd0\xcf\x11\xe0")

        assert seen and not os.path.exists(seen[0])
        assert not os.path.exists(os.path.dirname(seen[0]))


class TestTheRenderingLooksLikeAPage:
    def test_it_is_a_whole_html_document(self):
        # Framed on its own, so it needs its own doctype, charset and styling —
        # and a light background, because a document is a white page and the
        # app's dark surface behind one reads as the rendering fault this
        # feature exists to stop producing.
        preview = document_preview.preview_for("cv.docx", RESUME_DOCX, DOCX_MIME)
        html = preview.content.decode("utf-8")

        assert html.startswith("<!doctype html>")
        assert "utf-8" in html
        assert "background: #fff" in html

    def test_typographic_punctuation_survives_the_round_trip(self):
        # A preview that renders a candidate's name with a mojibake apostrophe
        # looks like our bug, and the charset is the whole of the fix.
        preview = document_preview.preview_for(
            "cv.docx", build_docx(paragraph("Jordan O’Néill — Résumé")), DOCX_MIME
        )

        assert "Jordan O’Néill — Résumé" in preview.content.decode("utf-8")

    def test_the_title_is_the_document_not_the_extension(self):
        preview = document_preview.preview_for("jordan-cv.docx", RESUME_DOCX, DOCX_MIME)

        assert re.search(r"<title>jordan-cv</title>", preview.content.decode("utf-8"))
