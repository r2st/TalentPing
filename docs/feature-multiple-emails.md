# Multiple Email Accounts — connecting more than one mailbox

**Status:** Proposed — not implemented
**Last updated:** 2026-07-29
**Depends on:** Gmail OAuth (shipped), reputation service (shipped), recruiter inbox (shipped)
**Alembic head at time of writing:** `d4a8f2e61c93`

> Note on placement: every other feature doc lives in `docs/features/`. This one
> is at `docs/feature-multiple-emails.md` as requested. Worth moving into
> `docs/features/` before it is implemented, so the set stays in one place.

---

## 1. Overview

### 1.1 The headline finding

**The data model already supports multiple mailboxes. The UI and the send/poll
paths do not.**

`gmail_accounts` has been a one-to-many table against `users` since the first
migration. It carries `is_primary`, per-mailbox reputation counters, and a
per-mailbox push subscription. Two tables (`recruiter_emails`,
`recruiter_scan_runs`) already record *which* mailbox a message arrived in, and
`email_attachments._account_for_inbound()` already resolves a mailbox from a
delivery address rather than assuming the primary.

So this is not a "build multi-account" feature. It is three narrower jobs:

1. **Let the user add a second mailbox** — the setup page has no button for it,
   and the OAuth URL will not offer Google's account chooser.
2. **Stop 11 call sites assuming `user.primary_gmail`** — several of which are
   outright broken the moment a second account exists, not merely limited.
3. **Give a thread a mailbox of its own**, so "which address does this go out
   from?" is a stored fact rather than a lookup that guesses.

### 1.2 What is already multi-account correct

Worth knowing before touching anything, because these are the patterns to copy:

| Thing | Where | Why it's already right |
|---|---|---|
| Every Gmail API call is account-scoped | `services/gmail_service.py` | Takes `account`, decrypts *that* row's refresh token. No global client. |
| Push webhook routing | `services/gmail_push.py:account_for_address` | Resolves the account from the notification's address. |
| One watch per mailbox | `models/gmail_watch.py:48` (`unique=True` on `gmail_account_id`) | The schema already assumes N watches for N mailboxes. |
| Inbound message → mailbox | `models/recruiter_email.py:119` | `gmail_account_id`, with the comment "A user may have several, and the reply must go back out of the one the recruiter wrote to." |
| Scan-run attribution | `models/recruiter_scan_run.py:54` | Same column, `SET NULL`. |
| Attachment fetch | `services/email_attachments.py:602-617` | Picks the account from `Email.to_address`, primary as fallback. |
| "Is this me?" checks | `services/inbound_scanner.py:221` (`own_addresses`) | Unions *all* connected addresses plus the login email. |
| `poll_thread` sender detection | `tasks/inbox_tasks.py:285` | Same union. |
| Disconnect promotes a replacement | `routers/gmail.py:197-210` | Keeps a sending identity when the primary goes away. |
| Scopes are replayed per account | `services/google_oauth.py:credential_scopes_for` | Google rejects a refresh whose scopes exceed the original grant; each row remembers its own. |

### 1.3 Scope

**In scope**

- Adding, listing, re-ordering and removing several Gmail accounts per user.
- Choosing which mailbox outreach goes out from, and recording that choice on
  the thread.
- Making reputation, warm-up, polling and push per-mailbox rather than
  per-primary.
- Push and recruiter-inbox scanning on every connected mailbox, not just one.

**Out of scope**

- **Non-Gmail providers (IMAP/SMTP, Outlook).** Today's connection is Gmail
  OAuth only, and the whole deliverability argument rests on sending *as* the
  candidate through Gmail's API. IMAP is a separate feature with its own
  credential storage, its own bounce handling and no push. Nothing in this
  design blocks it later; nothing here delivers it.
- **Send-address rotation to raise volume.** See §4.3 — this is deliberately
  refused, not deferred.
- Per-mailbox signatures, aliases, or Gmail "send as" identities.

---

## 2. Current state

### 2.1 How email connection works

Gmail OAuth 2.0 authorization-code flow, no IMAP, no SMTP, no shared relay.

```
Setup step 3 ──► GET /api/v1/gmail/authorize      (auth required)
                   └─ signed Fernet `state` carries the user id
                 window.open(authorization_url)   ← popup
                 Google consent screen
                 GET /api/v1/gmail/callback       (PUBLIC — Google sends no auth header)
                   ├─ read_state()  → user id
                   ├─ exchange_code() → refresh/access token + userinfo
                   ├─ upsert GmailAccount on google_sub
                   └─ HTML that postMessage()s the opener and closes itself
```

- Scopes: `openid`, `userinfo.email`, `userinfo.profile`, `gmail.send`,
  `gmail.readonly` (`services/google_oauth.py:33-39`).
- Tokens are Fernet-encrypted at rest (`services/crypto.py`).
- `access_type=offline`, `prompt=consent`, `include_granted_scopes=true`
  (`services/google_oauth.py:138-144`).
- Sending as the candidate is the point: the mail inherits their domain's
  SPF/DKIM/DMARC alignment, which is what keeps cold outreach out of spam.

### 2.2 The data model — already one-to-many

```python
# models/user.py
gmail_accounts: Mapped[list[GmailAccount]] = relationship(..., lazy="selectin")

@property
def primary_gmail(self) -> GmailAccount | None:
    """The account outreach sends from — the primary, else the first live one."""
    live = [a for a in self.gmail_accounts if a.status == "connected"]
    if not live:
        return None
    return next((a for a in live if a.is_primary), live[0])
```

`gmail_accounts` columns of interest:

| Column | Note |
|---|---|
| `user_id` | FK, `CASCADE`, indexed |
| `email` | `unique=True` **globally** — one Gmail address may belong to only one TalentPing user |
| `google_sub` | `unique=True` — the upsert key in the callback |
| `is_primary` | `default=True`; set to `False` in the callback when a live account already exists (`routers/gmail.py:135`) |
| `status` | `connected` / `revoked` / `error` |
| `daily_send_count`, `daily_count_reset_at`, `last_used_at` | per-mailbox send accounting |
| `warmup_started_at`, `sent_total`, `bounce_count`, `complaint_count` | per-mailbox reputation ledger |
| `paused_until`, `pause_reason` | per-mailbox guardrail pause |

**What is missing is not a table. It is a link from the work to the mailbox.**
`email_threads` has `application_id`, `gmail_thread_id`, `subject`,
`message_count`, `last_message_at` — and no `gmail_account_id`. A Gmail thread
id is only meaningful inside the mailbox that holds it, so a thread row that
does not name its mailbox is ambiguous the moment there are two.

### 2.3 Where "one account" is hardcoded

Eleven call sites read `user.primary_gmail`. They are not equally harmless.

**Broken today if a second account exists (correctness bugs, not limitations):**

| # | Site | What goes wrong |
|---|---|---|
| B1 | `tasks/email_tasks.py:197` | The *only* sender in the product. Every outreach, every follow-up, every approved reply and every auto-reply goes out from the primary — including a reply to a recruiter who wrote to mailbox B. The recruiter gets an answer from an address they never contacted, breaking their own threading and reading as a spoof. |
| B2 | `tasks/inbox_tasks.py:272-274` | `poll_thread` reads a thread with the primary's credentials. A thread that lives in mailbox B returns Gmail 404 as an `HttpError`, and the handler only catches `GmailNotConfigured` — so the task raises rather than degrading. Replies on that thread are never ingested. |
| B3 | `tasks/email_tasks.py:42-57` + `:201` | `_sent_in_last_24h(db, user.id)` counts sends across **all** the user's mailboxes and hands that number to `reputation_service.evaluate(account, …)`, which compares it against **one** mailbox's warm-up ceiling. Two fresh mailboxes at 5/day each share a single 5/day budget, and each blames the other. |
| B4 | `tasks/inbox_tasks.py:94-103` | `_push_covers` asks whether the **primary** has healthy push, then skips polling for a thread that may belong to mailbox B — which has no watch. That thread is neither pushed nor polled. Silent. |
| B5 | `tasks/recruiter_reply_tasks.py:101` | `scan_all_recruiter_inboxes` scans the primary only. A recruiter writing to a secondary mailbox is never detected at all. |
| B6 | `tasks/inbox_tasks.py:365` | A hard bounce on mailbox B's outreach is booked against the primary's `bounce_count`, and can pause the wrong mailbox. |

**Limited but not wrong:**

| # | Site | Effect |
|---|---|---|
| L1 | `routers/gmail.py:277,295` | `POST/DELETE /gmail/watch` acts on the primary only — no way to enable push on a second mailbox. |
| L2 | `routers/gmail.py:170` | `GET /gmail/status` lists every account but returns only the *primary's* `watch`, `push_healthy`. |
| L3 | `routers/recruiter_inbox.py:298` | "Scan now" scans the primary. |
| L4 | `routers/recruiter_inbox.py:498` | The stats push panel reports the primary's watch as if it were the user's. |
| L5 | `routers/tracker.py:76,95` | `GET /onboarding` returns a single `gmail_address`, which the UI prints as "Autopilot is running from …". |

**Also worth fixing while here (pre-existing, made much likelier by this feature):**

`recruiter_emails.gmail_account_id` is `ondelete="CASCADE"` on a *nullable*
column (`models/recruiter_email.py:119`). Disconnecting a mailbox therefore
**deletes every inbound recruiter message ever detected in it** — the detection
history, the classifier audit trail, the lot. Its sibling
`recruiter_scan_runs.gmail_account_id` is `SET NULL` for exactly this reason.
With one mailbox, disconnecting is rare and terminal anyway. With several,
removing a mailbox you no longer use is a routine act that silently destroys
inbox history.

### 2.4 Where the frontend blocks it

`frontend/src/pages/Setup.jsx:873-874`:

```jsx
if (status.gmail_connected)
  return <DisconnectRow address={status.gmail_address} onDisconnect={disconnect} />;
```

Once one mailbox is connected the "Connect Gmail" button is gone, permanently.
There is no way to reach `api.gmailAuthorize()` again. That single early return
is the whole user-facing blocker.

Everything below it is *half* ready: `DisconnectRow` already `.map()`s over
`status.accounts` and renders a chip per account with its own disconnect
button (`Setup.jsx:911-926`). It just never receives more than one, and there is
no "make this the sender" control because no endpoint exists to set one.

`PushToggle` (`Setup.jsx:940`) reads the single `status.watch` — one switch for
what is really N subscriptions. `StartAutopilot` (`Setup.jsx:1128,1141`) prints
"running from {gmail_address}" / "send from {gmail_address}", singular.

API client surface today (`frontend/src/lib/api.js:104-112`):

```js
gmailStatus, gmailAuthorize, gmailDisconnect(id),
enableGmailPush, disableGmailPush
```

### 2.5 Second-order problem: the OAuth URL will not offer the chooser

Even with an "Add another" button, `prompt=consent` alone
(`services/google_oauth.py:142`) re-consents whichever Google account the
browser is already signed into. A user with one active Google session presses
"Add another mailbox", sees a consent screen for the account they already
connected, approves it, and the callback finds the existing `google_sub` and
updates the row in place. The button appears to do nothing. This must be fixed
in the same change as the button, or the button is a bug report.

---

## 3. Proposed changes — backend model

### 3.1 New column: `email_threads.gmail_account_id`

```python
# models/email_thread.py
# Which connected mailbox this conversation lives in. A Gmail thread id is only
# meaningful inside the mailbox that holds it, so a thread that does not name
# its mailbox cannot be polled, replied to, or have its attachments fetched
# once the user has more than one. Nullable because every row written before
# this column existed belongs to whatever was primary at the time — see
# `resolve_account`, which falls back rather than guessing wrong.
gmail_account_id: Mapped[int | None] = mapped_column(
    ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
)
```

This one column is the spine of the feature. It is what B1, B2, B4 and B6 all
resolve against, and it is written at exactly two places — the two places that
create an `EmailThread`:

- `services/outreach_service.py:122` — outbound campaign thread. Takes the
  mailbox chosen for the campaign/profile.
- `services/recruiter_reply_service.py:466` — inbound recruiter thread. Takes
  `row.gmail_account_id`, which is **already recorded** on the `RecruiterEmail`.
  This is the fix for the worst bug (B1) and it needs no new inference at all.

`SET NULL`, not `CASCADE`: disconnecting a mailbox must not delete the
conversation history that happened in it.

### 3.2 New column: `profiles.gmail_account_id`

```python
# models/profile.py
# The mailbox outreach for this profile goes out from. Null means the user's
# primary. Profiles already answer "which kind of role am I going after"; for a
# candidate keeping a consulting identity apart from a staff-role identity,
# "from which address" is the same question.
gmail_account_id: Mapped[int | None] = mapped_column(
    ForeignKey("gmail_accounts.id", ondelete="SET NULL"), index=True
)
```

See §4.1 for why the routing rule hangs off `Profile` rather than `Campaign` or
`AutopilotPreference`.

### 3.3 Fix the CASCADE on `recruiter_emails.gmail_account_id`

`CASCADE` → `SET NULL`, matching `recruiter_scan_runs`. Independently correct,
and required before "remove a mailbox" becomes a routine action.

### 3.4 New resolver, replacing `user.primary_gmail` at the broken sites

```python
# services/gmail_accounts.py (new)

def live_accounts(user: User) -> list[GmailAccount]:
    """Connected mailboxes, primary first, then oldest first."""

def resolve_for_thread(db: Session, thread: EmailThread) -> GmailAccount | None:
    """The mailbox this conversation belongs to.

    In order: the thread's own `gmail_account_id`; the address an inbound
    message on it was delivered to (the `_account_for_inbound` rule, which is
    what makes pre-migration rows work); the user's primary. Never silently
    returns a mailbox that is not `connected`.
    """

def resolve_for_new_outreach(
    db: Session, user: User, *, profile: Profile | None = None
) -> GmailAccount | None:
    """The mailbox to start a new conversation from: the profile's, else primary."""

def set_primary(db: Session, user: User, account_id: int) -> GmailAccount:
    """Move the sending identity, clearing the flag on every sibling."""
```

`user.primary_gmail` stays — it is the correct answer for "the user's default
sending identity" and is what `resolve_*` falls back to. What changes is that
per-thread and per-mailbox work stops calling it.

### 3.5 Per-mailbox reputation accounting

`_sent_in_last_24h(db, user_id)` (`tasks/email_tasks.py:42`) becomes
`_sent_in_last_24h(db, user_id, account_id)`, filtered on the sending mailbox.
Because `Email.from_address` is only stamped *after* a successful send
(`tasks/email_tasks.py:263`), the count must join through
`email_threads.gmail_account_id` rather than match on the address string —
which is the second reason §3.1's column has to exist.

`reputation_service` itself needs no change: it is already pure, already takes
one `GmailAccount`, and already reads only that row's counters. It has simply
been fed a user-wide number.

`services/send_policy.py` stays **per-user**. Its ceiling governs how much
unreviewed mail the product sends in the candidate's name — that is a property
of the person, not of a mailbox, and per-mailbox accounting there would let two
mailboxes double the amount of unread outreach. Deliberate asymmetry; worth a
comment in the code so it does not read as an oversight.

### 3.6 Per-mailbox background work

| Task | Change |
|---|---|
| `poll_thread` | Resolve via `resolve_for_thread`. Catch `HttpError` 404 → mark and skip rather than raise (B2). |
| `poll_all_inboxes` / `_push_covers` | Ask whether **this thread's** mailbox has healthy push (B4). |
| `scan_all_recruiter_inboxes` | Loop `live_accounts(user)`, one `scan_mailbox` task per mailbox. Debounce (`recently_scanned`) is currently keyed on `RecruiterReplyPreference.last_scan_at`, one row per user — it must become per-mailbox, or the first mailbox scanned suppresses the rest (B5). |
| `renew_gmail_watches` | Already iterates `GmailWatch` rows, not users. No change. |
| bounce recording | Book against the thread's mailbox (B6). |

The `recently_scanned` detail is easy to miss and would make multi-mailbox
scanning look like it works while only ever reading one mailbox per cycle.
Options: move `last_scan_at`/`detected_count` onto `gmail_accounts`, or add a
small `(user_id, gmail_account_id) → last_scan_at` row. The former is fewer
moving parts and the counters are already mailbox-shaped.

### 3.7 OAuth: offer the account chooser

```python
"prompt": "select_account consent",
```

`select_account` makes Google show the picker even when a session already
exists. Optionally pass `login_hint` when the user names an address. Scopes are
unchanged, so **no new OAuth verification review is required** — the same
client, the same consent screen, one more grant.

The callback should also distinguish "connected X" from "reconnected X" so
re-authorizing an existing mailbox does not read as adding one.

---

## 4. Design decisions

### 4.1 Where the routing rule lives

| Option | Verdict |
|---|---|
| Per-user primary only | Status quo. Multiple accounts would be receive-only. Not enough. |
| **Per-profile (recommended)** | `Profile` already models "one candidate, several jobs they'd take" and carries its own roles, locations, salary and resume. "And its own from-address" is the same axis. Reuses a concept the user already understands and a screen that already exists (`ProfileManager` in setup step 2). |
| Per-campaign | Campaigns are mostly created *by* autopilot, not by the user. Exposing a from-address there means exposing campaigns, which the product deliberately keeps behind the scenes. |
| Per-recipient / per-domain | Real use case (a personal address for startups, a professional one for enterprises) but a rules engine's worth of UI for a first pass. Later, if asked for. |

Recommendation: **per-profile, with the user's primary as the fallback**, and
the primary always used when there is exactly one mailbox — so single-mailbox
users see no change whatsoever.

### 4.2 Inbound replies always answer from the mailbox that was written to

Not configurable, not overridable. A recruiter who emails
`jane.doe@gmail.com` and gets an answer from `jane.consulting@gmail.com` sees a
broken thread and an address they have no record of contacting — which is what
a spoof looks like. The `RecruiterEmail` row already knows the right answer.

### 4.3 No send-address rotation

Multiple mailboxes must **not** raise the user's total send volume by rotating
across them. It is the mechanics of the exact behaviour `reputation_service`
exists to prevent, it defeats the warm-up ramp by construction, and it puts a
real person's personal Gmail accounts at risk of suspension — accounts that are
theirs, not ours.

Concretely: each mailbox keeps its own independent warm-up ramp (which is
already how the ledger works), and `send_policy`'s auto-send ceiling stays
per-user (§3.5). Adding a mailbox buys you a *separate identity*, not extra
throughput. The UI copy should say so plainly at the point of adding one.

---

## 5. Proposed changes — API

### 5.1 Changed

| Endpoint | Change |
|---|---|
| `GET /gmail/authorize` | Add `select_account` to `prompt`. Optional `?email=` → `login_hint`. |
| `GET /gmail/status` | `push_configured` stays top-level (server fact). Move `watch`, `push_healthy` **onto each `GmailAccountOut`** — they are per-mailbox facts. Keep the top-level pair, populated from the primary, for one release so the current UI keeps working. |
| `GET /onboarding` | Keep `gmail_address` (primary) for compatibility; add `gmail_addresses: list[str]` and `gmail_account_count`. |
| `DELETE /gmail/accounts/{id}` | Keep the promote-a-replacement behaviour (`routers/gmail.py:197-210`). Add: refuse with 409 when this is the last connected mailbox **and** autopilot is active — silently disarming the product is worse than an error. |

### 5.2 New

| Endpoint | Purpose |
|---|---|
| `PATCH /gmail/accounts/{id}` | `{ "is_primary": true }` → `set_primary`. **The gap that most needs closing:** today the only way to change the sending identity is to disconnect the first mailbox. |
| `POST /gmail/accounts/{id}/watch` | Enable push on one mailbox. |
| `DELETE /gmail/accounts/{id}/watch` | Disable it. |
| `POST /gmail/accounts/{id}/scan` | Recruiter scan of one mailbox (per-mailbox form of `POST /recruiter-inbox/scan`). |

Keep `POST/DELETE /gmail/watch` as aliases acting on the primary, so nothing
breaks mid-deploy.

### 5.3 Schema additions

```python
# schemas/gmail.py
class GmailAccountOut(BaseModel):
    ...                                  # unchanged fields
    watch: GmailWatchOut | None = None   # this mailbox's subscription
    push_healthy: bool = False           # ...and whether it is actually delivering
    warmup: dict | None = None           # reputation_service.warmup_progress(account)
    paused_until: datetime | None = None
    pause_reason: str | None = None
```

`warmup_progress()` already exists and already returns exactly what a
per-mailbox panel needs (`services/reputation_service.py:242`). Surfacing it
per account is what stops "my second mailbox only sends 5 a day" reading as a
bug.

---

## 6. Proposed changes — frontend

### 6.1 `Setup.jsx` — the one required change

Delete the early return at `Setup.jsx:873-874` and render the connected list
*and* an "Add another mailbox" button. `ConnectEmail` becomes:

```
┌ Connected mailboxes ────────────────────────────────┐
│ ● jane@gmail.com          [sender] [instant] [ × ]  │
│   5 of 10 today · rising to 15 in 2 days            │
│ ● jane.consulting@gmail.com                         │
│   warming up · 3 of 5 today   [Make sender] [ × ]   │
│                                                     │
│ [+ Add another mailbox]                             │
│ Adding a mailbox gives you a second identity, not a │
│ higher send limit — each warms up on its own.       │
└─────────────────────────────────────────────────────┘
```

- `DisconnectRow` already maps over `status.accounts`. Promote it from chips to
  rows and give each one: sender badge / "Make sender" (`PATCH`), its own
  `PushToggle`, its warm-up line, and the existing disconnect `×`.
- `PushToggle` takes `account` instead of reading `status.watch`.
- The `useEffect` popup/postMessage plumbing (`Setup.jsx:807-831`) is unchanged
  — it already just means "a connection completed, refresh".
- Copy: the volume note above, and step-3 body text that stops implying one
  address.

### 6.2 Elsewhere

- `StartAutopilot` (`Setup.jsx:1128,1141`) — "from {gmail_address}" becomes
  "from your N mailboxes" / names the primary when there is one.
- `ProfileManager` — a mailbox selector per profile ("Send from"), defaulting to
  "your main mailbox". Only render the control when more than one is connected,
  so single-mailbox users see nothing new.
- `lib/api.js` — add `gmailSetPrimary(id)`, `enableGmailPush(id)`,
  `disableGmailPush(id)`, `scanMailbox(id)`.
- Inbox / recruiter inbox — show which mailbox a thread lives in once there are
  two. Without it, two conversations with the same recruiter from different
  addresses are indistinguishable.

### 6.3 Test that needs a look

`frontend/src/pages/Setup.test.jsx:795-802` asserts no `/Connect Gmail/` button
before the step is reachable — still valid. But the new "Add another mailbox"
label must not match that regex, or the assertion starts failing for the wrong
reason. Name it deliberately.

---

## 7. Migration plan

One Alembic revision on top of `d4a8f2e61c93`. All three columns are nullable,
so it is additive and reversible.

```python
# alembic/versions/xxxx_multiple_gmail_accounts.py
def upgrade():
    op.add_column("email_threads",
        sa.Column("gmail_account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(..., "email_threads", "gmail_accounts",
        ["gmail_account_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_email_threads_gmail_account_id", "email_threads",
        ["gmail_account_id"])

    op.add_column("profiles",
        sa.Column("gmail_account_id", sa.Integer(), nullable=True))
    # ...FK + index

    # Backfill: every existing thread belongs to whatever was primary. With one
    # mailbox per user today this is exact, not a guess.
    op.execute("""
        UPDATE email_threads SET gmail_account_id = (
            SELECT ga.id FROM gmail_accounts ga
            JOIN applications a ON a.user_id = ga.user_id
            WHERE a.id = email_threads.application_id
              AND ga.status = 'connected'
            ORDER BY ga.is_primary DESC, ga.id
            LIMIT 1
        )
    """)

    # Pre-existing data-loss fix — see §2.3.
    # SQLite cannot ALTER a constraint; use batch_alter_table.
    with op.batch_alter_table("recruiter_emails") as batch:
        batch.drop_constraint("fk_recruiter_emails_gmail_account_id",
                              type_="foreignkey")
        batch.create_foreign_key("fk_recruiter_emails_gmail_account_id",
                                 "gmail_accounts", ["gmail_account_id"], ["id"],
                                 ondelete="SET NULL")
```

Note the SQLite constraint on the third block: dev and tests run on SQLite,
prod on Postgres, and the initial migration may not have named that FK. Check
the actual constraint name on the Postgres side before writing this, and use
`batch_alter_table` so the SQLite path works.

### Rollout order

The correctness fixes are worth shipping **before** the button, because they are
latent bugs that go live the instant anybody connects a second mailbox.

| Phase | Ships | Observable change |
|---|---|---|
| 1 | Migration + backfill + `services/gmail_accounts.py` + write `gmail_account_id` at the two thread-creation sites | None. Groundwork. |
| 2 | Fix B1–B6 to resolve per-thread / per-mailbox; per-mailbox `_sent_in_last_24h`; per-mailbox scan debounce; `recruiter_emails` FK | None for single-mailbox users. This is the phase whose tests matter most. |
| 3 | API: `PATCH` primary, per-account watch/scan, per-account fields on `GmailAccountOut`, `select_account` | Endpoints exist, unused. |
| 4 | Frontend: add-another button, per-mailbox rows, "Make sender", per-profile selector | The feature appears. |

Phases 1–3 are independently deployable and individually invisible, so a
problem in any of them is caught before a user can create the state that
exercises it.

### Backward compatibility

- Single-mailbox users: no behaviour change at any phase. Every resolver falls
  back to `primary_gmail`, which is the same account it always was.
- Threads with `gmail_account_id IS NULL` (rows written between deploy and
  backfill, or after a mailbox is removed) resolve via delivery address, then
  primary — the `_account_for_inbound` rule, which already ships and already
  works.
- Old clients keep working through phase 3 thanks to the retained top-level
  `watch` / `push_healthy` / `gmail_address` fields and the `/gmail/watch`
  aliases.

---

## 8. Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | **Mail goes out from the wrong address.** The single worst outcome: it breaks the recipient's threading, looks like spoofing, and damages the reputation of a mailbox the user did not choose. | High | Phase 2 before phase 4. `resolve_for_thread` is a pure function with a test per branch. Pin the inbound case with a test that connects two mailboxes, delivers to the second, and asserts the reply's `from_address`. |
| R2 | **Warm-up gets defeated or double-counted.** Left as-is (B3), two mailboxes share one budget. Fixed carelessly, a user gets 2× the cold-outreach volume from one ramp. | High | Per-mailbox rolling count keyed on `email_threads.gmail_account_id`; `send_policy` stays per-user (§3.5) as the user-level backstop. Test both: two mailboxes each get their own ramp, and the auto-send ceiling still bites across both. |
| R3 | **"Add another" appears to do nothing** because Google re-consents the signed-in account (§2.5). | Medium | `prompt=select_account consent` ships in the same change as the button. Callback distinguishes connected from reconnected. Manual check with two real Google sessions — this cannot be verified by unit test. |
| R4 | **Gmail API project quota scales with mailbox count.** Watches, history calls, scans and polls all multiply per connected mailbox against a project-level quota. | Medium | Per-mailbox scan debounce (§3.6) is the main lever, and push already suppresses polling per mailbox. Consider a configurable cap on mailboxes per user (`MAX_GMAIL_ACCOUNTS`, default 3) — cheap to add now, awkward to retrofit. Watch the quota dashboard after rollout. |
| R5 | **Disconnect destroys inbox history** (§2.3, pre-existing CASCADE), and multi-mailbox makes disconnecting routine. | Medium | The FK change is in the same migration. |
| R6 | **Deleting the last mailbox silently disarms autopilot.** Already possible; more confusing with several. | Low | 409 on removing the last connected mailbox while autopilot is active. |
| R7 | **`gmail_accounts.email` is globally unique**, so a shared address (a couple sharing one Gmail, a candidate with two TalentPing logins) still cannot be connected twice — and the callback's error is about "another user". | Low | Keep the constraint; it is a real safety property. Make sure the message stays accurate as accounts multiply. |
| R8 | **Push and scanning per mailbox multiply failure surface** — one mailbox's failed watch must not read as "replies have stopped". | Low | Per-account `watch` on `GmailAccountOut` and per-row status in the UI. `due_for_renewal` already iterates watches and already retries failed ones. |
| R9 | Users read "second mailbox" as "double the sending". | Low | The copy in §6.1 says otherwise at the point of adding. |

---

## 9. Testing

Every item below is a test the current suite does not have.

**Backend**

- `resolve_for_thread`: thread's own column → delivery address → primary →
  `None` when nothing is connected. One test per branch.
- Two connected mailboxes, recruiter writes to the **second**: the queued reply
  sends from the second, with the second's credentials, and `from_address`
  records it. (Pins R1 / B1.)
- `poll_thread` on a thread owned by the non-primary mailbox uses that
  mailbox's credentials; a Gmail 404 is handled, not raised. (B2.)
- Warm-up: two mailboxes, each with its own ramp, do not consume each other's
  daily allowance. (B3 / R2.)
- `send_policy`'s auto-send ceiling still applies across both mailboxes
  combined. (§3.5.)
- `_push_covers`: primary has healthy push, secondary does not — a thread in the
  secondary is still polled. (B4.)
- `scan_all_recruiter_inboxes` enqueues one scan per live mailbox, and the
  debounce does not let the first suppress the rest. (B5.)
- A hard bounce on the secondary's outreach increments *its* `bounce_count`. (B6.)
- `PATCH /gmail/accounts/{id}` moves `is_primary` and clears it on siblings;
  rejects an account belonging to another user with 404.
- `DELETE` the last connected mailbox with autopilot active → 409.
- Disconnecting a mailbox leaves its `recruiter_emails` rows in place with
  `gmail_account_id` nulled. (R5.)
- Migration backfill: a pre-existing thread resolves to the account that was
  primary.

**Frontend**

- With one account connected, "Add another mailbox" is present and calls
  `gmailAuthorize`.
- With two, both render, exactly one carries the sender badge, and "Make sender"
  on the other calls `gmailSetPrimary`.
- Each row's push toggle calls the per-account endpoint with that account's id.
- The per-profile "Send from" selector is absent with one mailbox and present
  with two.

**Manual (not unit-testable)**

- Two real Google sessions in one browser: "Add another" reaches the chooser and
  connects the *second* account (R3).
- Reconnecting an already-connected mailbox says "reconnected", not "connected".

---

## 10. Open questions for review

1. **Cap the number of mailboxes?** A `MAX_GMAIL_ACCOUNTS` default of 3 is cheap
   now and awkward later (R4). Worth having, or unnecessary ceremony?
2. **Per-profile routing, or something simpler for v1?** A single "send from"
   choice per user plus the inbound rule (§4.2) would fix every correctness bug
   and deliver most of the value with no `profiles` column. Per-profile could
   follow. This is the main scope decision in the doc.
3. **Should adding a mailbox reset the recruiter-scan preference to "watching"?**
   `RecruiterReplyPreference` is one row per user; if the user opted in, does a
   newly-added mailbox get watched automatically, or does it need its own
   opt-in? Watching a mailbox the user added for *sending* may be more than they
   asked for.
4. **Non-Gmail providers** are out of scope (§1.3) — but if IMAP/Outlook is on
   the near roadmap, `gmail_accounts` is the wrong table name to build a second
   feature onto, and renaming it to `email_accounts` is far cheaper before this
   change than after.
