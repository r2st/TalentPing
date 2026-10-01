"""GET /recruiters: listing and the name/company/email search filter."""
from __future__ import annotations

import pytest

from app.models.recruiter import Recruiter


@pytest.fixture()
def contacts(db_session, current_user) -> list[Recruiter]:
    rows = [
        Recruiter(
            user_id=current_user.id,
            email="jane@acme.com",
            name="Jane Doe",
            company="Acme Corp",
            confidence=0.9,
        ),
        Recruiter(
            user_id=current_user.id,
            email="talent@globex.example",
            name="Pat Recruiter",
            company="Globex",
            confidence=0.7,
        ),
        Recruiter(
            user_id=current_user.id,
            email="hr@initech.example",
            name=None,
            company="Initech",
            confidence=0.5,
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()
    return rows


class TestListRecruiters:
    def test_lists_every_contact_newest_and_most_confident_first(self, auth_client, contacts):
        body = auth_client.get("/api/v1/recruiters").json()
        assert [r["email"] for r in body] == [
            "jane@acme.com",
            "talent@globex.example",
            "hr@initech.example",
        ]

    def test_another_users_contacts_are_invisible(self, auth_client, db_session, contacts):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        db_session.add(Recruiter(user_id=other.id, email="secret@rival.example"))
        db_session.commit()

        emails = {r["email"] for r in auth_client.get("/api/v1/recruiters").json()}
        assert "secret@rival.example" not in emails

    def test_requires_authentication(self, client):
        assert client.get("/api/v1/recruiters").status_code == 401


class TestSearch:
    def test_matches_by_name(self, auth_client, contacts):
        body = auth_client.get("/api/v1/recruiters", params={"q": "jane"}).json()
        assert [r["email"] for r in body] == ["jane@acme.com"]

    def test_matches_by_company(self, auth_client, contacts):
        body = auth_client.get("/api/v1/recruiters", params={"q": "globex"}).json()
        assert [r["email"] for r in body] == ["talent@globex.example"]

    def test_matches_by_email(self, auth_client, contacts):
        body = auth_client.get("/api/v1/recruiters", params={"q": "initech"}).json()
        assert [r["email"] for r in body] == ["hr@initech.example"]

    def test_is_case_insensitive(self, auth_client, contacts):
        body = auth_client.get("/api/v1/recruiters", params={"q": "ACME"}).json()
        assert [r["email"] for r in body] == ["jane@acme.com"]

    def test_is_a_substring_match(self, auth_client, contacts):
        body = auth_client.get("/api/v1/recruiters", params={"q": "rec"}).json()
        assert [r["email"] for r in body] == ["talent@globex.example"]

    def test_no_match_returns_an_empty_list_not_an_error(self, auth_client, contacts):
        body = auth_client.get("/api/v1/recruiters", params={"q": "nonexistent"}).json()
        assert body == []

    def test_a_contact_with_no_name_can_still_be_found_by_company(
        self, auth_client, contacts
    ):
        body = auth_client.get("/api/v1/recruiters", params={"q": "initech"}).json()
        assert len(body) == 1
