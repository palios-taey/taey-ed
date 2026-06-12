"""DEV STUB - accept-any-credentials. Disabled when SPARK_V2_AUTH=enforce (out of scope for tonight)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, Request

router = APIRouter()


def _token_payload() -> dict:
    return {
        "access_token": uuid.uuid4().hex,
        "refresh_token": uuid.uuid4().hex,
        "expires_in": 3600,
        "token_type": "Bearer",
    }


@router.post("/auth/signup")
async def auth_signup(_: Request) -> dict:
    return _token_payload()


@router.post("/auth/login")
async def auth_login(_: Request) -> dict:
    return _token_payload()


@router.post("/auth/refresh")
async def auth_refresh(_: Request) -> dict:
    return _token_payload()


@router.post("/auth/logout")
async def auth_logout() -> dict:
    return {"ok": True}


@router.get("/auth/me")
async def auth_me(authorization: str | None = Header(default=None)) -> dict:
    _ = authorization
    return {
        "email": "dev@local",
        "user_id": "dev-local-1",
        "is_dev_stub": True,
    }
