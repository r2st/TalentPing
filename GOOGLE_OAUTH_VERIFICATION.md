# Google OAuth Verification — Scope Justification for job.doaide.com

**Google Cloud Project:** gen-lang-client-0768076486
**OAuth Client Owner:** sumaninster18@gmail.com
**Application URL:** https://job.doaide.com
**Application Name:** TalentPing (branded as DoAide AutoApply)
**Date:** 2026-10-06

---

## Table of Contents

1. [Application Overview](#1-application-overview)
2. [Scopes Requested](#2-scopes-requested)
3. [Scope Justification — gmail.send](#3-scope-justification--gmailsend)
4. [Scope Justification — gmail.readonly](#4-scope-justification--gmailreadonly)
5. [Why Narrower Scopes Cannot Replace These](#5-why-narrower-scopes-cannot-replace-these)
6. [Scope Alignment Audit](#6-scope-alignment-audit)
7. [Test Account Setup Instructions](#7-test-account-setup-instructions)
8. [Demo Video Script](#8-demo-video-script)

---

## 1. Application Overview

TalentPing (job.doaide.com) is an AI-powered job-application platform that sends
personalized recruiter outreach on behalf of job-seeking candidates. The candidate
connects their own Gmail account via OAuth, and the application:

1. **Sends outreach emails** as the candidate (from their own address) to recruiters,
   preserving the candidate's SPF/DKIM/DMARC alignment so the mail lands in the
   inbox rather than spam.
2. **Monitors the candidate's inbox** for recruiter replies, classifies them by
   intent (interested, rejection, request-for-info, etc.), and drafts AI-powered
   responses for the candidate to review before sending.
3. **Sends follow-up and reply emails** in existing threads when the candidate
   approves drafted responses.

The user is always in control: every outgoing email passes through a review queue
where the candidate can edit, approve, or dismiss it before it is sent.

---

## 2. Scopes Requested

The OAuth consent screen requests five scopes. Three are non-sensitive identity
scopes; two are sensitive Gmail scopes that require verification.

| Scope | Classification | Purpose |
|-------|---------------|---------|
| `openid` | Non-sensitive | Identify which Google account is being connected |
| `https://www.googleapis.com/auth/userinfo.email` | Non-sensitive | Read the user's email address for account linking |
| `https://www.googleapis.com/auth/userinfo.profile` | Non-sensitive | Read the user's display name for the "From" header |
| **`https://www.googleapis.com/auth/gmail.send`** | **Sensitive** | Send emails as the candidate via the Gmail API |
| **`https://www.googleapis.com/auth/gmail.readonly`** | **Restricted** | Read incoming emails to detect and display recruiter replies |

**Code reference:** `backend/app/services/google_oauth.py`, lines 33–39.

---

## 3. Scope Justification — gmail.send

### What it does

`gmail.send` allows the application to send emails as the connected user via the
Gmail API's `users.messages.send` endpoint.

### Features that use it

#### 3a. Sending initial outreach emails

**Code path:** `backend/app/tasks/email_tasks.py` → `gmail_service.send_email()`

When the candidate approves a drafted outreach email from the review queue, the
application calls `users().messages().send()` to deliver it from the candidate's
own Gmail address. This is the core product functionality — each email is
individually composed by an LLM using the candidate's profile and the target
recruiter's context.

The email includes:
- A personalized body addressed to the specific recruiter
- The candidate's resume and/or cover letter as attachments
- RFC 2369 / RFC 8058 `List-Unsubscribe` headers for CAN-SPAM compliance
- A CAN-SPAM footer with physical address and opt-out link

#### 3b. Sending follow-up emails in existing threads

**Code path:** `backend/app/tasks/email_tasks.py` → `gmail_service.send_email()`

When a recruiter has not replied within a configurable window, the system drafts a
follow-up email. The candidate reviews and approves it, and it is sent via the same
`users().messages().send()` call, threaded onto the original conversation using
Gmail's `threadId`.

#### 3c. Sending reply emails to recruiter responses

**Code path:** `backend/app/tasks/email_tasks.py` → `gmail_service.send_email()`

When a recruiter replies to outreach, the system detects the reply (via
`gmail.readonly`), classifies the intent, and drafts an appropriate response. The
candidate reviews the draft, optionally edits it, and approves it for sending. The
reply is sent with proper `In-Reply-To` and `References` headers for threading.

#### 3d. Sending weekly digest emails

**Code path:** `backend/app/services/digest_service.py` → `gmail_service.send_email()`

A weekly digest summarizing campaign activity (applications sent, replies received,
upcoming follow-ups) is sent from the candidate's own mailbox to their own address.
This uses `gmail.send` because the digest is delivered through the same Gmail API
path as all other mail.

#### 3e. Reading back the RFC 5322 Message-ID after sending

**Code path:** `gmail_service._sent_message_id()` → `users().messages().get()`

After each send, the application reads back the sent message to capture the
`Message-ID` header that Gmail assigned. This is necessary for proper email
threading: subsequent replies must reference this ID in their `In-Reply-To` header.
Gmail rewrites the `Message-ID` on submission, so the only way to get the
authoritative value is to read it back after sending.

### Why gmail.send is necessary

The application's entire value proposition depends on sending email as the candidate
from their own address. Sending from a shared relay or the application's own domain
would:

- Fail SPF/DKIM/DMARC checks (the email would not come from the candidate's domain)
- Land in spam (recruiters' mail servers would reject or filter it)
- Look impersonal (the "From" address would not be the candidate's)
- Prevent threading (replies would go to the relay, not the candidate's inbox)

---

## 4. Scope Justification — gmail.readonly

### What it does

`gmail.readonly` allows the application to read (but not modify, delete, or
organize) the contents of the connected mailbox via the Gmail API.

### Features that use it

#### 4a. Polling for recruiter replies on outreach threads

**Code path:** `backend/app/tasks/inbox_tasks.py` → `gmail_service.list_thread_messages()`

The application periodically polls each outreach thread to check for new messages
from the recruiter. This uses `users().threads().get()` to fetch the full thread
and detect new messages added since the last check.

**API call:** `users().threads().get(userId="me", id=thread_id)`

#### 4b. Push-notification-driven ingestion (Gmail Pub/Sub)

**Code path:** `backend/app/tasks/inbox_tasks.py` → `gmail_service.list_history()`

When Gmail push notifications are enabled (via `users().watch()`), the application
receives real-time notifications of new mail. It then calls
`users().history().list()` to fetch the changes since the last known state, which
identifies new messages that need processing.

**API calls:**
- `users().watch()` — register for push notifications (scoped to INBOX)
- `users().stop()` — unregister push notifications
- `users().history().list()` — fetch changes since a history ID

#### 4c. Scanning the full mailbox for inbound recruiter messages

**Code path:** `backend/app/services/inbound_scanner.py` → `gmail_service.list_messages()`, `gmail_service.get_message()`

The inbound scanner reads the candidate's mailbox to find emails from recruiters
that the candidate did not initiate (unsolicited recruiter outreach). It uses
`users().messages().list()` with a Gmail search query to find recent messages, then
`users().messages().get()` to fetch the full content of each candidate message for
classification.

**API calls:**
- `users().messages().list(userId="me", q=query, maxResults=...)` — list message IDs matching a search
- `users().messages().get(userId="me", id=message_id)` — fetch a full message

#### 4d. Extracting email body text for classification and display

**Code path:** `backend/app/services/gmail_service.py` → `extract_plain_text()`

Every fetched message's body is extracted (plain text preferred, with HTML-to-text
fallback) for:
- Intent classification (interested, rejection, scheduling, etc.)
- Display in the candidate's inbox view
- Context for AI-drafted reply composition

#### 4e. Listing and downloading inbound attachments

**Code path:** `backend/app/services/email_attachments.py` → `gmail_service.get_attachment()`

When a recruiter attaches documents (job descriptions, contracts, scheduling links),
the application lists them via `list_attachments()` (parsed from the message
resource) and downloads their content via `users().messages().attachments().get()`
so the candidate can view them in the inbox.

**API call:** `users().messages().attachments().get(userId="me", messageId=..., id=...)`

### Why gmail.readonly is necessary

The application must be able to:

1. **Detect recruiter replies** — without reading the mailbox, the application has no
   way to know when a recruiter has responded to outreach.
2. **Read message content** — the reply's body is needed to classify intent and draft
   an appropriate response.
3. **Read message headers** — `From`, `Subject`, `Message-ID`, `In-Reply-To`, and
   `References` headers are essential for proper threading and sender identification.
4. **Read attachments** — recruiters often send job descriptions, contracts, or
   scheduling links that the candidate needs to see.
5. **Detect unsolicited recruiter mail** — the inbound scanner finds opportunities
   the candidate didn't initiate, which is a core product feature.

---

## 5. Why Narrower Scopes Cannot Replace These

### Why not gmail.compose instead of gmail.send?

`gmail.compose` does not exist as a standalone scope. `gmail.send` is the narrowest
scope that permits sending email via the API.

### Why not gmail.metadata instead of gmail.readonly?

`gmail.metadata` allows reading only message metadata (headers, labels, size) but
**not message bodies or attachments**. TalentPing requires:

- **Message bodies** — to classify recruiter intent (interested vs. rejection vs.
  request-for-info), to display conversation history in the inbox, and to provide
  context for AI reply drafting.
- **Attachments** — to let candidates view job descriptions, contracts, and other
  files recruiters send.

Without body access, the application cannot perform its core function of
understanding and responding to recruiter communications.

### Why not gmail.labels or gmail.modify?

The application does **not** request `gmail.modify` or `gmail.labels`. It only
reads mail; it never modifies read/unread state, adds/removes labels, moves
messages, or deletes anything in the connected mailbox. `gmail.readonly` is the
narrowest read scope that includes message bodies.

### Why not gmail.addons.current.message.readonly?

This scope is limited to the currently open message in a Gmail add-on context. It
is not applicable to a web application that processes mail in the background.

### Summary of scope narrowing analysis

| Narrower Scope | Why It Cannot Replace | What Would Break |
|---------------|----------------------|-----------------|
| `gmail.metadata` | No body/attachment access | Reply detection, intent classification, inbox display, attachment viewing |
| `gmail.labels` | Labels only, no message content | Everything that reads mail |
| `gmail.addons.current.message.readonly` | Add-on context only | Background polling, push notifications, batch scanning |
| No `gmail.send` alternative exists | `gmail.send` is already the narrowest send scope | — |

**Conclusion:** `gmail.send` + `gmail.readonly` is the minimum viable scope set.
The application does not request `gmail.modify` (which would allow deleting,
labeling, or marking messages as read) because it does not need to change any state
in the user's mailbox.

---

## 6. Scope Alignment Audit

### Scopes in the codebase

**File:** `backend/app/services/google_oauth.py`, lines 33–39

```python
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
```

### What should be in Google Cloud Console

The OAuth consent screen in Google Cloud Console (project `gen-lang-client-0768076486`)
must list exactly these scopes:

1. `openid`
2. `https://www.googleapis.com/auth/userinfo.email`
3. `https://www.googleapis.com/auth/userinfo.profile`
4. `https://www.googleapis.com/auth/gmail.send`
5. `https://www.googleapis.com/auth/gmail.readonly`

### Verification checklist

- [ ] **Console scopes match code** — the five scopes above are listed on the OAuth
  consent screen configuration, with no extras.
- [ ] **No gmail.modify** — the codebase uses only `gmail.send` and `gmail.readonly`.
  Confirmed: `gmail.modify` does not appear in `google_oauth.py` or anywhere as a
  requested scope.
- [ ] **Redirect URI matches** — the console's authorized redirect URI is
  `https://job.doaide.com/api/v1/gmail/callback`, matching `GOOGLE_OAUTH_REDIRECT_URI`.
- [ ] **Application type** — Web application (not "Desktop app").
- [ ] **User type** — External.

### Gmail API methods used and their required scopes

| API Method | Requires | Used By |
|-----------|----------|---------|
| `users().messages().send()` | `gmail.send` | `send_email()` |
| `users().messages().get()` | `gmail.readonly` | `get_message()`, `_sent_message_id()` |
| `users().messages().list()` | `gmail.readonly` | `list_messages()` |
| `users().messages().attachments().get()` | `gmail.readonly` | `get_attachment()` |
| `users().threads().get()` | `gmail.readonly` | `list_thread_messages()` |
| `users().history().list()` | `gmail.readonly` | `list_history()` |
| `users().watch()` | `gmail.readonly` | `start_watch()` |
| `users().stop()` | `gmail.readonly` | `stop_watch()` |

**No methods requiring `gmail.modify`, `gmail.labels`, or `gmail.compose` are used.**

---

## 7. Test Account Setup Instructions

These instructions are for Google's OAuth verification review team to create a test
account and exercise the application's Gmail integration.

### Step 1: Create a test Gmail account

1. Go to https://accounts.google.com/signup
2. Create a new Gmail account (e.g., `talentping.reviewer.2026@gmail.com`)
3. Complete the account setup wizard

### Step 2: Add the test account to the OAuth client's test users

> **Note for the app owner (sumaninster18@gmail.com):** This step must be done
> before the reviewer can connect. Skip if the app is already published.

1. Go to https://console.cloud.google.com/
2. Select project `gen-lang-client-0768076486`
3. Navigate to **APIs & Services → OAuth consent screen**
4. Under **Test users**, click **Add users**
5. Enter the reviewer's Gmail address
6. Click **Save**

### Step 3: Register on job.doaide.com

1. Open https://job.doaide.com in a browser
2. Click **Get Started** or **Sign Up**
3. Register with any email address (this is the TalentPing account, separate from Gmail)
4. Verify the email if required
5. Complete the onboarding wizard:
   - Enter a name
   - Upload or paste a resume (any test resume will work)
   - Set job preferences (any values)

### Step 4: Connect a Gmail account

1. In the app, navigate to **Setup** (or the setup wizard's Gmail step)
2. Click **Connect Gmail**
3. A popup opens showing Google's OAuth consent screen
4. Select the test Gmail account
5. **Review the permissions carefully** — the consent screen shows:
   - "Read your email" (`gmail.readonly`)
   - "Send email on your behalf" (`gmail.send`)
   - "See your personal info" (identity scopes)
6. Click **Allow** (leave all permissions checked)
7. The popup closes and the app shows the connected mailbox

### Step 5: Verify the connection

1. The Setup page should show the Gmail address as "Connected"
2. Navigate to **Dashboard** — the connected mailbox appears in the sidebar

### Step 6: Exercise gmail.send — send a test outreach email

1. Import at least one recruiter contact:
   - Go to **Contacts** or the recruiter import section
   - Add a test contact (use your own secondary email as the recruiter)
2. Start a campaign targeting the test contact
3. The system generates a personalized outreach email
4. Review it in the **Review Queue**
5. Click **Approve** to send
6. Verify the email arrives in the test recruiter's inbox
7. Confirm the email is sent **from the connected Gmail address** (check headers)

### Step 7: Exercise gmail.readonly — receive and view a reply

1. From the test recruiter's email, reply to the outreach email
2. Wait up to 5 minutes (or click **Sync** in the inbox to poll immediately)
3. The reply appears in the app's **Inbox**
4. Verify:
   - The reply's full body text is visible (not just a snippet)
   - Any attachments sent by the recruiter are listed and downloadable
   - The system classifies the reply's intent (interested, rejection, etc.)
   - A draft response is generated for review

### Step 8: Verify no mailbox modification

1. Open the test Gmail account at https://mail.google.com
2. Confirm that:
   - No labels have been created by the app
   - No messages have been marked as read/unread by the app
   - No messages have been moved, archived, or deleted by the app
   - The only change is new sent messages (from gmail.send)

---

## 8. Demo Video Script

**Target length:** 3–5 minutes
**Resolution:** 1920×1080, 30fps
**Format:** Screen recording with voiceover

### Scene 1: OAuth Consent Screen (0:00–0:45)

1. Open https://job.doaide.com and log in to a test account
2. Navigate to **Setup**
3. Click **Connect Gmail**
4. **Pause on the consent screen** — show all requested permissions:
   - "See your personal info, including any personal info you've made publicly available"
   - "Read all resources and their metadata—no write operations"
   - "Send email on your behalf"

   **Voiceover:** "TalentPing requests two Gmail permissions: the ability to read
   your email so it can detect recruiter replies, and the ability to send email so
   it can deliver your outreach from your own address. It does not request
   permission to modify, delete, or organize your email."

5. Click **Allow**
6. Show the connected state on the Setup page

### Scene 2: Sending Outreach — gmail.send in action (0:45–1:45)

1. Show an existing campaign with queued outreach emails
2. Open one email in the **Review Queue**
3. **Show the full email** — body, subject, attachments, recipient
4. **Voiceover:** "Every outreach email is composed by AI using the candidate's
   resume and the recruiter's context. The candidate reviews it before it's sent —
   nothing leaves without explicit approval."
5. Click **Approve**
6. Show the email status change to "Sent"
7. **Switch to the Gmail web UI** — show the sent email in the Sent folder
8. **Voiceover:** "The email is sent from the candidate's own Gmail address.
   Checking the headers confirms it passed SPF, DKIM, and DMARC — it's
   indistinguishable from mail the candidate wrote manually."

### Scene 3: Receiving Replies — gmail.readonly in action (1:45–3:00)

1. **From a second browser/account**, send a reply to the outreach email
   (simulating a recruiter responding)
2. Switch back to job.doaide.com
3. Click **Sync** in the inbox (or wait for the automatic poll)
4. **Show the reply appearing** in the Inbox view
5. Open the thread — show the full conversation:
   - Original outreach (sent)
   - Recruiter's reply (received) — full body visible
6. **Voiceover:** "The application reads the recruiter's reply to classify its
   intent — is the recruiter interested? Asking for more information? The full
   message body is needed for this classification, which is why gmail.readonly
   is required rather than gmail.metadata."
7. Show the intent classification badge (e.g., "Interested")
8. Show the AI-drafted response in the review queue
9. **Voiceover:** "A response is drafted automatically and placed in the review
   queue. The candidate can edit it, approve it, or dismiss it."

### Scene 4: Attachments — gmail.readonly for file access (3:00–3:30)

1. **From the recruiter account**, send a reply with an attachment (e.g., a PDF
   job description)
2. Sync and show the attachment appearing in the inbox thread
3. Click to preview/download it
4. **Voiceover:** "Recruiters often send job descriptions, contracts, or
   scheduling details as attachments. The application reads these so the
   candidate can view them directly."

### Scene 5: No Mailbox Modification — proving the negative (3:30–4:15)

1. Open the test Gmail account at https://mail.google.com
2. Show the inbox — **no new labels created by TalentPing**
3. Show that received messages are still in their original read/unread state
4. Show the Sent folder — only the outreach emails sent by the app
5. Show Trash — **empty** (nothing deleted by the app)
6. **Voiceover:** "The application does not modify the user's mailbox in any way.
   It does not create labels, mark messages as read, move messages, or delete
   anything. The only change to the mailbox is new messages in the Sent folder,
   which is the result of gmail.send — not gmail.modify."

### Scene 6: Disconnecting — user control (4:15–4:45)

1. Go to **Setup** in job.doaide.com
2. Click the disconnect button next to the Gmail account
3. Show the confirmation dialog
4. Confirm disconnection
5. **Voiceover:** "The user can disconnect their Gmail account at any time. This
   revokes the OAuth grant at Google, and the application immediately loses all
   access to the mailbox. Users can also revoke access directly at
   myaccount.google.com/permissions."
6. Show the account returning to "Not connected" state

### Closing (4:45–5:00)

**Voiceover:** "To summarize: TalentPing uses gmail.send to deliver personalized
outreach from the candidate's own address, and gmail.readonly to detect and display
recruiter replies. These are the two narrowest scopes that support the product's
core functionality. No broader access is requested or needed."

---

## Appendix A: Complete Gmail API Call Inventory

Every Gmail API call made by the application, with its code location and required
scope:

```
SCOPE: gmail.send
  users().messages().send()
    → backend/app/services/gmail_service.py:1044  (send_email)
    Called by:
      - backend/app/tasks/email_tasks.py:631      (outreach, follow-ups, replies)
      - backend/app/services/digest_service.py:1242 (weekly digest to self)

SCOPE: gmail.readonly
  users().messages().get()
    → backend/app/services/gmail_service.py:936   (_sent_message_id — read-back after send)
    → backend/app/services/gmail_service.py:1248  (get_message — full message fetch)
    Called by:
      - backend/app/services/inbound_scanner.py:771   (fetch messages for classification)
      - backend/app/tasks/inbox_tasks.py (thread polling)

  users().messages().list()
    → backend/app/services/gmail_service.py:1107  (list_messages — search mailbox)
    Called by:
      - backend/app/services/inbound_scanner.py:676   (scan for recruiter mail)

  users().messages().attachments().get()
    → backend/app/services/gmail_service.py:1632  (get_attachment)
    Called by:
      - backend/app/services/email_attachments.py:1005 (download inbound attachments)

  users().threads().get()
    → backend/app/services/gmail_service.py:1064  (list_thread_messages)
    Called by:
      - backend/app/tasks/inbox_tasks.py:471      (poll threads for new replies)

  users().history().list()
    → backend/app/services/gmail_service.py:1205  (list_history — push notification delta)
    Called by:
      - backend/app/tasks/inbox_tasks.py:228      (ingest push notification)

  users().watch()
    → backend/app/services/gmail_service.py:1143  (start_watch — register push)
    Called by:
      - backend/app/services/gmail_push.py:175

  users().stop()
    → backend/app/services/gmail_service.py:1155  (stop_watch — unregister push)
    Called by:
      - backend/app/services/gmail_push.py:212
```

## Appendix B: Data Handling and Privacy

- **Refresh tokens** are encrypted at rest with Fernet (AES-128-CBC) before storage.
  The encryption key (`TOKEN_ENCRYPTION_KEY`) is a deployment secret.
- **Email content** is processed server-side for classification and reply drafting.
  Message bodies are stored only for active conversations the user is tracking.
- **No email is forwarded** to third parties. All processing happens on the
  application's own servers.
- **Users can disconnect** at any time via the app's UI, which triggers a
  best-effort token revocation at Google (`oauth2.googleapis.com/revoke`).
- **Users can also revoke** access independently at
  https://myaccount.google.com/permissions.
