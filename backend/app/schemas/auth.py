"""Auth-related schemas."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.emails import normalize_address

#: What the client must echo back to close an account. Upper-cased on both
#: sides so the check is about intent, not about the caps lock key.
DELETE_CONFIRMATION = "DELETE"


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)

    @field_validator("email")
    @classmethod
    def _casefold_email(cls, value: str) -> str:
        """Store the address lower-cased.

        ``EmailStr`` lower-cases the *domain* only — ``Jane.Doe@ACME.com``
        validates to ``Jane.Doe@acme.com`` with the local part untouched. Since
        the login lookup and ``users.email``'s unique index are both
        case-sensitive string comparisons, leaving that case in the column meant
        the same human could hold two accounts and, far more often, could not
        sign in to the one they had. See ``routers.auth``.
        """
        return normalize_address(value)


class PasswordChange(BaseModel):
    """A password change is a re-authentication, not just an update.

    ``current_password`` is required for that reason: without it a stolen
    access token is a permanent account takeover, because the thief can lock
    the owner out with the very token the owner is trying to revoke.

    ``new_password`` carries the same constraints as registration — one policy,
    or the change endpoint quietly becomes a way to set a password the signup
    form would have refused.
    """

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class AccountDeletion(BaseModel):
    """Closing the account, which is the one action here that cannot be undone.

    Same re-authentication as :class:`PasswordChange`, for a stronger version
    of the same reason: a stolen token that can change a password locks the
    owner out, and a stolen token that can delete the account destroys the
    correspondence, the resumes and the applications with no way back.

    ``confirm`` is not security — anyone holding the password can type it. It
    is there so the request cannot be the accidental result of a mis-wired
    client or a repeated fetch; a DELETE with a body the user had to compose
    is not something a stray retry produces.
    """

    password: str = Field(min_length=1, max_length=128)
    confirm: str = Field(min_length=1, max_length=64)

    @field_validator("confirm")
    @classmethod
    def _must_match_phrase(cls, value: str) -> str:
        if value.strip().upper() != DELETE_CONFIRMATION:
            raise ValueError(f"Type {DELETE_CONFIRMATION} to confirm")
        return value.strip().upper()


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: EmailStr
    full_name: str | None = None
    is_active: bool
    gmail_connected: bool
    # What the client uses to decide whether to render the admin nav entry.
    # Convenience only — it hides a link, it does not guard anything. Every
    # admin route re-derives this server-side from `users.role`, because a
    # response field is a claim the client could simply have made up.
    is_admin: bool = False
    role: str = "user"

    model_config = {"from_attributes": True}
