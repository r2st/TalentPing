"""User (job seeker) model."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.autopilot import AutopilotPreference
    from app.models.campaign import Campaign
    from app.models.digest import DigestPreference
    from app.models.gmail_account import GmailAccount
    from app.models.notification import Notification, NotificationPreference
    from app.models.profile import Profile
    from app.models.recruiter import Recruiter
    from app.models.recruiter_email import RecruiterEmail, RecruiterReplyPreference
    from app.models.resume import Resume

#: The two values ``users.role`` may hold. Not a database enum — see the column.
ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_ADMIN)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    oauth_provider: Mapped[str | None] = mapped_column(String(30))
    oauth_id: Mapped[str | None] = mapped_column(String(255))

    # ``user`` or ``admin``. Deliberately a string rather than a bool, because
    # the question this answers will not stay binary — but deliberately *not* an
    # Enum type either: adding a value to a Postgres enum is a migration, and
    # this column will grow a role long before it grows a schema change budget.
    #
    # An admin here is a **deployment** operator, not a tenant with extra
    # buttons. TalentPing has one Google Cloud project and one set of provider
    # keys for everybody, so what the role really grants is the ability to
    # re-point this installation at a different Google client — see
    # :mod:`app.services.credential_store`. That is why it is not something a
    # user can hold over their own account.
    role: Mapped[str] = mapped_column(
        String(20), default=ROLE_USER, server_default=ROLE_USER, nullable=False
    )

    # Bumped whenever every existing session for this user must stop working —
    # today that means a password change or an explicit "sign out everywhere".
    # Access tokens carry the version they were minted under, so revocation is
    # one integer compare in `get_current_user` rather than a denylist of every
    # token ever issued. A token whose `ver` no longer matches is refused for
    # the rest of its life, which is what makes a password change actually end
    # the session an attacker is holding.
    #
    # Tokens minted before this column existed carry no `ver` claim at all and
    # are read as 0, which is the default here — so deploying this does not log
    # the whole userbase out.
    token_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    gmail_accounts: Mapped[list[GmailAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    resumes: Mapped[list[Resume]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    profiles: Mapped[list[Profile]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="Profile.id",
        lazy="selectin",
    )
    recruiters: Mapped[list[Recruiter]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    autopilot: Mapped[AutopilotPreference | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    recruiter_emails: Mapped[list[RecruiterEmail]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    recruiter_reply_preference: Mapped[RecruiterReplyPreference | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    digest_preference: Mapped[DigestPreference | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    notification_preference: Mapped[NotificationPreference | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    # Not `lazy="selectin"`: an account accumulates these, and every
    # `get_current_user` would otherwise load a month of them to serve a
    # request that wants an email address.
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        """True when this account may manage the deployment.

        Read from the column alone. The ``ADMIN_EMAILS`` bootstrap reconciles
        *into* the column at login rather than being consulted here, so there is
        exactly one source of truth for an authorization decision — a property
        that also consulted settings would answer differently in a worker that
        loaded a different environment than the API that granted the session.
        """
        return self.role == ROLE_ADMIN

    @property
    def gmail_connected(self) -> bool:
        """True when at least one Gmail account is connected and healthy."""
        return any(a.status == "connected" for a in self.gmail_accounts)

    @property
    def primary_gmail(self) -> GmailAccount | None:
        """The account outreach sends from — the primary, else the first live one."""
        live = [a for a in self.gmail_accounts if a.status == "connected"]
        if not live:
            return None
        return next((a for a in live if a.is_primary), live[0])

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User id={self.id} email={self.email!r}>"
