"""Resume parsing: heuristics, LLM merge, .docx extraction, and the upload endpoint."""
from __future__ import annotations

import io
import zipfile

import pytest

from app.services import resume_parser
from app.services.resume_parser import (
    ParsedResume,
    extract_text_from_docx,
    extract_text_from_pdf,
    parse_heuristic,
    parse_resume,
)
from tests.conftest import SAMPLE_RESUME_TEXT

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

DEFAULT_PARAGRAPHS = [
    "Dana Rivera",
    "Staff Platform Engineer",
    "dana@example.com",
    "Experience",
    "Staff Platform Engineer, Globex 2018 - 2025",
    "Skills",
    "Kubernetes, Terraform, Go, AWS, Docker",
]


def _wrap(paragraphs: list[str], tag: str = "document") -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:{tag} {_W}><w:body>{body}</w:body></w:{tag}>"
    ).encode()


def docx_bytes(
    paragraphs: list[str] | None = None,
    *,
    header: list[str] | None = None,
    footer: list[str] | None = None,
    body_xml: bytes | None = None,
) -> bytes:
    """A real .docx: a zip whose ``word/document.xml`` is WordprocessingML.

    Built by hand rather than with python-docx, for the same reason the reader
    is: the format's text layer is four element names wide, and neither this
    suite nor the product should take a dependency to touch it.
    """
    if paragraphs is None:
        paragraphs = list(DEFAULT_PARAGRAPHS)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.'
            'org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml", body_xml if body_xml is not None else _wrap(paragraphs)
        )
        if header is not None:
            archive.writestr("word/header1.xml", _wrap(header, tag="hdr"))
        if footer is not None:
            archive.writestr("word/footer1.xml", _wrap(footer, tag="ftr"))
    return buffer.getvalue()


class TestDocxExtraction:
    """Reading a Word file, which is the format candidates actually hold."""

    def test_paragraphs_become_lines(self):
        text = extract_text_from_docx(docx_bytes())

        assert text.splitlines()[0] == "Dana Rivera"
        assert "Kubernetes, Terraform, Go, AWS, Docker" in text

    def test_the_line_structure_survives(self):
        """Every heuristic downstream reads line by line.

        The name is line one, the section headings are their own lines, and the
        role/company split is per line — collapse the paragraphs and the whole
        parse degrades to one unreadable string.
        """
        parsed = parse_heuristic(extract_text_from_docx(docx_bytes()))

        assert parsed.full_name == "Dana Rivera"
        assert parsed.headline == "Staff Platform Engineer"
        assert "kubernetes" in parsed.skills

    def test_runs_inside_a_paragraph_are_joined(self):
        """Word splits a line into runs at every formatting change.

        A bolded name or a spell-check mark is enough to break "Dana Rivera"
        into two `w:t` elements, and a reader that emitted one line per run
        would turn every styled resume into gibberish.
        """
        body = (
            f'<?xml version="1.0"?><w:document {_W}><w:body>'
            "<w:p><w:r><w:t>Dana </w:t></w:r><w:r><w:t>Rivera</w:t></w:r></w:p>"
            "</w:body></w:document>"
        ).encode()

        assert extract_text_from_docx(docx_bytes(body_xml=body)) == "Dana Rivera"

    def test_tabs_and_breaks_are_the_whitespace_the_author_typed(self):
        body = (
            f'<?xml version="1.0"?><w:document {_W}><w:body>'
            "<w:p><w:r><w:t>Acme</w:t><w:tab/><w:t>2019</w:t></w:r></w:p>"
            "</w:body></w:document>"
        ).encode()

        assert "Acme 2019" in extract_text_from_docx(docx_bytes(body_xml=body))

    def test_a_header_is_read_before_the_body(self):
        """Resumes routinely put the name and contact details in a Word header.

        The heuristics read the name off line one, so appending the header would
        file it as a stray line halfway down the document.
        """
        text = extract_text_from_docx(
            docx_bytes(
                ["Experience", "Staff Platform Engineer, Globex"],
                header=["Dana Rivera", "dana@example.com"],
            )
        )

        assert text.splitlines()[0] == "Dana Rivera"
        assert parse_heuristic(text).full_name == "Dana Rivera"

    def test_a_footer_comes_last(self):
        text = extract_text_from_docx(
            docx_bytes(["Dana Rivera"], footer=["Page 1 of 2"])
        )

        assert text.splitlines()[-1] == "Page 1 of 2"

    def test_empty_paragraphs_are_dropped(self):
        text = extract_text_from_docx(docx_bytes(["Dana Rivera", "", "  ", "Skills"]))

        assert text.splitlines() == ["Dana Rivera", "Skills"]

    def test_a_non_zip_raises_a_readable_error(self):
        with pytest.raises(ValueError, match="not a Word document"):
            extract_text_from_docx(b"this is not a zip")

    def test_a_zip_that_is_not_a_docx_raises(self):
        """A .pages or .odt export, or a .doc someone renamed."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("index.xml", "<x/>")

        with pytest.raises(ValueError, match="not a Word document"):
            extract_text_from_docx(buffer.getvalue())

    def test_one_unreadable_header_does_not_cost_the_resume(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", _wrap(["Dana Rivera"]))
            archive.writestr("word/header1.xml", b"<not valid xml")

        assert "Dana Rivera" in extract_text_from_docx(buffer.getvalue())


# A PDF has no lines — it has text objects at coordinates, and pypdf emits a
# newline between any two it can't prove share a run. A layout that positions each
# word separately (justified text, tracked-out headings, most Google Docs exports)
# therefore extracts one word per line. This is the text a real five-page CV
# produced, and against it every line-based heuristic returned nothing: no jobs, no
# degrees, no name, and a section slicer that locked onto the lowercase word
# "experience" in the middle of the summary paragraph.
SHATTERED_PDF_TEXT = """\
SUBHENDU DAS +1-365-275-4408 | dev@example.com | Toronto, Canada LinkedIn: \
linkedin.com/in/subhendu | GitHub: github.com/r2st
EXECUTIVE SUMMARY Senior AI engineering leader with 22 years of software
architecture

experience,

specializing

in

Conversational

AI

platforms

at

enterprise

scale.

Proven

track

record

architecting

multi-channel

platforms

in

production

environments.

TECHNICAL SKILLS Languages Python, TypeScript, Rust; FastAPI, React, Kubernetes, \
PostgreSQL, Terraform
PROFESSIONAL EXPERIENCE
SOFTWARE ARCHITECT | Digital Wave Technology | USASep 2024 – Sep 2025
• Built a channel-agnostic message normalization layer that converts inbound
messages

into

a

unified

envelope

CTO & FOUNDER | OneNet Inc | CanadaJul 2018 – Sep 2024
• Ran engineering across a federated query engine over Postgres and ClickHouse
LEAD DEVELOPER | Aztecsoft | IndiaJun 2006 – May 2007
EDUCATION B.Sc. (Physics, Chemistry & Mathematics) | Gauhati University | India | \
1999 – 2002
OPEN SOURCE & PROJECTS • Knol (github.com/aiknol/knol) — long-term memory for \
LLM agents
"""


def shattered_pdf_bytes(lines: list[str] | None = None) -> bytes:
    """A real PDF whose words are each their own text object.

    Built by drawing word by word, which is what produces the extraction above —
    reproducing it from the writing end rather than pasting a fixture keeps the
    test honest about *why* the text arrives shattered.
    """
    reportlab_canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    if lines is None:
        lines = SHATTERED_PDF_TEXT.splitlines()

    buffer = io.BytesIO()
    pdf = reportlab_canvas.Canvas(buffer)
    y = 780
    for line in lines:
        if not line.strip():
            continue
        x = 40
        for word in line.split():
            pdf.drawString(x, y, word)
            x += 6 * len(word) + 8
            if x > 520:
                x, y = 40, y - 12
        y -= 12
        if y < 40:
            pdf.showPage()
            y = 780
    pdf.save()
    return buffer.getvalue()


class TestShatteredPdfText:
    """A PDF that extracts one word per line — the commonest way a parse silently
    loses four fifths of a resume.

    The symptom is not an error. Upload succeeds, the profile comes back with a
    name and a skills list, and the work history is simply absent — so nothing
    downstream has any way to know the resume was only a quarter read.
    """

    def test_the_damage_is_recognised_and_repaired(self):
        text = resume_parser._reflow_shattered_lines(SHATTERED_PDF_TEXT)

        assert "specializing in Conversational AI platforms" in text
        # The whole page must not collapse into one line either — headings and
        # entries are still sliced apart by line.
        assert "PROFESSIONAL EXPERIENCE" in text.splitlines()

    def test_clean_extraction_is_left_alone(self):
        """A PDF whose line structure survived must come back byte-for-byte.

        The repair is a repair, not a normalisation: applying it to text that
        already reads correctly is how one-skill-per-line resumes get their
        headings glued to their content.
        """
        assert resume_parser._reflow_shattered_lines(SAMPLE_RESUME_TEXT) == (
            SAMPLE_RESUME_TEXT
        )

    def test_a_heading_is_never_absorbed_into_the_paragraph_above_it(self):
        text = resume_parser._reflow_shattered_lines(SHATTERED_PDF_TEXT)

        assert resume_parser._section(text, "experience")
        assert "SOFTWARE ARCHITECT" in resume_parser._section(text, "experience")

    def test_a_short_document_is_not_mistaken_for_a_shattered_one(self):
        """Line-per-item content is legitimate; it just has to be long enough to
        distinguish from damage, and a handful of lines never is."""
        text = "Dana Rivera\nSkills\nPython\nRust\nGo\n"

        assert resume_parser._reflow_shattered_lines(text) == text

    def test_every_job_survives_the_round_trip_through_a_real_pdf(self):
        parsed = parse_heuristic(extract_text_from_pdf(shattered_pdf_bytes()))

        titles = [entry["title"] for entry in parsed.experience]
        assert titles == ["Software Architect", "CTO & Founder", "Lead Developer"]
        assert [entry["company"] for entry in parsed.experience] == [
            "Digital Wave Technology",
            "OneNet Inc",
            "Aztecsoft",
        ]

    def test_the_name_survives_a_header_packed_onto_one_line(self):
        """Contact details share the name's line, and used to disqualify it.

        The line holds a phone number, so a whole-line test rejected it for
        containing digits and the profile came back with no name at all — or, worse,
        with "Professional Experience", the next thing on the page that looks like
        two capitalised words.
        """
        parsed = parse_heuristic(SHATTERED_PDF_TEXT)

        assert parsed.full_name == "Subhendu Das"

    def test_education_is_found_when_its_heading_shares_a_line_with_its_body(self):
        parsed = parse_heuristic(SHATTERED_PDF_TEXT)

        assert len(parsed.education) == 1
        assert parsed.education[0]["year"] == "1999"
        assert "Gauhati University" in parsed.education[0]["school"]

    def test_skills_are_found_when_the_heading_shares_a_line_with_its_body(self):
        assert {"python", "typescript", "rust", "kubernetes"} <= set(
            parse_heuristic(SHATTERED_PDF_TEXT).skills
        )

    def test_a_body_word_is_not_read_as_a_section_heading(self):
        """"experience" and "profile" appear in every summary paragraph.

        A shattered extraction leaves them alone on a line, where they are
        indistinguishable from a heading unless case is taken into account — and
        slicing the experience section from the middle of the summary is how a
        nine-job history came back empty.
        """
        text = "Summary\nSenior engineer with deep\nexperience\nin payments.\n"

        assert resume_parser._section(text, "experience") == ""

    def test_a_sentence_opening_with_a_heading_word_is_not_a_heading(self):
        text = "Summary\nExperience building payment systems at scale.\nSkills\nRust\n"

        assert resume_parser._section(text, "experience") == ""
        assert resume_parser._section(text, "skills") == "Rust"

    def test_a_lowercase_heading_still_counts_when_punctuated(self):
        """Plain-text resumes write "skills:" and mean it."""
        assert resume_parser._section("skills:\nRust, Go\n", "skills") == "Rust, Go"

    def test_target_roles_come_back_from_the_recovered_history(self):
        """The roles the outreach engine pitches are derived from job titles.

        With no experience entries there were none, and the campaign had nothing to
        search for — which is the real cost of a silently trimmed parse.
        """
        assert parse_heuristic(SHATTERED_PDF_TEXT).target_roles[:2] == [
            "Software Architect",
            "CTO & Founder",
        ]

    def test_a_title_set_in_caps_for_layout_is_not_pitched_in_caps(self):
        """These titles reach a recruiter's inbox, so "SOFTWARE ARCHITECT" shouts.

        Acronyms have to survive it, though — `.title()` alone would send "Cto".
        """
        entries = resume_parser.extract_experience(
            "Experience\nCTO & FOUNDER | OneNet Inc 2018 - 2024\n"
            "PRINCIPAL AI ENGINEER | Acme 2016 - 2018\n"
        )

        assert [e["title"] for e in entries] == [
            "CTO & Founder",
            "Principal AI Engineer",
        ]

    def test_a_title_already_in_mixed_case_is_untouched(self):
        [entry] = resume_parser.extract_experience(
            "Experience\nStaff Backend Engineer, Northwind Payments 2021 - Present\n"
        )

        assert entry["title"] == "Staff Backend Engineer"
        assert entry["company"] == "Northwind Payments"


class TestSectionBoundaries:
    def test_a_named_but_unextracted_heading_still_ends_the_section_above_it(self):
        """"Projects" has no field, so it used to be read as more experience.

        A heading absent from the table is not merely unread — it is invisible, and
        everything under it is filed under whatever section came before.
        """
        text = (
            "Experience\nEngineer at Acme 2019 - 2021\n"
            "PROJECTS • Knol — long-term memory for agents 2022 - 2023\n"
        )

        assert "Knol" not in resume_parser._section(text, "experience")
        assert [e["company"] for e in resume_parser.extract_experience(text)] == ["Acme"]


class TestDateRanges:
    """A job entry is recognised only by its dates, so an unreadable date range is
    an unread job."""

    @pytest.mark.parametrize(
        "line, start, end",
        [
            ("Engineer at Acme 2019 - 2021", "2019", "2021"),
            ("Engineer at Acme Jan 2019 – Mar 2021", "2019", "2021"),
            ("Engineer at Acme May 2004 – Apr 2006", "2004", "2006"),
            ("Engineer at Acme Sept. 2019 to Present", "2019", "Present"),
            ("Engineer at Acme 2019 — Ongoing", "2019", "Ongoing"),
        ],
    )
    def test_month_qualified_ranges_are_read(self, line, start, end):
        [entry] = resume_parser.extract_experience(f"Experience\n{line}\n")

        assert (entry["start"], entry["end"]) == (start, end)
        assert entry["company"] == "Acme"

    def test_a_month_glued_to_the_word_before_it_is_not_read_as_a_month(self):
        """"| RemoteSep 2025" is a location and a start month with no space between.

        Matching "Sep" inside "RemoteSep" would put the location into the company
        field; the year is the part that can be trusted.
        """
        [entry] = resume_parser.extract_experience(
            "Experience\nARCHITECT | Acme | RemoteSep 2025 – Present\n"
        )

        assert entry["start"] == "2025"
        assert entry["company"] == "Acme"

    def test_several_entries_on_one_line_are_all_read(self):
        """A PDF glues an entry onto the end of the bullet above it.

        Reading one entry per line meant only the first of them ever landed.
        """
        entries = resume_parser.extract_experience(
            "Experience\nCTO | Acme | US 2019 - 2021 LEAD DEVELOPER | Initech 2016 - 2019\n"
        )

        assert [(e["title"], e["company"]) for e in entries] == [
            ("CTO", "Acme"),
            ("Lead Developer", "Initech"),
        ]

    def test_a_long_entry_is_no_longer_dropped_for_its_length(self):
        """Entries over a length cap were skipped, and a glued line is always long."""
        prose = "• " + "shipped a great many production services for enterprise " * 3
        entries = resume_parser.extract_experience(
            f"Experience\n{prose} CTO & FOUNDER | OneNet Inc | Canada Jul 2018 - 2024\n"
        )

        assert [(e["title"], e["company"]) for e in entries] == [
            ("CTO & Founder", "OneNet Inc")
        ]

    def test_a_location_does_not_end_up_in_the_company_field(self):
        [entry] = resume_parser.extract_experience(
            "Experience\nCTO | OneNet Inc | Canada 2018 - 2024\n"
        )

        assert entry["company"] == "OneNet Inc"


class TestLinks:
    def test_a_bare_profile_url_is_captured(self):
        """Candidates write "linkedin.com/in/name", not the https:// in front of it.

        Requiring the scheme meant the two links a recruiter most wants were the two
        that were never captured.
        """
        links = resume_parser.extract_links(
            "LinkedIn: linkedin.com/in/subhendu | GitHub: github.com/r2st"
        )

        assert links == ["linkedin.com/in/subhendu", "github.com/r2st"]

    def test_a_domain_mentioned_in_prose_is_not_a_link(self):
        assert resume_parser.extract_links("Migrated the platform off heroku.com.") == []

    def test_a_full_url_still_wins_intact(self):
        assert resume_parser.extract_links("https://github.com/jordanc/x") == [
            "https://github.com/jordanc/x"
        ]


class TestLlmInputBudget:
    """What the model is shown of a long resume.

    The budget was 6k characters on the belief that it "covers a long resume". A
    five-page CV extracts to around 20k, so the cut landed in the middle of the
    second job — and since the merge only overwrites fields the model answered,
    the result was a profile confidently listing two jobs and no degree.
    """

    def test_a_normal_resume_is_sent_whole(self):
        assert resume_parser._llm_input(SAMPLE_RESUME_TEXT) == SAMPLE_RESUME_TEXT

    def test_an_over_long_resume_keeps_its_tail_sections(self):
        filler = "\n".join(f"• shipped service number {n}" for n in range(4000))
        text = (
            "Jordan Candidate\nSenior Backend Engineer\n"
            "Experience\n"
            f"Staff Engineer at Northwind 2021 - Present\n{filler}\n"
            "Education\nB.S. Computer Science, State University 2016\n"
        )
        assert len(text) > resume_parser._LLM_INPUT_CHARS

        sent = resume_parser._llm_input(text)

        assert len(sent) <= resume_parser._LLM_INPUT_CHARS
        assert "Jordan Candidate" in sent
        # The whole point: the sections at the end of the document survive.
        assert "State University" in sent

    def test_the_budget_is_respected_exactly(self):
        assert len(resume_parser._llm_input("x" * 50_000, limit=1_000)) == 1_000

    def test_the_model_sees_the_late_sections_of_a_real_shattered_pdf(self, monkeypatch):
        """End to end: what actually reached the model for the resume that broke."""
        seen: dict[str, str] = {}

        def _capture(messages, **kwargs):
            seen["prompt"] = messages[-1]["content"]
            return "{}"

        monkeypatch.setattr(resume_parser.settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(resume_parser, "chat_completion", _capture)

        parse_resume(extract_text_from_pdf(shattered_pdf_bytes()))

        assert "Gauhati University" in seen["prompt"]
        assert "LEAD DEVELOPER" in seen["prompt"]


class TestHeuristics:
    def test_extracts_identity_from_header(self):
        parsed = parse_heuristic(SAMPLE_RESUME_TEXT)
        assert parsed.full_name == "Jordan Candidate"
        assert parsed.email == "jordan.candidate@example.com"
        assert parsed.location == "San Francisco, CA"
        assert "github.com/jordanc" in parsed.links[0]

    def test_phone_is_found_and_years_are_not_mistaken_for_one(self):
        parsed = parse_heuristic(SAMPLE_RESUME_TEXT)
        assert parsed.phone is not None
        assert "555" in parsed.phone

    def test_headline_and_seniority(self):
        parsed = parse_heuristic(SAMPLE_RESUME_TEXT)
        assert parsed.headline == "Senior Backend Engineer"
        # "Staff Backend Engineer" appears in the header block -> lead.
        assert parsed.seniority in {"senior", "lead"}

    def test_stated_years_of_experience_wins(self):
        parsed = parse_heuristic(SAMPLE_RESUME_TEXT)
        assert parsed.years_experience == 8

    def test_years_inferred_from_date_ranges_when_not_stated(self):
        text = "Experience\nEngineer at Acme 2015 - 2021\n"
        assert parse_heuristic(text).years_experience == 6

    def test_skills_come_from_the_taxonomy(self):
        skills = parse_heuristic(SAMPLE_RESUME_TEXT).skills
        assert {"python", "fastapi", "postgresql", "kubernetes", "aws"} <= set(skills)

    def test_go_does_not_match_inside_another_word(self):
        assert "go" not in parse_heuristic("I worked at Google on algorithms.").skills

    def test_experience_entries_split_title_from_company(self):
        entries = parse_heuristic(SAMPLE_RESUME_TEXT).experience
        assert len(entries) == 3
        assert entries[0] == {
            "title": "Staff Backend Engineer",
            "company": "Northwind Payments",
            "start": "2021",
            "end": "Present",
        }
        assert entries[1]["company"] == "Acme Corp"

    def test_education_is_captured(self):
        education = parse_heuristic(SAMPLE_RESUME_TEXT).education
        assert education and education[0]["year"] == "2016"

    def test_target_roles_derive_from_headline_and_history(self):
        roles = parse_heuristic(SAMPLE_RESUME_TEXT).target_roles
        assert roles[0] == "Senior Backend Engineer"
        assert len(roles) <= 3

    def test_summary_section_is_sliced_out(self):
        summary = parse_heuristic(SAMPLE_RESUME_TEXT).summary
        assert summary is not None
        assert summary.startswith("Backend engineer with 8 years")
        # The next section must not bleed in.
        assert "Staff Backend Engineer" not in summary

    def test_empty_input_yields_empty_fields_rather_than_raising(self):
        parsed = parse_heuristic("")
        assert parsed.full_name is None
        assert parsed.skills == []


class TestLlmEnrichment:
    def test_no_api_key_means_heuristics_only(self):
        parsed = parse_resume(SAMPLE_RESUME_TEXT)
        assert parsed.parsed_with == "heuristic"

    def test_valid_json_overlays_heuristic_fields(self, monkeypatch):
        payload = (
            '{"headline": "Principal Platform Engineer", "seniority": "lead", '
            '"years_experience": 9, "skills": ["Rust"], '
            '"target_roles": ["Principal Engineer"], '
            '"target_industries": ["fintech"]}'
        )
        monkeypatch.setattr(resume_parser.settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(resume_parser, "chat_completion", lambda *a, **k: payload)

        parsed = parse_resume(SAMPLE_RESUME_TEXT)
        assert parsed.parsed_with == "llm"
        assert parsed.headline == "Principal Platform Engineer"
        assert parsed.seniority == "lead"
        assert parsed.years_experience == 9
        assert parsed.target_industries == ["fintech"]
        # Heuristic skills survive; the model's are unioned in.
        assert "python" in parsed.skills and "rust" in parsed.skills
        # Contact details stay with the regex, which does not typo them.
        assert parsed.email == "jordan.candidate@example.com"

    def test_fenced_json_is_unwrapped(self, monkeypatch):
        monkeypatch.setattr(resume_parser.settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(
            resume_parser,
            "chat_completion",
            lambda *a, **k: '```json\n{"headline": "Staff Engineer"}\n```',
        )
        assert parse_resume(SAMPLE_RESUME_TEXT).headline == "Staff Engineer"

    @pytest.mark.parametrize(
        "response", ["not json at all", "", '{"seniority": "wizard"}', "[1, 2, 3]"]
    )
    def test_bad_output_degrades_to_heuristics(self, monkeypatch, response):
        monkeypatch.setattr(resume_parser.settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(resume_parser, "chat_completion", lambda *a, **k: response)

        parsed = parse_resume(SAMPLE_RESUME_TEXT)
        assert parsed.headline == "Senior Backend Engineer"
        assert parsed.seniority != "wizard"

    def test_api_failure_never_blocks_a_parse(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise resume_parser.OpenRouterError("upstream is down")

        monkeypatch.setattr(resume_parser.settings, "openrouter_api_key", "test-key")
        monkeypatch.setattr(resume_parser, "chat_completion", _boom)

        parsed = parse_resume(SAMPLE_RESUME_TEXT)
        assert parsed.parsed_with == "heuristic"
        assert parsed.full_name == "Jordan Candidate"


class TestUploadEndpoint:
    def test_upload_parses_and_stores_a_profile(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("jordan.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 201, resp.text
        [body] = resp.json()
        assert body["full_name"] == "Jordan Candidate"
        assert body["years_experience"] == 8
        assert body["is_default"] is True
        assert "python" in body["skills"]

    def test_multiple_resumes_upload_together_one_default(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        resp = auth_client.post(
            "/api/v1/resumes",
            files=[
                ("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf")),
                ("files", ("b.pdf", b"%PDF-1.4 b", "application/pdf")),
            ],
        )
        assert resp.status_code == 201, resp.text
        rows = resp.json()
        assert len(rows) == 2
        assert [r["is_default"] for r in rows] == [True, False]

    def test_an_unreadable_format_is_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("resume.txt", b"hello", "text/plain")},
        )
        assert resp.status_code == 415
        assert ".docx" in resp.json()["detail"]

    def test_a_docx_is_accepted_and_parsed(self, auth_client):
        """The file most candidates actually have.

        A resume lives in Word until the moment it is sent, so PDF-only refused
        the commonest upload there is and sent the user off to export a PDF
        before the product would talk to them.
        """
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("dana.docx", docx_bytes(), DOCX_MIME)},
        )
        assert resp.status_code == 201, resp.text
        [body] = resp.json()
        assert body["filename"] == "dana.docx"
        assert body["full_name"] == "Dana Rivera"
        assert body["headline"] == "Staff Platform Engineer"
        assert "kubernetes" in body["skills"]

    def test_the_browsers_content_type_does_not_decide(self, auth_client):
        """Chrome, Safari and a drag-and-drop disagree about what a .docx is.

        The extension is the one thing the user can see and we can trust, so a
        .docx arriving as `application/octet-stream` is still a .docx.
        """
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("dana.docx", docx_bytes(), "application/octet-stream")},
        )
        assert resp.status_code == 201, resp.text

    def test_a_legacy_doc_is_told_how_to_fix_itself(self, auth_client):
        """"Must be .pdf or .docx" is no help to someone holding a .doc."""
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("old.doc", b"\xd0\xcf\x11\xe0", "application/msword")},
        )
        assert resp.status_code == 415
        assert "Save As" in resp.json()["detail"]

    def test_a_docx_that_is_not_a_docx_gets_a_clean_422(self, auth_client):
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("broken.docx", b"not a zip at all", DOCX_MIME)},
        )
        assert resp.status_code == 422
        assert "not a Word document" in resp.json()["detail"]

    def test_an_empty_docx_is_rejected_like_a_scanned_pdf(self, auth_client):
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("blank.docx", docx_bytes(paragraphs=[]), DOCX_MIME)},
        )
        assert resp.status_code == 422
        assert "no text found" in resp.json()["detail"]

    def test_pdf_and_docx_upload_together(self, auth_client, monkeypatch):
        """One drop, mixed formats — each read by the right extractor."""
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        resp = auth_client.post(
            "/api/v1/resumes",
            files=[
                ("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf")),
                ("files", ("b.docx", docx_bytes(), DOCX_MIME)),
            ],
        )
        assert resp.status_code == 201, resp.text
        rows = resp.json()
        assert [r["full_name"] for r in rows] == ["Jordan Candidate", "Dana Rivera"]
        # Still exactly one default across a mixed upload.
        assert [r["is_default"] for r in rows] == [True, False]

    def test_text_free_pdf_is_rejected_with_a_useful_message(
        self, auth_client, monkeypatch
    ):
        monkeypatch.setattr("app.routers.resumes.extract_text_from_pdf", lambda _: "  ")
        resp = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("scan.pdf", b"%PDF-1.4", "application/pdf")},
        )
        assert resp.status_code == 422
        assert "scanned" in resp.json()["detail"].lower()

    def test_upload_requires_auth(self, client):
        resp = client.post(
            "/api/v1/resumes",
            files={"files": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        )
        assert resp.status_code == 401

    def test_patch_can_promote_a_different_default(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        created = auth_client.post(
            "/api/v1/resumes",
            files=[
                ("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf")),
                ("files", ("b.pdf", b"%PDF-1.4 b", "application/pdf")),
            ],
        ).json()
        second = created[1]["id"]

        resp = auth_client.patch(f"/api/v1/resumes/{second}", json={"is_default": True})
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True

        defaults = [r for r in auth_client.get("/api/v1/resumes").json() if r["is_default"]]
        assert [r["id"] for r in defaults] == [second]

    def test_deleting_the_default_promotes_another(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        created = auth_client.post(
            "/api/v1/resumes",
            files=[
                ("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf")),
                ("files", ("b.pdf", b"%PDF-1.4 b", "application/pdf")),
            ],
        ).json()

        assert auth_client.delete(f"/api/v1/resumes/{created[0]['id']}").status_code == 204
        remaining = auth_client.get("/api/v1/resumes").json()
        assert len(remaining) == 1 and remaining[0]["is_default"] is True


class TestResumeLifecycleWithDependents:
    """Promoting and deleting a resume that the rest of the product points at.

    The existing coverage exercised bare resumes, uploaded and immediately acted
    on. Every real resume is referenced — by a profile, a campaign, a tailoring
    run, a fit score, a cover letter — and those references are the whole risk in
    a delete. The suite also runs with SQLite foreign keys **on** (see
    ``conftest``), so a constraint production would enforce fails here too rather
    than passing quietly all the way to Postgres.
    """

    @pytest.fixture()
    def two_resumes(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        return auth_client.post(
            "/api/v1/resumes",
            files=[
                ("files", ("a.pdf", b"%PDF-1.4 a", "application/pdf")),
                ("files", ("b.pdf", b"%PDF-1.4 b", "application/pdf")),
            ],
        ).json()

    def _wire_dependents(self, db, user_id: int, resume_id: int) -> None:
        from app.models.campaign import Campaign
        from app.models.cover_letter import CoverLetter
        from app.models.fit_score import FitScore
        from app.models.job import JobPosting, job_fingerprint
        from app.models.profile import Profile
        from app.models.tailored_resume import TailoredResume

        posting = JobPosting(
            user_id=user_id, title="Eng", company="Acme", url="http://x/1",
            fingerprint=job_fingerprint("Eng", "Acme", None),
        )
        db.add(posting)
        db.flush()
        db.add(Profile(user_id=user_id, resume_id=resume_id, name="Backend"))
        db.add(Campaign(user_id=user_id, name="C", resume_id=resume_id))
        db.add(TailoredResume(
            user_id=user_id, resume_id=resume_id, job_posting_id=posting.id
        ))
        db.add(FitScore(
            user_id=user_id, resume_id=resume_id, job_posting_id=posting.id,
            jd_hash="h", overall=50,
        ))
        db.add(CoverLetter(
            user_id=user_id, resume_id=resume_id, job_posting_id=posting.id, body="hi",
        ))
        db.commit()

    def test_a_referenced_resume_can_still_be_deleted(
        self, auth_client, db_session, current_user, two_resumes
    ):
        self._wire_dependents(db_session, current_user.id, two_resumes[0]["id"])

        resp = auth_client.delete(f"/api/v1/resumes/{two_resumes[0]['id']}")

        assert resp.status_code == 204, resp.text
        assert [r["id"] for r in auth_client.get("/api/v1/resumes").json()] == [
            two_resumes[1]["id"]
        ]

    def test_deleting_a_resume_leaves_its_profile_working(
        self, auth_client, db_session, current_user, two_resumes
    ):
        """A profile outlives the document it named.

        ``profile_service.resume_for`` falls back to the user's default, so
        losing a file costs the candidate a file — never an intent they still
        hold. This pins that the delete relies on it rather than orphaning them.
        """
        from app.models.profile import Profile
        from app.services import profile_service

        self._wire_dependents(db_session, current_user.id, two_resumes[0]["id"])

        auth_client.delete(f"/api/v1/resumes/{two_resumes[0]['id']}")
        db_session.expire_all()

        profile = db_session.query(Profile).filter_by(name="Backend").one()
        assert profile.resume_id is None
        resolved = profile_service.resume_for(db_session, current_user, profile)
        assert resolved is not None and resolved.id == two_resumes[1]["id"]

    def test_deleting_the_last_resume_is_allowed(
        self, auth_client, db_session, current_user, monkeypatch
    ):
        """Nothing left to promote is a valid state, not an error."""
        monkeypatch.setattr(
            "app.routers.resumes.extract_text_from_pdf", lambda _: SAMPLE_RESUME_TEXT
        )
        [only] = auth_client.post(
            "/api/v1/resumes",
            files={"files": ("a.pdf", b"%PDF-1.4 a", "application/pdf")},
        ).json()
        self._wire_dependents(db_session, current_user.id, only["id"])

        assert auth_client.delete(f"/api/v1/resumes/{only['id']}").status_code == 204
        assert auth_client.get("/api/v1/resumes").json() == []

    def test_promoting_a_referenced_resume_moves_exactly_one_default(
        self, auth_client, db_session, current_user, two_resumes
    ):
        self._wire_dependents(db_session, current_user.id, two_resumes[0]["id"])

        resp = auth_client.patch(
            f"/api/v1/resumes/{two_resumes[1]['id']}", json={"is_default": True}
        )

        assert resp.status_code == 200, resp.text
        rows = auth_client.get("/api/v1/resumes").json()
        assert [r["id"] for r in rows if r["is_default"]] == [two_resumes[1]["id"]]

    def test_promoting_the_resume_that_is_already_default_is_a_no_op(
        self, auth_client, two_resumes
    ):
        """Double-clicking must not end with nothing marked default."""
        first = two_resumes[0]["id"]

        assert auth_client.patch(
            f"/api/v1/resumes/{first}", json={"is_default": True}
        ).status_code == 200
        rows = auth_client.get("/api/v1/resumes").json()
        assert [r["id"] for r in rows if r["is_default"]] == [first]

    def test_another_users_resume_cannot_be_promoted_or_deleted(
        self, auth_client, db_session, two_resumes
    ):
        from app.models.resume import Resume
        from app.models.user import User

        stranger = User(email="stranger@example.com", hashed_password="x")
        db_session.add(stranger)
        db_session.flush()
        theirs = Resume(user_id=stranger.id, filename="theirs.pdf", is_default=True)
        db_session.add(theirs)
        db_session.commit()

        assert auth_client.patch(
            f"/api/v1/resumes/{theirs.id}", json={"is_default": True}
        ).status_code == 404
        assert auth_client.delete(f"/api/v1/resumes/{theirs.id}").status_code == 404
        # And it is untouched.
        db_session.refresh(theirs)
        assert theirs.is_default is True


def test_parsed_resume_round_trips_to_a_dict():
    parsed = ParsedResume(full_name="A B", skills=["python"])
    assert parsed.as_dict()["skills"] == ["python"]
