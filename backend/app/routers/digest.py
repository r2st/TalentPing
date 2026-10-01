"""Weekly digest routes — the one email the product sends *to* its user.

    GET  /digest             -> preferences, and whether a digest can be sent
    PUT  /digest             -> change the day, the hour, or switch it off
    GET  /digest/preview     -> exactly what this week's email would say
    POST /digest/send        -> send it now, ignoring the schedule
    GET  /digest/unsubscribe -> public one-click opt-out (no session)
    POST /digest/unsubscribe -> the same, for RFC 8058 one-click providers

The unsubscribe route is unauthenticated because it is clicked from a mail
client, which has no session. It is safe to leave open because the token is a
32-byte secret and the only thing it can do is set ``enabled`` to False — there
is no destructive action reachable from it, and no information disclosed by it.
It answers identically for a good and a bad token so the endpoint cannot be used
to test whether a token is live.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.patching import reject_nulls
from app.core.rate_limit import ip_rate_limit, rate_limit
from app.models.digest import DigestPreference
from app.models.user import User
from app.schemas.digest import (
    DigestContent,
    DigestDraft,
    DigestOpportunity,
    DigestPipeline,
    DigestPreferenceOut,
    DigestPreferenceUpdate,
    DigestSendResult,
)
from app.services import digest_service, usage_events

router = APIRouter(prefix="/digest", tags=["digest"])

# The digest's own opt-out, unauthenticated for the same reason the cold-outreach
# one is: it is clicked from a mailbox with no session. Metered for the same
# reason too — it writes, and the token that decides *which* row is a query
# parameter, so unbounded it is a token-guessing loop that switches digests off
# as it goes.
#
# Its own scope, not the cold-outreach one. They turn off different things for
# different people, and sharing a bucket would let a run against either deny the
# other — the same reason login and register are separate.
#
# Loose on purpose, and for the same reason: the digest goes out weekly, so a
# refused opt-out is a real recipient told "no" by the one control that has to
# work. Thirty in five minutes is far past any honest burst.
_digest_unsubscribe_limit = ip_rate_limit(30, 300, scope="digest-unsubscribe")


def _out(user: User, pref: DigestPreference) -> DigestPreferenceOut:
    return DigestPreferenceOut(
        enabled=pref.enabled,
        weekday=pref.weekday,
        hour=pref.hour,
        last_sent_at=pref.last_sent_at,
        last_error=pref.last_error,
        sent_count=pref.sent_count,
        can_send=user.primary_gmail is not None,
    )


@router.get("", response_model=DigestPreferenceOut)
def get_preferences(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DigestPreferenceOut:
    return _out(user, digest_service.get_or_create(db, user))


@router.put("", response_model=DigestPreferenceOut)
def update_preferences(
    payload: DigestPreferenceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DigestPreferenceOut:
    """Change the schedule, or switch the digest off from inside the app."""
    pref = digest_service.get_or_create(db, user)
    fields = payload.model_dump(exclude_unset=True)
    reject_nulls(DigestPreference, fields)
    for name, value in fields.items():
        setattr(pref, name, value)
    # One event name for every settings surface in the product, with `scope`
    # saying which screen. A name per screen would make "how many people change
    # settings at all" a sum somebody has to remember to compute, and would grow
    # the vocabulary by one every time a preferences form is added — which is
    # the cost that stops anyone instrumenting the next one.
    usage_events.record(
        db,
        "settings.changed",
        user_id=user.id,
        scope="digest",
        sections=sorted(fields),
    )
    db.commit()
    db.refresh(pref)
    return _out(user, pref)


@router.get("/preview", response_model=DigestContent)
def preview(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DigestContent:
    """What this week's digest would say, rendered but not sent.

    Sending nothing is the point: a user deciding whether to let the product
    email them should be able to read the email first.
    """
    digest = digest_service.build(db, user)
    pref = digest_service.get_or_create(db, user)
    app_url = settings.frontend_url.rstrip("/")
    opt_out_url = digest_service.unsubscribe_url(pref)
    return DigestContent(
        period_start=digest.period_start,
        period_end=digest.period_end,
        sent=digest.sent,
        follow_ups_sent=digest.follow_ups_sent,
        jobs_found=digest.jobs_found,
        replies=digest.replies,
        interviews=digest.interviews,
        drafts_waiting=digest.drafts_waiting,
        drafts=[DigestDraft(**vars(d)) for d in digest.drafts],
        unanswered_replies=digest.unanswered_replies,
        queued_emails=digest.queued_emails,
        follow_ups_due=digest.follow_ups_due,
        opportunities=[
            DigestOpportunity(
                job_id=o.job_id,
                title=o.title,
                company=o.company,
                where=o.where,
                fit_score=o.fit_score,
                remote=o.remote,
            )
            for o in digest.opportunities
        ],
        pipeline=DigestPipeline(
            awaiting_reply=digest.pipeline.awaiting_reply,
            in_conversation=digest.pipeline.in_conversation,
            interviewing=digest.pipeline.interviewing,
            offers=digest.pipeline.offers,
            closed=digest.pipeline.closed,
            stalled=digest.pipeline.stalled,
            active=digest.pipeline.active,
            stall_rate=digest.pipeline.stall_rate,
        ),
        response_rate=digest.response_rate,
        previous_response_rate=digest.previous_response_rate,
        headline=digest.headline,
        is_quiet=digest.is_quiet,
        subject=digest_service.subject_line(digest),
        body_text=digest_service.render_text(
            digest,
            app_url=app_url,
            opt_out_url=opt_out_url,
        ),
        body_html=digest_service.render_html(
            digest,
            app_url=app_url,
            opt_out_url=opt_out_url,
        ),
    )


@router.post(
    "/send",
    response_model=DigestSendResult,
    # `force=True` is the whole point of this route, so nothing upstream stops a
    # second press: each one composes a digest and hands a real message to
    # `gmail_service.send_email`. It goes to the user's own address, so the
    # damage is not somebody else's inbox — it is the sending reputation every
    # other feature depends on, and a Gmail quota shared with the outreach that
    # actually matters.
    #
    # A digest is a daily thing. Three an hour is far more than anyone wants and
    # far less than a stuck button sends.
    dependencies=[Depends(rate_limit(3, 3600, scope="digest-send"))],
)
def send_now(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DigestSendResult:
    """Send the digest immediately, whatever the schedule says.

    Still refuses for a user who has unsubscribed, and still refuses from a
    paused mailbox — a button in the app is not consent that overrides an
    opt-out, and it is not a reason to send from a mailbox in trouble.

    ``skip_quiet`` is off here: an explicit request for the digest should
    produce the digest, even if this week's answer is "nothing happened".
    """
    result = digest_service.send(db, user, force=True, skip_quiet=False)
    return DigestSendResult(
        status=result["status"], to=result.get("to"), error=result.get("error")
    )


@router.get(
    "/unsubscribe",
    response_class=HTMLResponse,
    dependencies=[Depends(_digest_unsubscribe_limit)],
)
def unsubscribe(token: str, db: Session = Depends(get_db)) -> str:
    """One-click opt-out from the digest. Public, idempotent, non-destructive.

    Returns the same page whether or not the token matched. A different answer
    for a live token would turn this into an oracle for guessing them, and the
    honest failure mode — a user who clicks an old link and is told they are
    unsubscribed, which they are — costs nothing.
    """
    return _opt_out(db, token)


@router.post(
    "/unsubscribe",
    response_class=HTMLResponse,
    dependencies=[Depends(_digest_unsubscribe_limit)],
)
def unsubscribe_one_click(
    token: str,
    # RFC 8058 says the provider POSTs `List-Unsubscribe=One-Click` as a form
    # body. Declared so the body parses rather than 422ing, and optional so a
    # hand-rolled POST without it still works. Not branched on: the token in the
    # URL already identifies the row, whichever way the click arrived.
    List_Unsubscribe: str | None = Form(default=None, alias="List-Unsubscribe"),
    db: Session = Depends(get_db),
) -> str:
    """The one-click opt-out (RFC 8058) the digest's own headers advertise.

    The digest ships ``List-Unsubscribe-Post: List-Unsubscribe=One-Click``, and
    that header is a promise that the URL beside it answers a POST. It did not:
    this route was GET-only, so Gmail's and Yahoo's native Unsubscribe buttons —
    which POST rather than navigate — got a 405 from a sender whose headers said
    otherwise. A provider reads that as a broken opt-out, which is the exact
    failure the header exists to rule out, and it is scored against the
    *candidate's own mailbox* — the one this product spends the rest of its
    effort protecting.

    Cold outreach already had both verbs (``app.routers.misc``). The digest's
    opt-out lives on its own route and was missed.
    """
    return _opt_out(db, token)


def _opt_out(db: Session, token: str) -> str:
    """Switch the digest off for the row *token* names, and render the page."""
    pref = db.scalar(
        select(DigestPreference).where(DigestPreference.unsubscribe_token == token)
    )
    if pref is not None and pref.enabled:
        pref.enabled = False
        db.commit()
    return (
        "<html><body style='font-family:sans-serif;max-width:480px;margin:64px auto'>"
        "<h2>Weekly digest turned off</h2>"
        f"<p>{settings.app_name} will stop sending you the Monday summary. "
        "Everything else — outreach, follow-ups, replies — is unchanged.</p>"
        "<p>You can turn it back on any time in your settings.</p>"
        "</body></html>"
    )
