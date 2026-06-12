"""DEV STUB - spark_v2 does not track credits. Replace when production credit system is wired."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/credits/balance")
async def credits_balance() -> dict:
    return {
        "balance": 999999,
        "currency": "credits",
        "_dev_stub": True,
    }
