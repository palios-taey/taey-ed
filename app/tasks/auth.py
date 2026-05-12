"""
Auth surface for the Mac app — login / signup / token management.

Storage model (per LAUNCH_PLAN §10 + dispatch 2026-05-12):
  - access_token: in-memory only, process lifetime
  - refresh_token: macOS Keychain (single item, 1 user per machine)
  - user_email: macOS Keychain (so we know who's logged in across launches)

The Keychain is accessed via the `security` CLI; no new Python dependency.

API contract: matches Mira at https://taey-ed-api.taey.ai
  POST /auth/signup   {email, password} → {access_token, refresh_token, expires_in, token_type}
  POST /auth/login    {email, password} → {access_token, refresh_token, expires_in, token_type}
  POST /auth/refresh  {refresh_token}   → {access_token, refresh_token, expires_in, token_type}
  POST /auth/logout                     → {ok: true}
  GET  /auth/me       Bearer            → {email, user_id, ...}
"""

import logging
import os
import subprocess
import time
from typing import Optional

import httpx

logger = logging.getLogger("taey-ed")

_KEYCHAIN_SERVICE = "com.paliostaey.taey-ed"
_KEYCHAIN_ACCOUNT_REFRESH = "refresh-token"
_KEYCHAIN_ACCOUNT_EMAIL = "user-email"

_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=15.0)

# In-memory state — re-acquired from refresh token on launch
_access_token: Optional[str] = None
_access_expires_at: Optional[float] = None  # unix epoch
_user_email_cached: Optional[str] = None


def _server_url() -> str:
    """Resolve the server URL from app.config (so this respects the
    same precedence the rest of the Mac uses)."""
    from app.config import get_spark_url
    return get_spark_url().rstrip("/")


# ---------------------------------------------------------------------------
# Keychain helpers (subprocess `security` CLI — no new dependency)
# ---------------------------------------------------------------------------

def _keychain_set(account: str, value: str) -> bool:
    """Store value in macOS Keychain. Update if exists, add if not."""
    try:
        subprocess.run(
            ["security", "add-generic-password",
             "-s", _KEYCHAIN_SERVICE,
             "-a", account,
             "-w", value,
             "-U"],
            check=True, capture_output=True, text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"keychain set {account!r} failed: {e.stderr}")
        return False


def _keychain_get(account: str) -> Optional[str]:
    """Read value from macOS Keychain. Returns None if not present."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", _KEYCHAIN_SERVICE,
             "-a", account,
             "-w"],
            check=True, capture_output=True, text=True,
        )
        return r.stdout.rstrip("\n")
    except subprocess.CalledProcessError:
        return None


def _keychain_delete(account: str) -> bool:
    """Remove value from macOS Keychain. Idempotent."""
    try:
        subprocess.run(
            ["security", "delete-generic-password",
             "-s", _KEYCHAIN_SERVICE,
             "-a", account],
            check=True, capture_output=True, text=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False  # nothing to delete


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def _store_tokens(access_token: str, refresh_token: str, expires_in: int,
                  email: Optional[str] = None) -> None:
    """Cache access token in memory, persist refresh token + email to Keychain."""
    global _access_token, _access_expires_at, _user_email_cached
    _access_token = access_token
    _access_expires_at = time.time() + max(0, expires_in - 30)  # 30s safety margin
    _keychain_set(_KEYCHAIN_ACCOUNT_REFRESH, refresh_token)
    if email:
        _user_email_cached = email
        _keychain_set(_KEYCHAIN_ACCOUNT_EMAIL, email)


def _clear_tokens() -> None:
    """Drop in-memory access + delete Keychain refresh/email. Idempotent."""
    global _access_token, _access_expires_at, _user_email_cached
    _access_token = None
    _access_expires_at = None
    _user_email_cached = None
    _keychain_delete(_KEYCHAIN_ACCOUNT_REFRESH)
    _keychain_delete(_KEYCHAIN_ACCOUNT_EMAIL)


def get_access_token() -> Optional[str]:
    """Return the in-memory access token if present and not expired.
    Does NOT auto-refresh — callers should handle 401 via refresh_access_token()."""
    if _access_token and _access_expires_at and time.time() < _access_expires_at:
        return _access_token
    return None


def get_user_email() -> Optional[str]:
    """Return logged-in user's email, in memory or Keychain."""
    global _user_email_cached
    if _user_email_cached:
        return _user_email_cached
    e = _keychain_get(_KEYCHAIN_ACCOUNT_EMAIL)
    if e:
        _user_email_cached = e
    return _user_email_cached


def is_logged_in() -> bool:
    """True if we have a refresh token on disk (regardless of access token freshness)."""
    return _keychain_get(_KEYCHAIN_ACCOUNT_REFRESH) is not None


# ---------------------------------------------------------------------------
# Server-side flows
# ---------------------------------------------------------------------------

def signup(email: str, password: str) -> dict:
    """Create a new account. Stores tokens on success. Raises on failure."""
    url = f"{_server_url()}/auth/signup"
    r = httpx.post(url, json={"email": email, "password": password}, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    _store_tokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=data.get("expires_in", 900),
        email=email,
    )
    logger.info(f"auth.signup ok: {email}")
    return data


def login(email: str, password: str) -> dict:
    """Sign in. Stores tokens on success. Raises on failure."""
    url = f"{_server_url()}/auth/login"
    r = httpx.post(url, json={"email": email, "password": password}, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    _store_tokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=data.get("expires_in", 900),
        email=email,
    )
    logger.info(f"auth.login ok: {email}")
    return data


def refresh_access_token() -> bool:
    """Try to refresh access token using Keychain refresh token.
    Returns True on success, False on failure. On failure, callers should
    clear tokens and show the login UI."""
    rt = _keychain_get(_KEYCHAIN_ACCOUNT_REFRESH)
    if not rt:
        return False
    url = f"{_server_url()}/auth/refresh"
    try:
        r = httpx.post(url, json={"refresh_token": rt}, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        _store_tokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_in=data.get("expires_in", 900),
        )
        logger.info("auth.refresh ok")
        return True
    except httpx.HTTPStatusError as e:
        logger.warning(f"auth.refresh failed status={e.response.status_code}")
        if e.response.status_code in (401, 403):
            # Refresh token rejected — wipe local state, force login UI
            _clear_tokens()
        return False
    except httpx.HTTPError as e:
        logger.error(f"auth.refresh network error: {e}")
        return False


def logout() -> None:
    """Sign out: best-effort revoke server-side, always wipe local."""
    rt = _keychain_get(_KEYCHAIN_ACCOUNT_REFRESH)
    if rt:
        url = f"{_server_url()}/auth/logout"
        try:
            httpx.post(url, json={"refresh_token": rt}, timeout=_TIMEOUT)
        except httpx.HTTPError:
            pass  # best effort; local clear below is what matters
    _clear_tokens()
    logger.info("auth.logout done")


def whoami() -> Optional[dict]:
    """GET /auth/me with current access token. Refreshes once on 401.
    Returns user dict on success; None if not logged in or refresh failed."""
    tok = get_access_token()
    if not tok:
        if not refresh_access_token():
            return None
        tok = get_access_token()
        if not tok:
            return None
    url = f"{_server_url()}/auth/me"
    headers = {"Authorization": f"Bearer {tok}"}
    try:
        r = httpx.get(url, headers=headers, timeout=_TIMEOUT)
        if r.status_code == 401:
            if refresh_access_token():
                tok = get_access_token()
                r = httpx.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=_TIMEOUT)
            else:
                return None
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------------------
# Bearer header helper (used by call_spark.py + embedding_client.py)
# ---------------------------------------------------------------------------

def bearer_header(*, refresh_if_needed: bool = True) -> dict:
    """Return {'Authorization': 'Bearer <token>'} or {} if not logged in.
    If refresh_if_needed and no fresh access token, tries refresh once."""
    tok = get_access_token()
    if not tok and refresh_if_needed:
        if refresh_access_token():
            tok = get_access_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}
