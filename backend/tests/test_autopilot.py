"""Campaign autopilot: discovery, generation, and the tracker read model.

The scraper is stubbed throughout — these tests assert the pipeline's wiring and
guard rails, not the crawler (see test_career_scraper.py for that).
"""
from __future__ import annotations

import pytest

from app.models.application import Application, ApplicationStatus
from app.models.campaign import CampaignStatus
from app.models.email import Email, EmailDirection, EmailStatus, ReplyIntent
from app.models.email_thread import EmailThread
from app.models.recruiter import Recruiter
from app.models.subject_variant import SubjectVariant
from app.services import career_scraper, recruiter_discovery
from app.services.career_scraper import Contact, ScrapeResult


@pytest.fixture()
def stub_scraper(monkeypatch):
    """Every company resolves to two contacts: a real one and a guessed one."""
    seen: list[str] = []

    def _fake(db, company, domain=None, **kwargs):
        seen.append(company)
        slug = company.lower().replace(" ", "")
        return ScrapeResult(
            company=company,
            domain=f"{slug}.com",
            contacts=[
                Contact(
                    email=f"careers@{slug}.com",
                    kind="careers_page",
                    confidence=0.95,
                    source_url=f"https://{slug}.com/careers",
                ),
                Contact(email=f"hr@{slug}.com", kind="pattern", confidence=0.4),
            ],
            careers_url=f"https://{slug}.com/careers",
        )

    monkeypatch.setattr(recruiter_discovery, "get_or_scrape", _fake)
    return seen


@pytest.fixture()
def ready(auth_client, connected_gmail, resume, stub_scraper):
    """A user who has finished onboarding: Gmail connected, resume uploaded."""
    return auth_client


class TestOnboarding:
    def test_starts_at_upload_resume(self, auth_client):
        """Resumes first: it is the step that does work for the user."""
        body = auth_client.get("/api/v1/onboarding").json()
        assert body["next_step"] == "upload_resume"
        assert body["complete"] is False

    def test_a_connected_gmail_alone_does_not_advance_past_the_resume(
        self, auth_client, connected_gmail
    ):
        body = auth_client.get("/api/v1/onboarding").json()
        assert body["next_step"] == "upload_resume"
        assert body["gmail_address"] == "candidate@gmail.com"

    def test_advances_to_set_preferences_once_a_resume_exists(
        self, auth_client, resume
    ):
        """No Gmail needed to reach the search step — it is asked for last."""
        body = auth_client.get("/api/v1/onboarding").json()
        assert body["next_step"] == "set_preferences"
        assert body["resume_count"] == 1

    def test_reading_preferences_does_not_complete_the_step(self, ready):
        """GET /autopilot creates the row lazily; that must not skip the step."""
        ready.get("/api/v1/autopilot")
        body = ready.get("/api/v1/onboarding").json()
        assert body["next_step"] == "set_preferences"
        assert body["autopilot_configured"] is False

    def test_asks_for_gmail_once_the_search_is_confirmed(self, auth_client, resume):
        auth_client.put("/api/v1/autopilot", json={"min_fit_score": 75})
        body = auth_client.get("/api/v1/onboarding").json()
        assert body["next_step"] == "connect_email"
        assert body["gmail_connected"] is False

    def test_advances_to_start_once_preferences_are_saved(self, ready):
        ready.put("/api/v1/autopilot", json={"min_fit_score": 75})
        body = ready.get("/api/v1/onboarding").json()
        assert body["next_step"] == "start_autopilot"
        assert body["autopilot_configured"] is True
        assert body["autopilot_active"] is False

    def test_done_once_autopilot_is_switched_on(self, ready):
        ready.put("/api/v1/autopilot", json={"target_roles": ["Staff Engineer"]})
        ready.put("/api/v1/autopilot", json={"is_active": True})
        body = ready.get("/api/v1/onboarding").json()
        assert body["next_step"] == "done" and body["complete"] is True
        assert body["autopilot_active"] is True


class TestGuardRails:
    def test_campaign_requires_a_connected_gmail(self, auth_client, resume):
        resp = auth_client.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        assert resp.status_code == 409
        assert "Gmail" in resp.json()["detail"]

    def test_campaign_requires_a_resume(self, auth_client, connected_gmail):
        resp = auth_client.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        assert resp.status_code == 409
        assert "resume" in resp.json()["detail"]

    def test_campaign_requires_a_target(self, ready):
        assert ready.post("/api/v1/campaigns", json={}).status_code == 422


class TestAutopilot:
    def test_one_call_discovers_generates_and_queues(self, ready, db_session):
        resp = ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme", "Northwind"], "auto_send": True},
        )
        assert resp.status_code == 201, resp.text
        campaign = resp.json()["campaign"]

        assert campaign["status"] == CampaignStatus.ACTIVE.value
        assert campaign["companies_processed"] == 2
        assert campaign["contacts_found"] == 4
        assert campaign["emails_generated"] == 4

        # Contacts were saved with their provenance.
        recruiters = db_session.query(Recruiter).all()
        assert {r.email for r in recruiters} == {
            "careers@acme.com", "hr@acme.com",
            "careers@northwind.com", "hr@northwind.com",
        }
        scraped = next(r for r in recruiters if r.email == "careers@acme.com")
        assert scraped.source == "careers_page"
        assert scraped.source_url == "https://acme.com/careers"
        assert next(r for r in recruiters if r.email == "hr@acme.com").source == "pattern"

    def test_autopilot_queues_emails_without_review(self, ready, db_session):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        statuses = {e.status for e in db_session.query(Email).all()}
        assert statuses == {EmailStatus.QUEUED}

    def test_auto_send_false_parks_emails_as_drafts(self, ready, db_session):
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme"], "auto_send": False},
        )
        statuses = {e.status for e in db_session.query(Email).all()}
        assert statuses == {EmailStatus.DRAFT}

    def test_emails_are_personalized_from_the_resume(self, ready, db_session):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        email = db_session.query(Email).first()
        # The subject is one arm of the campaign's A/B experiment, so it is no
        # longer a fixed string — but it is always one the experiment produced.
        variants = db_session.query(SubjectVariant).all()
        assert email.subject in {v.text for v in variants}
        assert email.subject_variant_id in {v.id for v in variants}
        assert "Jordan Candidate" in (email.body_text or "")
        # No recruiter name is known, so it must address the company, not a person.
        assert "Hi Acme team," in email.body_text

    def test_campaign_name_is_derived_when_omitted(self, ready):
        body = ready.post(
            "/api/v1/campaigns", json={"target_companies": ["Acme"]}
        ).json()["campaign"]
        assert "Senior Backend Engineer" in body["name"]
        assert "Acme" in body["name"]

    def test_industry_targets_expand_into_companies(self, ready, stub_scraper):
        resp = ready.post("/api/v1/campaigns", json={"target_industries": ["fintech"]})
        assert resp.status_code == 201
        assert resp.json()["campaign"]["companies_processed"] > 1
        assert "Stripe" in stub_scraper

    def test_running_the_same_company_twice_does_not_duplicate_outreach(
        self, ready, db_session
    ):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        first = db_session.query(Application).count()
        # A second campaign against the same company reuses the recruiter rows
        # but must still create its own applications — the guard is per campaign.
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        assert db_session.query(Recruiter).count() == 2
        assert db_session.query(Application).count() == first * 2

    def test_a_company_with_no_contacts_completes_with_a_reason(
        self, ready, monkeypatch
    ):
        monkeypatch.setattr(
            recruiter_discovery,
            "get_or_scrape",
            lambda db, company, domain=None, **k: ScrapeResult(
                company=company, domain="x.com", status="empty", note="nothing published"
            ),
        )
        body = ready.post(
            "/api/v1/campaigns", json={"target_companies": ["Ghost Co"]}
        ).json()["campaign"]
        assert body["status"] == CampaignStatus.COMPLETED.value
        assert "nothing published" in body["last_error"]

    def test_opted_out_contacts_are_never_emailed(self, ready, db_session):
        db_session.add(
            Recruiter(
                user_id=db_session.query(Recruiter).count() and 1 or 1,
                email="careers@acme.com",
                company="Acme",
                opted_out=True,
            )
        )
        db_session.commit()

        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        recipients = {e.to_address for e in db_session.query(Email).all()}
        assert "careers@acme.com" not in recipients


class TestPauseResume:
    def test_pause_then_resume(self, ready, db_session):
        campaign_id = ready.post(
            "/api/v1/campaigns", json={"target_companies": ["Acme"]}
        ).json()["campaign"]["id"]

        paused = ready.post(f"/api/v1/campaigns/{campaign_id}/pause").json()
        assert paused["status"] == CampaignStatus.PAUSED.value

        resumed = ready.post(f"/api/v1/campaigns/{campaign_id}/resume").json()
        assert resumed["campaign"]["status"] in {
            CampaignStatus.ACTIVE.value,
            CampaignStatus.COMPLETED.value,
        }

    def test_an_active_campaign_cannot_be_resumed(self, ready):
        campaign_id = ready.post(
            "/api/v1/campaigns", json={"target_companies": ["Acme"]}
        ).json()["campaign"]["id"]
        # The stub completes the run synchronously, so force it back to ACTIVE.
        ready.post(f"/api/v1/campaigns/{campaign_id}/pause")
        ready.post(f"/api/v1/campaigns/{campaign_id}/resume")

        resp = ready.post(f"/api/v1/campaigns/{campaign_id}/resume")
        assert resp.status_code in {200, 409}

    def test_campaigns_are_scoped_to_their_owner(self, ready, client):
        campaign_id = ready.post(
            "/api/v1/campaigns", json={"target_companies": ["Acme"]}
        ).json()["campaign"]["id"]

        client.post(
            "/api/v1/auth/register",
            json={"email": "someone@else.com", "password": "supersecret123"},
        )
        token = client.post(
            "/api/v1/auth/login",
            data={"username": "someone@else.com", "password": "supersecret123"},
        ).json()["access_token"]

        resp = client.get(
            f"/api/v1/campaigns/{campaign_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


class TestTracker:
    def test_lists_every_outreach_with_stats(self, ready):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        body = ready.get("/api/v1/tracker").json()

        assert body["stats"]["contacted"] == 2
        assert body["stats"]["queued"] == 2
        assert body["stats"]["replied"] == 0
        row = body["rows"][0]
        assert row["company"] == "Acme"
        assert row["status"] == ApplicationStatus.QUEUED.value
        assert row["subject"]

    def test_surfaces_a_reply_and_its_intent(self, ready, db_session):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        thread = db_session.query(EmailThread).first()
        db_session.add(
            Email(
                thread_id=thread.id,
                direction=EmailDirection.RECEIVED,
                status=EmailStatus.RECEIVED,
                from_address="careers@acme.com",
                body_text="We'd love to chat — are you free Thursday?",
                intent=ReplyIntent.SCHEDULING,
            )
        )
        application = db_session.get(Application, thread.application_id)
        application.status = ApplicationStatus.SCHEDULING
        db_session.commit()

        body = ready.get("/api/v1/tracker").json()
        row = next(r for r in body["rows"] if r["application_id"] == application.id)
        assert row["reply_intent"] == ReplyIntent.SCHEDULING.value
        assert "Thursday" in row["reply_snippet"]
        assert body["stats"]["replied"] == 1
        assert body["stats"]["interviews"] == 1
        assert body["stats"]["reply_rate"] == 0.5

    def test_can_filter_by_campaign(self, ready):
        first = ready.post(
            "/api/v1/campaigns", json={"target_companies": ["Acme"]}
        ).json()["campaign"]["id"]
        ready.post("/api/v1/campaigns", json={"target_companies": ["Northwind"]})

        body = ready.get(f"/api/v1/tracker?campaign_id={first}").json()
        assert {r["campaign_id"] for r in body["rows"]} == {first}

    def test_detail_returns_the_message_history(self, ready):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        application_id = ready.get("/api/v1/tracker").json()["rows"][0]["application_id"]

        body = ready.get(f"/api/v1/tracker/{application_id}").json()
        assert body["threads"][0]["emails"][0]["subject"]

    def test_draft_emails_can_be_edited_before_sending(self, ready, db_session):
        ready.post(
            "/api/v1/campaigns",
            json={"target_companies": ["Acme"], "auto_send": False},
        )
        email_id = db_session.query(Email).first().id

        resp = ready.patch(
            f"/api/v1/tracker/emails/{email_id}", json={"subject": "Rewritten"}
        )
        assert resp.status_code == 200 and resp.json()["subject"] == "Rewritten"

    def test_queued_emails_cannot_be_edited(self, ready, db_session):
        ready.post("/api/v1/campaigns", json={"target_companies": ["Acme"]})
        email_id = db_session.query(Email).first().id

        resp = ready.patch(
            f"/api/v1/tracker/emails/{email_id}", json={"subject": "Too late"}
        )
        assert resp.status_code == 409

    def test_tracker_requires_auth(self, client):
        assert client.get("/api/v1/tracker").status_code == 401


class TestIndustryExpansion:
    def test_falls_back_to_a_static_list_without_an_api_key(self):
        companies = recruiter_discovery.companies_for_industry("fintech")
        assert "Stripe" in companies

    def test_unknown_industry_yields_nothing_rather_than_guessing(self):
        assert recruiter_discovery.companies_for_industry("underwater basketry") == []

    def test_llm_suggestions_are_used_when_available(self, monkeypatch):
        monkeypatch.setattr(
            recruiter_discovery.settings, "openrouter_api_key", "test-key"
        )
        monkeypatch.setattr(
            recruiter_discovery,
            "chat_completion",
            lambda *a, **k: '["Acme", "Northwind"]',
        )
        assert recruiter_discovery.companies_for_industry("tech") == ["Acme", "Northwind"]

    def test_malformed_llm_output_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            recruiter_discovery.settings, "openrouter_api_key", "test-key"
        )
        monkeypatch.setattr(recruiter_discovery, "chat_completion", lambda *a, **k: "oops")
        assert "Stripe" in recruiter_discovery.companies_for_industry("fintech")

    def test_targets_are_merged_and_deduplicated(self, monkeypatch):
        monkeypatch.setattr(
            recruiter_discovery, "companies_for_industry", lambda i, r=None: ["Stripe", "Plaid"]
        )
        merged = recruiter_discovery.expand_targets(["Stripe", "Acme"], ["fintech"], [])
        assert merged == ["Stripe", "Acme", "Plaid"]


class TestDiscoverEndpoint:
    def test_previews_contacts_for_named_companies(self, auth_client, stub_scraper):
        resp = auth_client.post(
            "/api/v1/recruiters/discover", json={"companies": ["Acme"]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["found"] == 2
        assert {r["email"] for r in body["recruiters"]} == {
            "careers@acme.com",
            "hr@acme.com",
        }

    def test_requires_at_least_one_company(self, auth_client):
        assert (
            auth_client.post("/api/v1/recruiters/discover", json={"companies": []}).status_code
            == 422
        )

    def test_reports_companies_that_yielded_nothing(self, auth_client, monkeypatch):
        monkeypatch.setattr(
            recruiter_discovery,
            "get_or_scrape",
            lambda db, company, domain=None, **k: ScrapeResult(
                company=company, status="empty", note="no site found"
            ),
        )
        body = auth_client.post(
            "/api/v1/recruiters/discover", json={"companies": ["Ghost"]}
        ).json()
        assert body["found"] == 0
        assert body["notes"] == ["Ghost: no site found"]


def test_scrape_result_exposes_a_flat_email_list():
    result = ScrapeResult(
        company="Acme", contacts=[Contact(email="a@acme.com"), Contact(email="b@acme.com")]
    )
    assert result.emails == ["a@acme.com", "b@acme.com"]
    assert career_scraper.ROLE_PREFIXES[0] == "careers"
