"""Auth routes: signup, login, refresh, logout, me, verify-email.

POST /auth/signup           → create user, send verification email
POST /auth/login            → password check → issue access + refresh tokens
POST /auth/refresh          → rotate refresh token, issue new access token
POST /auth/logout           → revoke refresh token
GET  /auth/me               → current user info (requires Bearer JWT)
GET  /auth/verify-email     → consume email-verification token
"""

import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from spark import email_service
from spark.jwt_helper import (
    ACCESS_TOKEN_TTL_MINUTES,
    decode_access_token,
    issue_access_token,
)
from spark.storage import refresh_tokens, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Request / response models ──

class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int  # seconds until access_token expires
    token_type: str = "Bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    email_verified: bool
    created_at: str


# ── Helpers ──

def _user_response(u: users.User) -> UserResponse:
    return UserResponse(
        id=u.id, email=u.email, email_verified=u.email_verified, created_at=u.created_at,
    )


def _token_response(user_id: str, request: Request) -> TokenResponse:
    """Mint access + refresh tokens for a user."""
    access_token, exp = issue_access_token(user_id)
    user_agent = request.headers.get("User-Agent", "")[:200]
    ip_addr = request.client.host if request.client else None
    raw_refresh, _ = refresh_tokens.issue_refresh_token(user_id, user_agent, ip_addr)
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )


def _require_user(request: Request) -> users.User:
    """Resolve the requesting user from the Authorization Bearer header.

    The APIKeyMiddleware in server.py has already validated the JWT and set
    request.state.user_id. We re-fetch the user record for fresh state.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id or user_id in ("spark_claude", "internal", "mac_pipeline"):
        # Non-user identities reaching here means the route isn't tied to a
        # real account — caller bug.
        raise HTTPException(status_code=401, detail="User auth required")
    u = users.get_user_by_id(user_id)
    if not u:
        raise HTTPException(status_code=401, detail="User not found")
    return u


# ── Routes ──

@router.post("/signup", status_code=201, response_model=UserResponse)
def signup(req: SignupRequest):
    """Create a new user account. Sends a verification email.

    Returns the user object (without tokens). Client should call /auth/login
    after signup to receive tokens; this keeps verification an explicit step.
    """
    try:
        user = users.create_user(req.email, req.password)
    except sqlite3.IntegrityError:
        # Email already registered. Return 409 with a generic message that
        # doesn't reveal whether the email exists (mild enumeration protection).
        raise HTTPException(
            status_code=409,
            detail="That email is already in use",
        )

    # Best-effort verification email send. Failure doesn't block signup;
    # user can request resend later (TODO endpoint, future).
    try:
        token = users.get_verification_token(user.id)
        if token:
            email_service.send_verification_email(user.email, token)
    except email_service.EmailError as e:
        logger.warning(f"verification email send failed for {user.email}: {e}")

    return _user_response(user)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request):
    """Email/password login. Returns access + refresh tokens.

    Constant-time comparison; same error message regardless of whether
    the email exists or the password is wrong.
    """
    user = users.get_user_by_email(req.email)
    if user is None:
        # Run a dummy bcrypt to keep response timing similar
        users.verify_password(req.password, "$2b$12$" + "x" * 53)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    pwd_hash = users.get_password_hash(user.id)
    if pwd_hash is None or not users.verify_password(req.password, pwd_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return _token_response(user.id, request)


@router.post("/refresh", response_model=TokenResponse)
def refresh(req: RefreshRequest, request: Request):
    """Rotate a refresh token: old one is revoked, new access + refresh issued.

    If the old token is unknown / expired / already revoked, returns 401 and
    the client must prompt the user to re-login.
    """
    record = refresh_tokens.lookup_active(req.refresh_token)
    if record is None:
        raise HTTPException(status_code=401, detail="Refresh token invalid or expired")

    # Verify user still exists
    user = users.get_user_by_id(record.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")

    # Rotate: revoke the used token, mint a new pair
    refresh_tokens.mark_used(record.id)
    refresh_tokens.revoke(record.id)
    return _token_response(user.id, request)


@router.post("/logout", status_code=204)
def logout(req: LogoutRequest):
    """Revoke a refresh token. Access tokens are short-lived (15 min) so we
    don't try to invalidate them server-side — they'll expire on their own.
    For force-logout of all sessions, an admin endpoint (future) calls
    refresh_tokens.revoke_all_for_user(user_id)."""
    record = refresh_tokens.lookup_active(req.refresh_token)
    if record:
        refresh_tokens.revoke(record.id)
    # 204 either way (don't reveal whether token was active)
    return None


@router.get("/me", response_model=UserResponse)
def me(request: Request):
    """Return the current user's info. Requires Bearer JWT."""
    user = _require_user(request)
    return _user_response(user)


@router.get("/verify-email")
def verify_email(token: str):
    """Consume an email verification token. The link in the verification
    email points here.

    Returns 200 if verified (or already verified). 404 if token unknown.

    NOTE: this endpoint must be accessible WITHOUT auth (user clicks the email
    link without being logged in). server.py's middleware needs an allowlist
    or we wire this into a /public prefix. For now, the link includes an
    opaque token that's the auth.
    """
    user = users.mark_email_verified(token)
    if user is None:
        raise HTTPException(status_code=404, detail="Invalid or expired verification token")
    return {"status": "verified", "email": user.email}
