"""Retention for the feature-usage table.

One task. Nothing else deletes from ``feature_events``, and it takes a row per
deliberate act — a filter changed, a card opened, a panel expanded — so it grows
with engagement rather than with anything bounded.

The argument for pruning is the one ``prune_notifications`` makes and it is not
about disk: the report this table feeds asks about the last thirty days, and
every row older than the retention window is a row that read has to skip. A
usage dashboard that gets slower the more the product is used is a dashboard
that stops being opened.
"""
from __future__ import annotations

import logging

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import usage_events
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.usage_tasks.prune_usage_events")
def prune_usage_events() -> dict:
    """Delete usage events past the retention window."""
    db = SessionLocal()
    try:
        removed = usage_events.prune(db)
        if removed:
            logger.info("pruned %s usage events", removed)
        return {
            "deleted": removed,
            "retention_days": settings.usage_analytics_retention_days,
        }
    finally:
        db.close()


__all__ = ["prune_usage_events"]
