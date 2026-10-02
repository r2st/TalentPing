# Gmail connection setup

TalentPing sends each candidate's outreach through **their own Gmail account**,
over the Gmail API. That is what gives the mail real SPF/DKIM/DMARC alignment —
the practical reason cold outreach lands in an inbox rather than a spam folder —
and it means replies arrive in a mailbox the user already reads.

This is a one-time setup per deployment. Until it is done, step 1 of the wizard
shows "Gmail sign-in isn't configured on this server yet".

## 1. Create the OAuth client

In the [Google Cloud console](https://console.cloud.google.com/):

1. Create (or pick) a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen**:
   - User type: **External**.
   - Fill in app name, support email, developer contact.
   - Add these scopes:
     - `openid`
     - `.../auth/userinfo.email`
     - `.../auth/userinfo.profile`
     - `.../auth/gmail.send`
     - `.../auth/gmail.readonly`
   - While the app is in **Testing**, add each user's Gmail as a test user.
     Publishing requires Google verification because `gmail.readonly` is a
     restricted scope — expect a review if you go past ~100 users.
4. **Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application** (not "Desktop app").
   - Authorised redirect URI — must match `GOOGLE_OAUTH_REDIRECT_URI` exactly:
     - production: `https://job.doaide.com/api/v1/gmail/callback`
     - local: `http://localhost:8000/api/v1/gmail/callback`

Copy the client ID and secret.

## 2. Generate a token encryption key

Refresh tokens are long-lived credentials that can send mail as the user, so
they are encrypted at rest with Fernet. Generate the key once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Rotating this key invalidates every stored token and forces all users to
reconnect — treat it like a database password.

## 3. Set the environment

```dotenv
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=...
GOOGLE_OAUTH_REDIRECT_URI=https://job.doaide.com/api/v1/gmail/callback
TOKEN_ENCRYPTION_KEY=...
FRONTEND_URL=https://job.doaide.com
```

`FRONTEND_URL` is the origin the callback popup posts its result back to. If it
doesn't match the site the user is on, the popup will close but the wizard won't
advance until the next poll.

Restart the API afterwards:

```bash
systemctl restart talentping-api talentping-worker talentping-beat
```

## How the flow works

```
GET  /api/v1/gmail/authorize   (auth required)  -> { authorization_url, state }
     browser opens the URL in a popup
GET  /api/v1/gmail/callback?code&state (public) -> stores the account, closes popup
GET  /api/v1/gmail/status                       -> connection state for the wizard
```

The callback is necessarily public — Google redirects the browser there with no
`Authorization` header. It is safe because `state` is a Fernet-encrypted,
10-minute token carrying the id of the user who started the flow, so the
connected account can only ever be attached to that user.

`access_type=offline` and `prompt=consent` are always sent, which guarantees a
refresh token even when re-connecting an already-authorised account.

## Troubleshooting

**"Google did not return a refresh token"** — the account had a lingering grant.
Remove TalentPing at [myaccount.google.com/permissions](https://myaccount.google.com/permissions)
and connect again.

**`invalid_scope` on refresh** — the account was connected before a scope was
added. The granted scopes are recorded per account and replayed on refresh
(`google_oauth.credential_scopes_for`), so this self-heals when the user
reconnects; it should not happen otherwise.

**`redirect_uri_mismatch`** — `GOOGLE_OAUTH_REDIRECT_URI` and the URI registered
on the Google client differ. They must match character for character, including
scheme and trailing path.

**Popup closes but nothing happens** — `FRONTEND_URL` doesn't match the origin
the user is browsing. The wizard falls back to polling on popup close, so it
recovers within a second, but fix the setting.
