"""Authentication schemas (specification section 22)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"


class UserOut(ORMModel):
    id: str
    email: str
    full_name: Optional[str] = None
    role: str
    is_active: bool
    last_login_at: Optional[datetime] = None


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12,
                          description="At least 12 characters. Hashed with bcrypt; never stored in the clear.")
    full_name: Optional[str] = None
    role: str = Field(default="viewer", pattern="^(admin|viewer)$")


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12)


TokenResponse.model_rebuild()
