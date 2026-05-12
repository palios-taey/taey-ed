"""JWT issuance + verification helpers.

Access tokens are short-lived (15 min) and carry the user_id as `sub`.
Refresh tokens are opaque random strings handled separately
(see spark.storage.refresh_tokens). This module covers access JWTs only.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt as pyjwt

from spark.secrets_loader import get_taey_ed_secrets

ACCESS_TOKEN_TTL_MINUTES = 15
JWT_ALGORITHM = "HS256"


def issue_access_token(user_id: str, extra_claims: Optional[dict] = None) -> tuple[str, datetime]:
    """Mint a short-lived access JWT for the given user.

    Returns (encoded_token, expires_at). The Mac app stores this in memory
    only — never to disk — and includes it in the Authorization: Bearer header
    on every API call.
    """
    secrets = get_taey_ed_secrets()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    token = pyjwt.encode(payload, secrets.jwt_secret, algorithm=JWT_ALGORITHM)
    return token, expires_at


def decode_access_token(token: str) -> Optional[dict]:
    """Decode + verify an access JWT. Returns the payload on success, None on
    any failure (expired, malformed, wrong signature). The caller surfaces 401
    on None.
    """
    secrets = get_taey_ed_secrets()
    try:
        payload = pyjwt.decode(token, secrets.jwt_secret, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload
    except pyjwt.PyJWTError:
        return None
