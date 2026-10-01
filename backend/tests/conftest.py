"""Pytest fixtures: in-memory SQLite DB + FastAPI test client with overrides.

No external services (Postgres, Redis, Gmail, OpenRouter, the open web) are
touched. AI calls degrade to deterministic fallbacks because no OpenRouter key is
set, and the scraper is never invoked without an explicit monkeypatch.
"""
from __future__ import annotations

import os
import tempfile

# Ensure a clean, keyless config *before* app modules import settings.
os.environ.setdefault("OPENROUTER_API_KEY", "")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-tests-only")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
# Run campaign work inline instead of dispatching to a worker — no broker exists
# in the test environment, and the pipeline is what we want to assert on.
os.environ.setdefault("CELERY_ENABLED", "false")
# A fixed Fernet key so OAuth-token encryption is exercised rather than skipped.
os.environ.setdefault(
    "TOKEN_ENCRYPTION_KEY", "cMHmDpN1Q0Rl7CQKvVGZBqvS_qtfPQrEbQTLLfnJ6Ac="
)
# The form-apply agent writes screenshots and resume files; keep both out of the
# working tree, and off by default so a test only pays for them when it asks.
os.environ.setdefault("FORM_APPLY_SCREENSHOTS", "false")
# The company-board sweep reaches five third-party JSON APIs. It is on in
# production and off here, so the suite stays offline; test_ats_boards.py turns it
# on with a stubbed session, which is the only place it should ever be on.
os.environ.setdefault("ATS_BOARD_DISCOVERY_ENABLED", "false")
os.environ.setdefault(
    "FORM_APPLY_ARTIFACT_DIR", tempfile.mkdtemp(prefix="talentping-artifacts-")
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.main import app  # importing main registers every model on Base.metadata
from app.models.gmail_account import GmailAccount
from app.models.resume import Resume
from app.models.user import User
from app.services import crypto

# A realistic resume body, shared by the parser and upload tests.
SAMPLE_RESUME_TEXT = """\
Jordan Candidate
Senior Backend Engineer
jordan.candidate@example.com | +1 (415) 555-0142 | San Francisco, CA
https://github.com/jordanc

Summary
Backend engineer with 8 years of experience building distributed systems and
payment infrastructure at scale.

Experience
Staff Backend Engineer, Northwind Payments 2021 - Present
Senior Backend Engineer at Acme Corp 2018 - 2021
Backend Engineer | Initech 2016 - 2018

Education
B.S. Computer Science, State University 2016

Skills
Python, FastAPI, PostgreSQL, Kubernetes, AWS, Kafka, Terraform
"""


@pytest.fixture()
def db_session():
    # A single shared in-memory DB for the test (StaticPool keeps one connection).
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_client(client):
    """A client already registered + logged in, with the Authorization header set."""
    email = "candidate@example.com"
    password = "supersecret123"
    client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Jordan Candidate"},
    )
    resp = client.post(
        "/api/v1/auth/login", data={"username": email, "password": password}
    )
    token = resp.json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture()
def current_user(db_session, auth_client) -> User:
    return db_session.query(User).filter_by(email="candidate@example.com").one()


@pytest.fixture()
def connected_gmail(db_session, current_user) -> GmailAccount:
    """A user with a (fake but well-formed) connected Gmail account."""
    account = GmailAccount(
        user_id=current_user.id,
        email="candidate@gmail.com",
        google_sub="test-sub-1",
        display_name="Jordan Candidate",
        refresh_token_encrypted=crypto.encrypt("fake-refresh-token"),
        scopes="openid https://www.googleapis.com/auth/gmail.send",
        status="connected",
        is_primary=True,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(current_user)
    return account


@pytest.fixture()
def stub_gmail(monkeypatch):
    """Drive the inbound scanner from an in-memory mailbox.

    Returns a dict the test fills with ``{message_id: message}``; ``list`` and
    ``get`` both read it, so a test controls exactly what the mailbox contains.

    Shared rather than per-file: every suite that exercises inbound recruiter
    mail — detection, threading, feedback, follow-ups, stats — needs a mailbox to
    read, and a scanner that reaches the network in a test is a scanner that
    fails in CI.
    """
    from app.services import gmail_service, inbound_scanner

    mailbox: dict[str, dict] = {}

    def _list(account, query, *, max_results=50):
        return [{"id": m["id"], "threadId": m["threadId"]} for m in mailbox.values()][
            :max_results
        ]

    def _get(account, message_id):
        return mailbox[message_id]

    monkeypatch.setattr(gmail_service, "list_messages", _list)
    monkeypatch.setattr(gmail_service, "get_message", _get)
    monkeypatch.setattr(inbound_scanner.gmail_service, "list_messages", _list)
    monkeypatch.setattr(inbound_scanner.gmail_service, "get_message", _get)
    return mailbox


@pytest.fixture()
def profiles(db_session, current_user, resume):
    """Two intents, one clearly right for the fixture recruiter email.

    Two rather than one on purpose: a single profile makes every match look
    correct, and the thing worth testing is that the *right* one wins.
    """
    from app.models.profile import Profile

    backend = Profile(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Backend Engineer",
        target_roles=["Senior Backend Engineer", "Backend Engineer"],
        skills=["python", "fastapi", "aws", "postgresql"],
        location_preferences=["San Francisco", "Remote"],
        experience_level="senior",
        is_active=True,
        is_default=True,
    )
    nursing = Profile(
        user_id=current_user.id,
        resume_id=resume.id,
        name="Registered Nurse",
        target_roles=["Registered Nurse"],
        skills=["triage", "patient care"],
        location_preferences=["Boston"],
        experience_level="mid",
        is_active=True,
    )
    db_session.add_all([backend, nursing])
    db_session.commit()
    return [backend, nursing]


@pytest.fixture()
def watched(db_session, current_user):
    """A user who has asked for their inbox to be watched."""
    from app.models.recruiter_email import RecruiterReplyPreference

    pref = RecruiterReplyPreference(user_id=current_user.id, enabled=True)
    db_session.add(pref)
    db_session.commit()
    return pref


@pytest.fixture()
def stub_scraper(monkeypatch):
    """Every company resolves to one solid recruiter contact.

    Shared rather than per-file: any test that drives the auto-apply pipeline to
    the point of composing an email needs a contact to compose it to, and the
    crawler that finds one is network-bound.
    """
    from app.services import recruiter_discovery
    from app.services.career_scraper import Contact, ScrapeResult

    def _fake(db, company, domain=None, **kwargs):
        slug = company.lower().replace(" ", "")
        return ScrapeResult(
            company=company,
            domain=f"{slug}.com",
            contacts=[
                Contact(
                    email=f"talent@{slug}.com",
                    name="Alex Recruiter",
                    title="Technical Recruiter",
                    kind="careers_page",
                    confidence=0.95,
                    source_url=f"https://{slug}.com/careers",
                )
            ],
            careers_url=f"https://{slug}.com/careers",
        )

    monkeypatch.setattr(recruiter_discovery, "get_or_scrape", _fake)


@pytest.fixture()
def no_scan(monkeypatch):
    """Neutralise the network discovery step — tests seed postings directly."""
    from app.services import auto_apply_service

    monkeypatch.setattr(auto_apply_service, "run_search", lambda db, search: None)


SAMPLE_JOB_DESCRIPTION = """\
Senior Backend Engineer

Company: Northwind Labs
Location: San Francisco, CA
We are a fintech payments company operating at scale. This is a hybrid role.
Salary: $160,000 - $200,000

Requirements
- 6+ years of professional experience building backend services
- Strong Python and FastAPI expertise
- Deep PostgreSQL knowledge
- Experience running services on AWS
- Familiarity with Kubernetes and Kafka

Nice to have
- Terraform
- Rust

Responsibilities
- Design and ship payment APIs
- Mentor engineers on the team
- Own service reliability end to end
"""


@pytest.fixture()
def job_description() -> str:
    """A realistic posting, shared by the parser, tailoring and scoring tests."""
    return SAMPLE_JOB_DESCRIPTION


@pytest.fixture()
def resume(db_session, current_user) -> Resume:
    """A parsed resume, as the uploader would have stored it."""
    row = Resume(
        user_id=current_user.id,
        filename="jordan.pdf",
        raw_text="Jordan Candidate — Senior Backend Engineer. Python, FastAPI, AWS.",
        full_name="Jordan Candidate",
        email="candidate@example.com",
        location="San Francisco, CA",
        headline="Senior Backend Engineer",
        years_experience=8,
        seniority="senior",
        skills=["python", "fastapi", "aws"],
        target_roles=["Senior Backend Engineer"],
        target_industries=["fintech"],
        experience=[
            {"company": "Acme", "title": "Backend Engineer", "start": "2018", "end": "2023"}
        ],
        education=[],
        links=[],
        is_default=True,
        parsed_with="heuristic",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row
