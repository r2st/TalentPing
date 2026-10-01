"""Beat task that carries credential overrides to every worker.

:mod:`app.services.credential_store` hydrates one process. The API worker that
served the write has the new value at once; every other API worker, the Celery
worker and beat itself are still holding what they loaded at startup.

Without this task the dashboard would report success while the worker that
actually sends mail went on using the retired key — which is worse than not
having the feature, because the operator has been told the rotation happened.
Five minutes is the propagation window that buys, and it is stated in the UI via
``applied_at`` rather than left to be discovered.
"""
from __future__ import annotations

import logging

from app.core.database import SessionLocal
from app.services import credential_store
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.credential_tasks.refresh_credentials")
def refresh_credentials() -> dict:
    """Re-read ``app_credentials`` and apply it to this process.

    Returns the keys in force rather than their values — this result is stored
    in the Celery backend and read by whoever is debugging, and neither is a
    place for a client secret.
    """
    db = SessionLocal()
    try:
        overrides = credential_store.hydrate(db)
        return {
            "overrides": sorted(overrides),
            "count": len(overrides),
            "applied_at": (
                stamp.isoformat()
                if (stamp := credential_store.last_applied()) is not None
                else None
            ),
        }
    finally:
        db.close()
