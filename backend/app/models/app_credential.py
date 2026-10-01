"""Deployment credentials, stored encrypted and editable from the dashboard.

Every credential this product needs — the Google OAuth client, the four LLM
provider keys — arrived only through the environment. Rotating one meant SSH
onto the box, edit ``/opt/TalentPing/.env``, restart three units. That is a
deploy for a value change, and it is why a closed Google Cloud project took
Gmail connectivity down until somebody with a key was at a keyboard.

A row here **overrides** the environment for its key; no row means the ``.env``
value stands (see :mod:`app.services.credential_store`). So this table is a set
of deltas rather than a replacement config: an install with an empty table
behaves exactly as it did before the table existed, and clearing an override
restores the deployed value rather than blanking it.

**Not per-user, deliberately.** These identify the *deployment* to a third
party, not a person to us: one Google Cloud project, one registered redirect
URI, one set of provider accounts billed together. Two things follow, and both
are why this has no ``user_id``:

- the OAuth callback resolves the client secret with nobody authenticated — it
  runs before any user context exists — as do the Celery beat sweeps;
- letting an ordinary user write ``google_client_id`` would let them point the
  consent flow at a client they control and harvest mailbox grants from every
  other user on the deployment.

Only an administrator (``users.role = 'admin'``) may read or write it.

Values are encrypted at rest with the same Fernet key as the Gmail refresh
tokens (:mod:`app.services.crypto`), because the column holds live client
secrets and provider keys — a database dump must not also be a credential dump.
"""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class AppCredential(Base, TimestampMixin):
    __tablename__ = "app_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    # A key from ``credential_store.MANAGED_CREDENTIALS``. That registry is an
    # allowlist, so this table can never become an arbitrary settings-write
    # primitive for whoever reaches the endpoint.
    key: Mapped[str] = mapped_column(
        String(100), unique=True, index=True, nullable=False
    )
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Email of the admin who last set it. Rotating a credential is exactly the
    # kind of change someone later has to be able to ask "who did this, and
    # when?" about.
    updated_by: Mapped[str | None] = mapped_column(String(320))

    def __repr__(self) -> str:  # pragma: no cover - never print the value
        return f"<AppCredential key={self.key!r}>"
