"""Celery application + beat schedule."""
from __future__ import annotations

from celery import Celery

from app.core.config import settings
from app.services.send_time import MAX_SEND_DELAY_SECONDS

#: How long Redis waits for an ack before handing a message to someone else.
#:
#: Redis has no server-side notion of an in-flight message. Kombu emulates one:
#: a delivered message sits in an ``unacked`` set, and anything not acked within
#: ``visibility_timeout`` is *restored to the queue* on the assumption its
#: consumer died. The default is one hour.
#:
#: A countdown is not a broker-side timer. ``apply_async(countdown=...)`` sends
#: the message immediately; the worker takes delivery, holds it in memory until
#: the ETA, and only acks when it finally runs (``task_acks_late``). So every
#: outreach send deferred past an hour — which is most of them, since
#: ``send_time.next_slot`` aims at the recipient's next weekday morning — was
#: unacked for far longer than the default timeout, and Redis restored it.
#:
#: The restore does not replace the copy the worker is already holding, it adds
#: one. So each hour every pending send *doubled*: production went from 54
#: queued rows to 7,390 held messages and 47,183 deliveries in a day, against a
#: single completed send. The worker's memory grew with the pile until systemd
#: restarted it — every few hours, and accelerating — and a restart re-queues
#: the whole unacked set, so the next worker inherited the duplicates and
#: started doubling them again. Fifty-four messages, the oldest twelve days old,
#: never went out; nothing failed, and nothing in the logs said so.
#:
#: Derived from the scheduler's own ceiling rather than written as a literal, so
#: the two cannot drift apart: whatever the furthest-out slot
#: ``send_time.delay_seconds`` will hand to the broker, this stays a day beyond
#: it. ``tests/test_celery_wiring.py`` holds that relationship.
#:
#: The cost is that a message whose worker is killed *ungracefully* waits this
#: long to be redelivered instead of an hour. Nothing here depends on that path:
#: every periodic task is re-created by beat within minutes, a graceful stop
#: restores the unacked set immediately, and queued outreach has its own
#: database-level recovery in ``email_tasks.sweep_stranded_sends``.
BROKER_VISIBILITY_TIMEOUT = MAX_SEND_DELAY_SECONDS + 86_400

celery_app = Celery(
    "talentping",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.email_tasks",
        "app.tasks.inbox_tasks",
        "app.tasks.follow_up_tasks",
        "app.tasks.job_tasks",
        "app.tasks.auto_apply_tasks",
        "app.tasks.form_apply_tasks",
        "app.tasks.recruiter_reply_tasks",
        "app.tasks.backlog_tasks",
        "app.tasks.digest_tasks",
        "app.tasks.notification_tasks",
        "app.tasks.credential_tasks",
        "app.tasks.dead_letter_tasks",
        "app.tasks.artifact_tasks",
        "app.tasks.usage_tasks",
    ],
)

# Importing this connects the ``task_failure`` / ``task_revoked`` handlers that
# write the dead-letter table. It has to happen here rather than in ``include``:
# ``include`` is a list of modules holding *tasks*, and this module holds none,
# so a worker would never import it and failures would go on vanishing into the
# log. Imported for the side effect only — see :mod:`app.tasks.dead_letter`.
from app.tasks import dead_letter as _dead_letter  # noqa: E402,F401  (side effect)

# Same reasoning, for the logging side: connecting `setup_logging` is what stops
# Celery installing a handler of its own, and it has to be connected before the
# worker boots rather than when some task module happens to be imported. Also
# imported by the API process, where the only handler that fires is the one that
# stamps the outgoing message with the request id publishing it.
from app.tasks import observability as _observability  # noqa: E402,F401  (side effect)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Nothing ever reads a task result — outreach tasks are fire-and-forget and
    # record their outcome in Postgres. Ignoring results keeps apply_async from
    # touching the result store, which otherwise blocks the caller for ~20s
    # reconnecting whenever Redis is unavailable.
    task_ignore_result=True,
    # Fail fast when the broker is unreachable instead of blocking the caller.
    # The API dispatches campaign work with .delay() from inside a request, and
    # falls back to running it inline (see routers/campaigns._launch) — that
    # fallback only works if the publish raises promptly rather than retrying
    # for minutes. Workers are unaffected; they reconnect via broker_pool_limit.
    broker_connection_retry_on_startup=False,
    # NB: 0 (and None) mean "retry forever" in Celery — 1 is the fail-fast value.
    broker_connection_max_retries=1,
    broker_transport_options={
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
        # See :data:`BROKER_VISIBILITY_TIMEOUT`. Without this, every send held
        # for the recipient's morning is re-delivered hourly and duplicates.
        "visibility_timeout": BROKER_VISIBILITY_TIMEOUT,
    },
    task_publish_retry=False,
)

# Periodic jobs.
#
# Cadences differ by how fast the underlying thing moves: replies arrive at any
# moment, follow-ups are due to the quarter-hour at best, and job boards refresh
# on the order of hours. Scanning any of them faster buys nothing.
celery_app.conf.beat_schedule = {
    # Credential overrides set from the admin screen are applied to the worker
    # that served the write and to nobody else. This is how the other API
    # workers, this worker and beat itself find out. Cheap — one indexed select
    # over a table that is usually empty — and skipping it means the dashboard
    # reports a rotation that the process actually sending mail never saw.
    "refresh-credentials": {
        "task": "app.tasks.credential_tasks.refresh_credentials",
        "schedule": 300.0,
    },
    "poll-inboxes": {
        "task": "app.tasks.inbox_tasks.poll_all_inboxes",
        "schedule": 300.0,
    },
    "process-due-follow-ups": {
        "task": "app.tasks.follow_up_tasks.process_due_follow_ups",
        "schedule": float(settings.follow_up_scan_interval_seconds),
    },
    "scan-job-searches": {
        "task": "app.tasks.job_tasks.scan_due_searches",
        "schedule": float(settings.job_scan_interval_seconds),
    },
    # The autopilot: for every user who has switched it on, run the full
    # discover→score→tailor→email→follow-up loop. Hourly is plenty — the
    # warm-up ramp caps how much can happen per day anyway, and running more
    # often just re-checks an exhausted budget.
    "run-autopilots": {
        "task": "app.tasks.auto_apply_tasks.run_all_autopilots",
        "schedule": float(settings.autopilot_scan_interval_seconds),
    },
    # Outreach that is QUEUED with nothing on the broker aiming at it. The only
    # re-dispatcher used to be `enqueue_campaign_sends`, reachable solely by
    # resuming the campaign — which a COMPLETED one refuses, and a completed
    # campaign is exactly where an approved review-queue draft and every
    # follow-up live. A dropped publish therefore retired real, hand-approved
    # mail permanently; production had accumulated sixty such rows, the oldest
    # ten days old. See `email_tasks.sweep_stranded_sends` for why re-dispatching
    # cannot double-send or send early.
    "sweep-stranded-sends": {
        "task": "app.tasks.email_tasks.sweep_stranded_sends",
        "schedule": 3600.0,
        # A sweep queued longer than the gap to the next one has nothing the next
        # one will not find — the rows it looks for are days old by definition.
        "options": {"expires": 3600.0},
    },
    # The warm-up ramp reads one column, and that column does not survive a
    # mailbox being removed and re-added — which is what a rotated Google OAuth
    # client does to every mailbox on the deployment at once. The connect
    # callback re-derives it now, but only for connections made *after* that
    # fix, so the mailboxes already carrying a wrong clock stay at 5/day
    # indefinitely and their queue never drains. This reconciles them from the
    # sends the address actually made. Idempotent and one-directional — see
    # `email_tasks.reconcile_warmup_ramps` for why it cannot fake warmth.
    "reconcile-warmup-ramps": {
        "task": "app.tasks.email_tasks.reconcile_warmup_ramps",
        "schedule": 21600.0,
        "options": {"expires": 21600.0},
    },
    # Form applications are the only work here that can be killed mid-flight —
    # a browser run takes minutes, so a restarted worker leaves rows stuck on
    # RUNNING. This sweeps them back onto the queue (or fails them for good once
    # they're out of attempts) rather than leaving the UI lying.
    "sweep-stuck-form-applications": {
        "task": "app.tasks.form_apply_tasks.sweep_stuck_applications",
        "schedule": 1800.0,
    },
    # Gmail watches expire after 7 days and lapse silently — no error, push just
    # stops. This re-registers them well ahead of the deadline; a no-op when no
    # Pub/Sub topic is configured.
    # Inbound recruiter mail, every minute by default. This one *is* urgent to
    # the minute: the thing being optimised is how long a recruiter waits for an
    # answer, and a quarter of an hour of that was spent waiting for a tick.
    #
    # A tick that finds nothing costs one `messages.list` per watched mailbox —
    # the model calls are driven by new messages, not by how often we look — and
    # the task itself refuses to re-scan a mailbox read within
    # RECRUITER_SCAN_MIN_GAP_SECONDS, so beat, Gmail push and the "scan now"
    # button can't stack up on each other.
    #
    # A no-op unless a deployment sets RECRUITER_REPLY_ENABLED and a user asks
    # for their inbox to be watched.
    "scan-recruiter-inboxes": {
        "task": "app.tasks.recruiter_reply_tasks.scan_all_recruiter_inboxes",
        "schedule": float(settings.recruiter_scan_interval_seconds),
        # Drop a tick that has been sitting in the queue longer than the gap to
        # the next one. A backed-up worker should skip stale scans, not work
        # through a queue of them — the newest scan sees everything the stale
        # ones would have.
        "options": {"expires": float(settings.recruiter_scan_interval_seconds)},
    },
    # Read again the mail that no model could read the first time. The scan above
    # gets one attempt per message at a provider chain that, on the free tiers,
    # answers about a third of the time; without this the other two thirds keep a
    # keyword guess forever, and a keyword guess cannot clear the auto bar once it
    # is multiplied by the match score. Deliberately slow and small — the
    # condition being recovered from is caused by too many model calls at once, so
    # the recovery must not make them. See
    # ``recruiter_reply_tasks.retry_degraded_classifications``.
    "retry-degraded-classifications": {
        "task": "app.tasks.recruiter_reply_tasks.retry_degraded_classifications",
        "schedule": float(settings.recruiter_retry_interval_seconds),
        # Same reasoning as the scan above: a sweep that has been queued longer
        # than the gap to the next one has nothing the next one won't find.
        "options": {"expires": float(settings.recruiter_retry_interval_seconds)},
    },
    # The mail that arrived while nothing was reading it. Every other scan in
    # this file is bounded by `RECRUITER_SCAN_WINDOW_DAYS`, so an outage longer
    # than a week does not delay the mail it covers — it hides it for good. This
    # is the only thing that looks further back than the window, and it is why a
    # ten-day OAuth outage is now a delay rather than a loss.
    #
    # Six-hourly, and a no-op on a healthy deployment: everything inside the
    # ordinary window was already detected, so the wide scan finds nothing new
    # and the drain finds nothing pending. It costs one `messages.list` per
    # mailbox per run to stay that way.
    "catch-up-recruiter-backlog": {
        "task": "app.tasks.backlog_tasks.catch_up_all_backlogs",
        "schedule": float(settings.recruiter_backlog_interval_seconds),
        # A catch-up queued longer than the gap to the next one has nothing the
        # next one will not find: it reads a window, not a delta.
        "options": {"expires": float(settings.recruiter_backlog_interval_seconds)},
    },
    # The weekly digest. Hourly rather than weekly on purpose: every user's row
    # decides whether it is due, so a worker that happens to be down at 08:00
    # Monday costs nobody their digest — beat has no catch-up for a missed
    # interval, it simply moves on. The is_due guard is what keeps an hourly
    # tick from sending twenty-four digests on a Monday.
    "send-weekly-digests": {
        "task": "app.tasks.digest_tasks.send_weekly_digests",
        "schedule": float(settings.digest_scan_interval_seconds),
    },
    "renew-gmail-watches": {
        "task": "app.tasks.inbox_tasks.renew_gmail_watches",
        "schedule": float(settings.gmail_watch_renew_interval_seconds),
    },
    # Nothing else deletes from `dead_letter_jobs`, and the collapse only merges
    # *identical* failures — so the row count grows with the number of distinct
    # things that have ever broken. Daily, because the thing being kept usable
    # is a screen someone opens during an incident, and it is no use if it opens
    # on a scroll of failures from releases that no longer exist. See
    # `dead_letter_tasks.prune_dead_letters` for why open and resolved rows age
    # out on different clocks.
    # Turn state into notice. Fifteen minutes is the resolution the product
    # promises on the one condition that is genuinely time-sensitive — a
    # recruiter who wrote and is waiting — and every other condition here is
    # measured in days, so a tick that is late costs nothing. Cheap by
    # construction: five bounded selects per user and, on a healthy account,
    # zero writes, because every key it would emit is already claimed.
    "sweep-notifications": {
        "task": "app.tasks.notification_tasks.sweep_notifications",
        "schedule": float(settings.notification_sweep_interval_seconds),
        # A sweep queued longer than the gap to the next one has nothing the
        # next one will not find: it reads current state, not a delta.
        "options": {"expires": float(settings.notification_sweep_interval_seconds)},
    },
    # Nothing else deletes from `notifications`, and the same argument as
    # `prune-dead-letters` applies: the row count grows with everything that has
    # ever happened, and the thing being kept usable is a list someone opens to
    # find out what is waiting on them today.
    "prune-notifications": {
        "task": "app.tasks.notification_tasks.prune_notifications",
        "schedule": 86400.0,
        "options": {"expires": 86400.0},
    },
    # Nothing else deletes from `feature_events`, and it takes a row per
    # deliberate act, so it grows with engagement. Same daily clock and the same
    # argument as the two above: what degrades first is not disk, it is the one
    # report that reads the table.
    "prune-usage-events": {
        "task": "app.tasks.usage_tasks.prune_usage_events",
        "schedule": 86400.0,
        "options": {"expires": 86400.0},
    },
    # The only prune here that deletes *files* rather than rows, and the only
    # one whose absence was a personal-data problem rather than a performance
    # one: a screenshot of a part-filled application, and the resume text
    # written out for an upload control, both sat on disk forever. See
    # `app.tasks.artifact_tasks`.
    "prune-form-apply-artifacts": {
        "task": "app.tasks.artifact_tasks.prune_form_apply_artifacts",
        "schedule": 86400.0,
        "options": {"expires": 86400.0},
    },
    "prune-dead-letters": {
        "task": "app.tasks.dead_letter_tasks.prune_dead_letters",
        "schedule": 86400.0,
        # A prune that queued behind a day's work has nothing tomorrow's will
        # not find: it deletes by age, so it is the same rows either way.
        "options": {"expires": 86400.0},
    },
}
