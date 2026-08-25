from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SignupRequest(BaseModel):
    username: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_.\-]+$")
    email: EmailStr
    # No length restriction on passwords beyond non-empty — that's a
    # deliberate product decision (users pick whatever they want). The
    # min_length=1 floor only prevents a literally-empty password, which
    # would be "no password at all" since bcrypt happily verifies the
    # empty string. The max guards against silly multi-MB POSTs (bcrypt
    # caps at 72 bytes for hashing anyway).
    password: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str
    email: EmailStr
    email_verified: bool
    is_admin: bool
    created_at: datetime
    # Real tier from the DB - what billing + features see by default.
    tier: str = "basic"
    # Admin-only impersonation. NULL for normal users (and for admins
    # who haven't flipped the Dev-page toggle). When set, the frontend
    # + worker app render as this tier instead.
    tier_override: Optional[str] = None
    # Convenience: tier_override ?? tier. Frontends should branch on
    # this rather than picking between the two themselves.
    effective_tier: str = "basic"


class VerifyEmailRequest(BaseModel):
    # Token from the verification URL. We hash it server-side and look
    # up the matching record. No session required to redeem — the
    # token IS the proof of email control.
    token: str = Field(min_length=10, max_length=200)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    # Token is the plaintext from the reset URL. We hash it server-side
    # and look up the matching record. Length range matches what
    # secrets.token_urlsafe(32) produces (~43 base64 chars), with a bit
    # of slack on either side.
    token: str = Field(min_length=10, max_length=200)
    # No length restriction beyond non-empty — matches SignupRequest.
    new_password: str = Field(min_length=1, max_length=200)


class RequestAccountDeletionRequest(BaseModel):
    # Step 1 (in-dialog): re-auth + accept the charge + opt-in for
    # the export email. Backend charges card synchronously, mints a
    # one-time token, sends the verification email. Account is NOT
    # deleted yet.
    current_password: str = Field(max_length=200)
    export_requested: bool = False


class ConfirmAccountDeletionRequest(BaseModel):
    # Step 2 (email link → /confirm-delete page → POST). Token IS
    # the proof of intent + email control, so this endpoint is
    # unauthenticated by design (might be on a different device).
    token: str = Field(min_length=10, max_length=200)


class UpdateProfileRequest(BaseModel):
    # current_password is always required — even for low-risk changes
    # like username — because we let you change multiple things in one
    # request and the email/password changes need it. Keeping the
    # single rule "you confirmed your password to commit any change"
    # is also simpler to reason about than per-field rules.
    current_password: str = Field(max_length=200)
    # All fields below are optional — leaving any None means "don't
    # change this." Empty strings are coerced to None server-side so
    # the UI can pass blanks without explicitly clearing them.
    new_username: Optional[str] = Field(
        default=None, min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_.\-]+$"
    )
    new_email: Optional[EmailStr] = None
    # No length restriction beyond non-empty — matches SignupRequest.
    new_password: Optional[str] = Field(default=None, min_length=1, max_length=200)
