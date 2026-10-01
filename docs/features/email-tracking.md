# Email Tracking — opens and clicks on outbound mail

**Status:** Implemented
**Last updated:** 2026-07-27
**Depends on:** Gmail send pipeline (shipped), analytics overview (shipped)

---

## 1. Overview

### 1.1 The gap

TalentPing knows two things about an outreach email today: that Gmail accepted
it, and whether a human wrote back. Everything between — did they open it, did
they click the portfolio link, did they open it four times and then go quiet —
is invisible.

That gap is why three other features on this roadmap are guessing:

* `follow_up_service.choose_template()` has an `OPENED_NO_REPLY` branch it
  selects by *step number*, because there is no open data to select it by.
* Subject-line A/B testing ([`subject-line-ab.md`](subject-line-ab.md)) needs an
  open rate to have anything to converge on.
* Analytics can rank resumes and companies by reply rate, but a 0% reply rate
  with a 60% open rate and a 0% open rate are completely different problems and
  the product cannot tell the user which one they have.

### 1.2 What this feature adds

```
compose ──► Email row (tracking_token minted)
      │
      ▼
send_outreach_email
      │  build_tracked_html(body, token)
      ▼
  multipart/alternative
    ├── text/plain  — the body exactly as before
    └── text/html   — same body + wrapped links + 1x1 pixel
      │
      ▼  recipient opens
  GET /api/v1/t/o/{token}.gif   ──► EmailEvent(OPEN)  ──► 1x1 transparent GIF
      │
      ▼  recipient clicks a link
  GET /api/v1/t/c/{token}?u=…   ──► EmailEvent(CLICK) ──► 302 to the target
      │
      ▼
  Email.open_count / click_count / first_opened_at  (denormalized counters)
      │
      ▼
  GET /api/v1/analytics/overview  ──► engagement block ──► Pipeline UI
```

### 1.3 Scope

**In scope:** pixel, link wrapping, a public tracking router, `EmailEvent`
persistence, denormalized counters on `Email`, engagement stats in the analytics
overview, and the Pipeline UI row that renders them.

**Out of scope:** tracking inbound mail, per-recipient device/geo profiling
(none of the user-agent parsing beyond a truncated string), and read receipts
requested from Gmail. Also out of scope: tracking on replies drafted by the
reply agent — this is outreach-only, so a conversation with a real human never
carries a pixel.

---

## 2. The honest limitations, stated up front

Open tracking is **directionally useful and individually unreliable**. This is
not a caveat to bury:

* **Gmail proxies images.** Every image in a message delivered to a Gmail
  recipient is fetched once by `googleusercontent.com`, often at delivery time
  rather than at read time. So an "open" from a Gmail address can mean "Google
  cached the image", not "a person looked at this".
* **Image blocking.** Outlook and many corporate gateways block remote images by
  default. A non-open is not a non-read.
* **Security scanners** click every link in a message to check it. That inflates
  click counts on the first few minutes after delivery.

Three mitigations, all implemented:

1. **Opens are deduplicated within `TRACKING_DEDUPE_WINDOW` (10 min)** per
   email. A proxy fetch and a human read seconds apart count once.
2. **A prefetch heuristic:** an open recorded within `PREFETCH_GRACE` (60 s) of
   `sent_at` is stored with `is_prefetch=True` and excluded from open *rates*.
   It is kept, not dropped — "the proxy fetched it" is still evidence of
   delivery.
3. **Rates are captioned, never presented as truth.** The UI labels the block
   "Engagement (approximate)" and the analytics payload carries
   `tracking_reliable: false` when the sample is below `MIN_SAMPLE`.

For A/B testing this is fine: the biases apply equally to both variants, so the
*comparison* survives even though the absolute number does not.

---

## 3. Data model

### 3.1 `email_events` (new)

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `user_id` | FK `users.id` CASCADE, indexed | Ownership. No `businessId` exists in this codebase; `user_id` is the scoping key everywhere (`routers/review._owned_draft`, `routers/inbox._owned_thread`). |
| `email_id` | FK `emails.id` CASCADE, indexed | |
| `event_type` | enum `OPEN` / `CLICK` | non-native VARCHAR, house style |
| `url` | Text, nullable | the click target; null for opens |
| `user_agent` | String(255), nullable | truncated |
| `ip_hash` | String(64), nullable | see §6 |
| `is_prefetch` | Boolean | proxy/scanner heuristic |
| `occurred_at` | DateTime(tz), indexed | |

### 3.2 New columns on `emails`

| Column | Type | Why denormalized |
|---|---|---|
| `tracking_token` | String(43) unique, nullable | URL-safe, 32 random bytes base64url. Not the row id — an enumerable pixel URL would let anyone inflate a stranger's stats. |
| `open_count` | Integer default 0 | The tracker page sorts and filters on these; a `COUNT(*)` over `email_events` per row is a join too far for a list endpoint that is polled. |
| `click_count` | Integer default 0 | |
| `first_opened_at` | DateTime(tz) nullable | |
| `last_opened_at` | DateTime(tz) nullable | |
| `first_clicked_at` | DateTime(tz) nullable | |

`email_events` remains the source of truth; the counters are derived and can be
rebuilt from it.

---

## 4. Sending a tracked message

`gmail_service._build_mime()` grows an optional `html_body`. When present the
message becomes `multipart/alternative` (or `multipart/mixed` wrapping an
`alternative` part when there are attachments), with the plain-text part
unchanged. **A message with tracking disabled produces byte-identical MIME to
what shipped before** — that property is asserted by a test, because the
deliverability work assumed single-part `text/plain` and a silent change there
would be expensive to debug.

`services/email_tracking.py` provides:

```python
def ensure_token(email: Email) -> str          # mint on first use, idempotent
def pixel_url(token: str) -> str
def wrap_link(token: str, url: str) -> str     # /t/c/{token}?u=<urlsafe b64>
def build_tracked_html(body_text: str, token: str) -> str
```

`build_tracked_html` escapes the body, converts it to `<p>`/`<br>` HTML,
rewrites bare URLs and `mailto:`-free absolute links through `wrap_link`, and
appends the pixel as the last element. Two URLs are never wrapped: the CAN-SPAM
unsubscribe link and the tracking URLs themselves — breaking one-click
unsubscribe to measure a click would be both illegal and stupid.

The link target travels as urlsafe-base64 in `?u=`, not as a raw query
parameter, so a target containing `&` or `#` survives the round trip.

---

## 5. The tracking endpoints

Public, unauthenticated, under the API v1 prefix — a recruiter's mail client has
no session:

```
GET /api/v1/t/o/{token}.gif   → 200 image/gif, 1x1 transparent, no-store
GET /api/v1/t/c/{token}       → 302 Location: <decoded ?u=>
```

Rules that make an unauthenticated write endpoint safe:

* **An unknown token is not an error.** Both endpoints return their normal
  response (pixel / redirect to the fallback) and write nothing. A 404 would
  turn the endpoint into an oracle for guessing valid tokens.
* **Nothing user-controlled is reflected.** The click endpoint validates the
  decoded target is `http`/`https` and refuses anything else, so it cannot be
  used as an open redirect to a `javascript:` or `data:` URL. A target that
  fails validation redirects to the app's frontend URL instead.
* **The row's `user_id` comes from the email, never from the request.**
* **Writes are counter increments only** — no way to reach another user's data.

---

## 6. Privacy

The recipient is a third party who did not sign up for this product, so:

* IP addresses are **never stored raw**. `ip_hash` is
  `sha256(ip + settings.jwt_secret)[:64]` — enough to deduplicate a repeat
  opener, useless for locating anyone, and unrecoverable if the table leaks.
* User agents are truncated to 255 chars and used only for the prefetch
  heuristic.
* Tracking is off per-deployment with `email_tracking_enabled=False`, which
  reverts sends to plain text with no pixel and no wrapped links.

---

## 7. Surfacing the stats

`GET /analytics/overview` gains an `engagement` block:

```json
{
  "engagement": {
    "tracked": 42, "opened": 27, "clicked": 6,
    "open_rate": 0.643, "click_rate": 0.143,
    "click_to_open_rate": 0.222,
    "reliable": true
  }
}
```

`tracked` counts sent emails that actually carried a token, so the rate's
denominator is never inflated by mail sent before tracking was switched on.
`reliable` is `tracked >= MIN_SAMPLE`.

The Pipeline page renders this as a three-stat row under the analytics headline,
captioned "approximate — image blocking and mail-proxy prefetch both distort
opens".

---

## 8. Testing

`tests/test_email_tracking.py`:

* token minting is idempotent and unique;
* `build_tracked_html` wraps `http(s)` links, leaves the unsubscribe URL alone,
  escapes HTML in the body, and always ends with the pixel;
* a `?u=` target containing `&`, `#` and unicode round-trips exactly;
* the open endpoint returns a GIF, writes one `EmailEvent`, and bumps the
  counters;
* a second open inside the dedupe window does not double-count; one outside does;
* an open within 60 s of `sent_at` is flagged `is_prefetch` and excluded from the
  analytics open rate;
* an unknown token still returns a pixel / redirect and writes nothing;
* a `javascript:` target is refused and redirected to the frontend URL;
* MIME with tracking disabled is byte-identical to the pre-feature build;
* the analytics engagement block computes rates off tracked sends only.
