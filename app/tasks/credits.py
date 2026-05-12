"""
Credits helper — Bearer-authenticated read of the user's balance.

Reads only. Mutations (purchase, debit) happen server-side via the
billing webhook + worker pipeline; the Mac never writes credits directly.

API contract:
  GET /credits/balance  Bearer → {"balance": <int>}
"""

import logging
from typing import Optional

import httpx

from app.tasks.call_spark import call_spark

logger = logging.getLogger("taey-ed")


def get_balance() -> Optional[int]:
    """Return the user's current credit balance, or None on any failure.

    Best-effort: a transient network or auth error returns None, and the
    UI shows "—" rather than crashing the header rendering.
    """
    try:
        data = call_spark("/credits/balance", method="GET")
    except httpx.HTTPError as e:
        logger.warning(f"credits.get_balance: HTTP error: {e}")
        return None
    except Exception as e:
        logger.warning(f"credits.get_balance: unexpected: {e}")
        return None
    bal = data.get("balance")
    try:
        return int(bal) if bal is not None else None
    except (TypeError, ValueError):
        return None
