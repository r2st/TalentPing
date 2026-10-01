"""Re-parse ``job_postings.salary_text`` into the ``salary_min``/``salary_max`` band.

The band is parsed once at ingest (and once in migration ``c9a4e7b21d68``), so a
posting stored before a parser fix keeps whatever the old parser made of it —
usually null. This re-runs the current parser over every stored posting so old
and new rows read identically, which is the same promise that migration made.

Read-only by default; pass ``--apply`` to write. Postings whose band is already
what the parser now produces are left alone, so a repeat run is a no-op.

    sudo -u talentping bash -c "set -a; . /opt/TalentPing/.env; set +a; \
      cd /opt/TalentPing/backend && PYTHONPATH=/opt/TalentPing/backend \
      .venv/bin/python scripts/backfill_job_salaries.py --apply"
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import or_, select

from app.core.database import SessionLocal
from app.models.job import JobPosting
from app.services import salary_service
from app.services.jd_parser import extract_salary

logger = logging.getLogger("backfill_job_salaries")


def _band_for(
    posting: JobPosting, use_description: bool
) -> tuple[str | None, int | None, int | None]:
    """The band the current parser reads off a posting: ``(text, min, max)``."""
    salary_text = posting.salary_text
    if not salary_text and use_description and posting.description:
        # What the ingest path does when the provider published no salary field:
        # the number is usually written in the body instead.
        salary_text = extract_salary(posting.description)[0]
    low, high = salary_service.parse_offered(salary_text)
    return salary_text, low, high


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument(
        "--descriptions",
        action="store_true",
        help="also read the band out of the body when the posting has no salary_text",
    )
    parser.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    stmt = select(JobPosting).order_by(JobPosting.id)
    if not args.descriptions:
        stmt = stmt.where(JobPosting.salary_text.is_not(None))
    else:
        stmt = stmt.where(
            or_(JobPosting.salary_text.is_not(None), JobPosting.description.is_not(None))
        )
    if args.limit:
        stmt = stmt.limit(args.limit)

    changed = 0
    scanned = 0
    with SessionLocal() as db:
        for posting in db.execute(stmt).scalars():
            scanned += 1
            salary_text, low, high = _band_for(posting, args.descriptions)
            if (low, high) == (posting.salary_min, posting.salary_max):
                continue
            # Never erase a band that is already there: a parser that reads
            # nothing today is a parser gap, not evidence the employer withdrew
            # the number.
            if low is None and high is None:
                continue

            logger.info(
                "posting %s  %r  (%s, %s) -> (%s, %s)",
                posting.id,
                (salary_text or "")[:60],
                posting.salary_min,
                posting.salary_max,
                low,
                high,
            )
            changed += 1
            if args.apply:
                posting.salary_min = low
                posting.salary_max = high
                if salary_text and not posting.salary_text:
                    posting.salary_text = salary_text[:255]
        if args.apply:
            db.commit()

    logger.info(
        "%s %s of %s postings%s",
        "updated" if args.apply else "would update",
        changed,
        scanned,
        "" if args.apply else "  (dry run — pass --apply to write)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
