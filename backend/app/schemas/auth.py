"""Authentication and user-management schemas (specification sections 9 and 10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import (AliasChoices, BaseModel, EmailStr, Field, field_validator,
                      model_validator)

from app.core.security import PasswordPolicyError, validate_password
from app.models.enums import normalize_role, normalize_status
from app.schemas.common import ORMModel

#: Usernames go in URLs, logs and audit rows; keeping the character set narrow means
#: none of those ever have to escape one.
USERNAME_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$"


def _check_password(password: str, *, username: str = "", email: str = "") -> str:
    try:
        validate_password(password, username=username, email=email)
    except PasswordPolicyError as exc:
        raise ValueError("The password must contain " + ", ".join(exc.problems) + ".") from exc
    return password


class LoginRequest(BaseModel):
    """Username *or* email address, plus a password (section 9, step 3).

    One field accepts either. ``email`` is kept as an alias so a client written
    against the previous API keeps working.
    """

    username: str = Field(
        min_length=1, max_length=255,
        validation_alias=AliasChoices("username", "email", "identifier"),
        description="Username or email address.")
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    #: Echoed so the front end can put it in the ``X-CSRF-Token`` header. It is not a
    #: secret in the way the session token is: on its own it authenticates nothing.
    csrf_token: str
    user: "UserOut"


class UserOut(ORMModel):
    id: str
    username: str
    email: str
    full_name: Optional[str] = None
    role: str
    status: str
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    created_by_user_id: Optional[str] = None
    #: The creator's username, resolved for display so the list needs no second call.
    created_by_username: Optional[str] = None


class UserCreate(BaseModel):
    """The Create User form (section 10).

    ``confirm_password`` is checked here rather than only in the browser: a mistyped
    password that reaches the database is an account nobody can sign in to.
    """

    full_name: Optional[str] = Field(default=None, max_length=255)
    username: str = Field(pattern=USERNAME_PATTERN, description="3–64 characters.")
    email: EmailStr
    role: str = Field(default="USER")
    password: str
    confirm_password: Optional[str] = None
    status: str = Field(default="ACTIVE")

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        return normalize_role(value).value

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        return normalize_status(value).value

    @model_validator(mode="after")
    def _passwords(self) -> "UserCreate":
        if self.confirm_password is not None and self.password != self.confirm_password:
            raise ValueError("The passwords do not match.")
        _check_password(self.password, username=self.username, email=str(self.email))
        return self


class UserUpdate(BaseModel):
    """What an administrator may change about an existing account (section 10).

    Not the username: it is the handle the audit trail is written against, and
    reassigning it would silently re-attribute history. Not the password either —
    that is a separate, separately audited action.
    """

    full_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    status: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _role(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else normalize_role(value).value

    @field_validator("status")
    @classmethod
    def _status(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else normalize_status(value).value


class PasswordReset(BaseModel):
    """An administrator setting someone else's password (section 10).

    No current password: the administrator does not have it, and must never be shown
    it. The event is audited as ``USER_PASSWORD_RESET``.
    """

    new_password: str
    confirm_password: Optional[str] = None

    @model_validator(mode="after")
    def _passwords(self) -> "PasswordReset":
        if self.confirm_password is not None and self.new_password != self.confirm_password:
            raise ValueError("The passwords do not match.")
        _check_password(self.new_password)
        return self


class PasswordChange(BaseModel):
    """A user changing their own password. The current one is required."""

    current_password: str
    new_password: str
    confirm_password: Optional[str] = None

    @model_validator(mode="after")
    def _passwords(self) -> "PasswordChange":
        if self.confirm_password is not None and self.new_password != self.confirm_password:
            raise ValueError("The passwords do not match.")
        if self.current_password == self.new_password:
            raise ValueError("The new password must differ from the current one.")
        _check_password(self.new_password)
        return self


class AuditLogOut(ORMModel):
    id: str
    event: str
    user_id: Optional[str] = None
    performed_by_user_id: Optional[str] = None
    subject_label: Optional[str] = None
    performed_by_label: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    detail: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class UserRoleOut(BaseModel):
    """One role and what it may do, so the UI can explain the choice it offers."""

    value: str
    label: str
    permissions: List[str]


TokenResponse.model_rebuild()
