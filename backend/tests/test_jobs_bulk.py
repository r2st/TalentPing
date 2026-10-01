"""Bulk triage on the job feed.

Triage is the job the feed exists for, and doing it one row at a time is where
a 40-result scan stops being worth opening. The behaviour worth pinning down is
what happens at the edges of a selection: ids that aren't yours, ids already in
the target status, and the fact that ``archive`` deletes rather than hides.
"""
from __future__ import annotations

import pytest

from app.models.job import JobPosting, JobStatus

BULK = "/api/v1/jobs/bulk"


@pytest.fixture()
def postings(db_session, current_user) -> list[JobPosting]:
    """Five untriaged postings in the feed."""
    rows = [
        JobPosting(
            user_id=current_user.id,
            title=f"Role {index}",
            company=f"Company {index}",
            fingerprint=f"fp-bulk-{index}",
            fit_score=90 - index,
        )
        for index in range(5)
    ]
    db_session.add_all(rows)
    db_session.commit()
    for row in rows:
        db_session.refresh(row)
    return rows


class TestBulkTriage:
    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("save", JobStatus.SAVED),
            ("apply", JobStatus.APPLIED),
            ("dismiss", JobStatus.DISMISSED),
        ],
    )
    def test_moves_the_whole_selection(
        self, auth_client, db_session, postings, action, expected
    ):
        ids = [p.id for p in postings[:3]]

        body = auth_client.post(BULK, json={"job_ids": ids, "action": action}).json()

        assert body["updated"] == 3
        assert body["not_found"] == []
        for posting in postings[:3]:
            db_session.refresh(posting)
            assert posting.status is expected
        # Everything outside the selection is untouched.
        db_session.refresh(postings[4])
        assert postings[4].status is JobStatus.NEW

    def test_applying_stamps_the_applied_time(self, auth_client, db_session, postings):
        auth_client.post(BULK, json={"job_ids": [postings[0].id], "action": "apply"})

        db_session.refresh(postings[0])
        assert postings[0].applied_at is not None

    def test_re_applying_does_not_move_the_original_timestamp(
        self, auth_client, db_session, postings
    ):
        auth_client.post(BULK, json={"job_ids": [postings[0].id], "action": "apply"})
        db_session.refresh(postings[0])
        first = postings[0].applied_at

        auth_client.post(BULK, json={"job_ids": [postings[0].id], "action": "apply"})
        db_session.refresh(postings[0])
        assert postings[0].applied_at == first

    def test_rows_already_in_the_target_status_are_reported_not_counted(
        self, auth_client, postings
    ):
        """A second sweep over the same selection is honest about doing nothing."""
        ids = [p.id for p in postings[:2]]
        auth_client.post(BULK, json={"job_ids": ids, "action": "dismiss"})

        body = auth_client.post(BULK, json={"job_ids": ids, "action": "dismiss"}).json()
        assert body["updated"] == 0
        assert body["skipped"] == 2

    def test_archive_deletes_the_rows(self, auth_client, db_session, postings):
        ids = [p.id for p in postings[:2]]

        body = auth_client.post(BULK, json={"job_ids": ids, "action": "archive"}).json()

        assert body["deleted"] == 2
        assert db_session.get(JobPosting, ids[0]) is None
        assert db_session.get(JobPosting, postings[4].id) is not None

    def test_the_feed_reflects_the_sweep(self, auth_client, postings):
        ids = [p.id for p in postings[:3]]
        auth_client.post(BULK, json={"job_ids": ids, "action": "dismiss"})

        titles = [row["title"] for row in auth_client.get("/api/v1/jobs").json()]
        assert titles == ["Role 3", "Role 4"]


class TestPartialSelections:
    def test_unknown_ids_come_back_rather_than_404ing_the_call(
        self, auth_client, db_session, postings
    ):
        """A stale tab shouldn't cost the user the other thirty-nine rows."""
        body = auth_client.post(
            BULK, json={"job_ids": [postings[0].id, 999_999], "action": "save"}
        ).json()

        assert body["updated"] == 1
        assert body["not_found"] == [999_999]
        db_session.refresh(postings[0])
        assert postings[0].status is JobStatus.SAVED

    def test_another_users_postings_are_not_found_not_modified(
        self, auth_client, db_session, current_user, postings
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        theirs = JobPosting(
            user_id=other.id, title="Secret", fingerprint="fp-secret"
        )
        db_session.add(theirs)
        db_session.commit()

        body = auth_client.post(
            BULK, json={"job_ids": [postings[0].id, theirs.id], "action": "dismiss"}
        ).json()

        assert body["updated"] == 1
        assert body["not_found"] == [theirs.id]
        db_session.refresh(theirs)
        assert theirs.status is JobStatus.NEW

    def test_another_users_postings_survive_an_archive(
        self, auth_client, db_session, postings
    ):
        from app.models.user import User

        other = User(email="other@example.com", hashed_password="x")
        db_session.add(other)
        db_session.flush()
        theirs = JobPosting(user_id=other.id, title="Secret", fingerprint="fp-secret")
        db_session.add(theirs)
        db_session.commit()

        auth_client.post(BULK, json={"job_ids": [theirs.id], "action": "archive"})
        assert db_session.get(JobPosting, theirs.id) is not None


class TestValidation:
    def test_an_empty_selection_is_rejected(self, auth_client, postings):
        assert auth_client.post(BULK, json={"job_ids": [], "action": "save"}).status_code == 422

    def test_an_unknown_action_is_rejected(self, auth_client, postings):
        response = auth_client.post(
            BULK, json={"job_ids": [postings[0].id], "action": "incinerate"}
        )
        assert response.status_code == 422

    def test_an_absurd_selection_is_rejected(self, auth_client, postings):
        response = auth_client.post(
            BULK, json={"job_ids": list(range(1, 502)), "action": "dismiss"}
        )
        assert response.status_code == 422

    def test_bulk_is_not_captured_as_a_posting_id(self, auth_client, postings):
        """The route sits before /{job_id}; a GET on it must not 200 as a job."""
        assert auth_client.get("/api/v1/jobs/bulk").status_code in (404, 405, 422)

    def test_requires_authentication(self, client):
        assert client.post(BULK, json={"job_ids": [1], "action": "save"}).status_code == 401
