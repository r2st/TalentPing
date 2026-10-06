# Reply to Google OAuth Verification Team — DoAide Jobs (job.doaide.com)

**To:** Google OAuth Verification Team
**From:** sumaninster18@gmail.com
**Subject:** Re: OAuth Verification — DoAide Jobs (job.doaide.com) — Scope Clarification and Justification
**Google Cloud Project:** gen-lang-client-0768076486

---

Hi,

Thank you for reviewing our OAuth verification request for DoAide Jobs (job.doaide.com). We'd like to address the questions raised in your review and clarify a scope discrepancy.

## 1. Scope Clarification — We Do NOT Use gmail.modify

We noticed the review references `gmail.modify`. To clarify: **our application does not request or use `gmail.modify`**. The only sensitive/restricted Gmail scopes we request are:

- **`gmail.send`** (sensitive)
- **`gmail.readonly`** (restricted)

Our codebase (`backend/app/services/google_oauth.py`) explicitly defines only these scopes, and a full-text search of our codebase confirms `gmail.modify` does not appear anywhere. We do not modify, delete, label, archive, or change the read/unread state of any message in the user's mailbox.

## 2. Scope Justification

### gmail.send — Why We Need It

DoAide Jobs is a job-application platform that sends personalized recruiter outreach **from the candidate's own Gmail address**. This is the core product — sending from the candidate's own address ensures:

- SPF/DKIM/DMARC alignment (emails land in inbox, not spam)
- Recruiter replies go directly to the candidate's inbox
- Proper email threading via Gmail's `threadId`

**Specific uses:**
- Sending initial outreach emails to recruiters (candidate-approved)
- Sending follow-up emails in existing threads
- Sending reply emails when the candidate approves an AI-drafted response
- Sending weekly activity digest emails (to the candidate's own address)

Every outgoing email goes through a review queue — **nothing is sent without explicit candidate approval**.

`gmail.send` is the narrowest scope that permits sending email via the Gmail API. There is no `gmail.compose` alternative.

### gmail.readonly — Why We Need It

We use `gmail.readonly` to detect and display recruiter replies so candidates can respond to them. Specific uses:

- **Reading email threads** — polling outreach threads for new recruiter replies (`users.threads.get`)
- **Fetching message content** — reading the full body of recruiter replies for AI-powered intent classification (interested, rejection, request-for-info, etc.) and display in the app's inbox (`users.messages.get`)
- **Listing messages** — searching the mailbox for inbound recruiter messages the candidate didn't initiate (`users.messages.list`)
- **Downloading attachments** — recruiters send job descriptions, contracts, and scheduling details that candidates need to view (`users.messages.attachments.get`)
- **Push notification registration** — registering for Gmail Pub/Sub notifications to receive real-time alerts when new mail arrives (`users.watch`, `users.history.list`)

### Why Narrower Scopes Won't Work

| Narrower Scope | Why It's Insufficient |
|---|---|
| `gmail.metadata` | Provides headers only — **no message bodies or attachments**. We cannot classify recruiter intent, display conversations, or draft replies without the message body. |
| `gmail.labels` | Labels only — no message content at all. |
| `gmail.addons.current.message.readonly` | Only works in the Gmail add-on context for the currently open message. Not applicable to a web application that processes mail in the background. |

**`gmail.readonly` is the narrowest read scope that includes message bodies.** We do not request `gmail.modify` because we never change anything in the user's mailbox.

## 3. Demo Video

We will record and submit a demo video (3–5 minutes) showing:

1. The OAuth consent screen with the exact permissions requested
2. Sending an outreach email (exercising `gmail.send`)
3. Receiving and viewing a recruiter reply (exercising `gmail.readonly`)
4. Viewing attachments sent by a recruiter
5. Proof that the app does NOT modify the mailbox (no labels created, no read/unread changes, no deletions)
6. Disconnecting the Gmail account

We will submit the video link as a follow-up within a few days.

## 4. Test Account Instructions

To test the application:

1. **Create a test Gmail account** (or use an existing one)
2. **Register at** https://job.doaide.com — click "Get Started", complete onboarding with any test data (name, resume, job preferences)
3. **Connect Gmail** — go to Setup, click "Connect Gmail", authorize on the consent screen
4. **Test gmail.send** — add a test recruiter contact (use a secondary email you control), start a campaign, review and approve the outreach email in the Review Queue, verify it arrives in the recruiter's inbox
5. **Test gmail.readonly** — reply to the outreach from the recruiter's email, sync in the app, verify the reply body, attachments, and intent classification are displayed
6. **Verify no mailbox modification** — check the test Gmail account at mail.google.com and confirm no labels were created, no messages were modified, and no messages were deleted

If you need us to add your test Gmail address to the OAuth test users list before the app is published, please let us know the address and we'll add it immediately.

## 5. Privacy and Security Summary

- OAuth refresh tokens are encrypted at rest (Fernet/AES-128-CBC)
- Email content is processed on our own servers only — never forwarded to third parties
- Users can disconnect at any time via the app UI (triggers token revocation) or at myaccount.google.com/permissions
- The privacy policy is available at https://job.doaide.com/privacy

---

Please let us know if you need any additional information, specific test credentials, or clarification on any of the above. We're happy to schedule a call if that would be helpful.

Best regards,
Suman Gangopadhyay
DoAide Jobs — job.doaide.com
