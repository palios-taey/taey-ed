"""Credits routes: balance and ledger inspection.

GET /credits           → current balance + recent history (default last 50 entries)
GET /credits/balance   → balance only (lightweight, for frequent polling from app)
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from spark.storage import credits, users

router = APIRouter(prefix="/credits", tags=["credits"])


class BalanceResponse(BaseModel):
    balance: int


class LedgerEntryResponse(BaseModel):
    id: str
    type: str
    amount: int
    balance_after: int
    source: Optional[str]
    created_at: str


class CreditsResponse(BaseModel):
    balance: int
    history: list[LedgerEntryResponse]


def _require_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if not user_id or user_id in ("spark_claude", "internal", "mac_pipeline"):
        raise HTTPException(status_code=401, detail="User auth required")
    if not users.get_user_by_id(user_id):
        raise HTTPException(status_code=401, detail="User not found")
    return user_id


@router.get("/balance", response_model=BalanceResponse)
def get_balance(request: Request):
    """Lightweight: returns just the current balance integer.
    Designed for the Mac app's persistent header/status bar."""
    user_id = _require_user_id(request)
    return BalanceResponse(balance=credits.get_balance(user_id))


@router.get("", response_model=CreditsResponse)
def get_credits(request: Request, limit: int = Query(default=50, ge=1, le=500)):
    """Balance + recent ledger history (newest first)."""
    user_id = _require_user_id(request)
    history = credits.get_history(user_id, limit=limit)
    return CreditsResponse(
        balance=credits.get_balance(user_id),
        history=[
            LedgerEntryResponse(
                id=e.id,
                type=e.type,
                amount=e.amount,
                balance_after=e.balance_after,
                source=e.source,
                created_at=e.created_at,
            )
            for e in history
        ],
    )
