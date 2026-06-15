"""Billing routes: Stripe one-time checkout for credit packs + webhook.

POST /billing/create-checkout-session  → returns Stripe Checkout URL for the user
POST /billing/webhook                  → Stripe webhook (signature-verified)

The webhook is the source of truth for credit deposits. The checkout session
just initiates payment; only the webhook (signed by Stripe) confirms it's real.

Idempotency: the webhook uses stripe_session_id as the idempotency key in the
credit ledger, so duplicate webhooks (Stripe retries) are no-ops.

Per LAUNCH_PLAN.md §4 Gap D: one-time credit purchases; no subscriptions.
Per ChatGPT review: "credits are charged only when a screen completes."
The webhook only credits successful one-time `checkout.session.completed` events.
"""

import json
import logging
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from spark.email_service import EmailError, send_purchase_receipt
from spark.secrets_loader import _load_raw, get_stripe_secrets, is_production
from spark.storage import credits, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# Configure Stripe SDK once at module load.
_stripe_secrets = get_stripe_secrets()
stripe.api_key = _stripe_secrets.secret_key


def _price_id_to_credits() -> dict[str, int]:
    """Map Stripe price IDs → credits granted per purchase.

    Read from the API secrets file under stripe.price_ids.
    Schema: { "price_xxx": { "credits": <int>, ... } }
    """
    raw = _load_raw()
    price_ids = (raw.get("stripe") or {}).get("price_ids") or {}
    mapping: dict[str, int] = {}
    for key, value in price_ids.items():
        if not isinstance(value, dict):
            continue
        credits_val = value.get("credits")
        if isinstance(credits_val, int) and credits_val > 0:
            mapping[key] = credits_val
    return mapping


# ── Request / response models ──

class CreateCheckoutRequest(BaseModel):
    # Optional: if omitted, the server picks the single configured price tier.
    # With one tier active this is what the site always wants; with multiple
    # tiers callers should pass an explicit price_id.
    price_id: Optional[str] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


# ── Routes ──

@router.post("/create-checkout-session", response_model=CheckoutResponse)
def create_checkout_session(req: CreateCheckoutRequest, request: Request):
    """Create a Stripe Checkout session for one-time credit pack purchase.

    The Mac app or site calls this with the desired price_id, then redirects
    the user to checkout_url. After payment Stripe redirects to success_url
    and (separately) sends a webhook to /billing/webhook.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id or user_id in ("spark_claude", "internal", "mac_pipeline"):
        raise HTTPException(status_code=401, detail="User auth required")
    user = users.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    mapping = _price_id_to_credits()
    if not mapping:
        raise HTTPException(
            status_code=500,
            detail="No price tiers configured (stripe.price_ids is empty in secrets)",
        )

    # Resolve price_id: explicit if given, otherwise the single configured
    # tier. If the caller passes one that isn't configured, reject — never
    # silently substitute, because price_id determines how many credits
    # the webhook will grant.
    if req.price_id is None:
        if len(mapping) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Multiple price tiers configured; client must pass an "
                    f"explicit price_id. Available: {sorted(mapping.keys())}"
                ),
            )
        price_id = next(iter(mapping.keys()))
    else:
        if req.price_id not in mapping:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown price_id (not configured for credit grant): {req.price_id}",
            )
        price_id = req.price_id

    # Find or create Stripe customer for this user.
    if user.stripe_customer_id:
        stripe_customer_id = user.stripe_customer_id
    else:
        cust = stripe.Customer.create(
            email=user.email,
            metadata={"taey_ed_user_id": user.id},
        )
        users.set_stripe_customer_id(user.id, cust.id)
        stripe_customer_id = cust.id

    # Site uses flat /*.html pages, not nested /checkout/* routes. Without
    # .html the python http.server returns 404.
    success_url = req.success_url or "https://app.taey.ai/success.html?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = req.cancel_url or "https://app.taey.ai/cancel.html"

    session = stripe.checkout.Session.create(
        mode="payment",  # one-time, NOT subscription
        customer=stripe_customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        # Show the "Add promotion code" field on the Stripe-hosted checkout
        # page. Lets Jesse (and future users) redeem promotion codes created
        # against coupons in the Stripe dashboard.
        allow_promotion_codes=True,
        metadata={
            "taey_ed_user_id": user.id,
            "taey_ed_credits": str(mapping[price_id]),
        },
    )
    logger.info(
        f"Stripe checkout created: session={session.id} user={user.id} "
        f"price={price_id} credits={mapping[price_id]}"
    )
    return CheckoutResponse(checkout_url=session.url, session_id=session.id)


@router.post("/webhook", status_code=200)
async def stripe_webhook(request: Request):
    """Stripe webhook endpoint. Verifies signature, deposits credits on
    successful checkout.

    Idempotent: uses stripe_session_id as the credit ledger idempotency key,
    so Stripe retries are no-ops.
    """
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, _stripe_secrets.webhook_secret,
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        return _handle_checkout_completed(obj)

    # Other event types (refunds, disputes, etc.) — log + ack but don't act.
    logger.info(f"webhook: received event {event_type} (no handler); acked")
    return {"received": True}


def _handle_checkout_completed(session) -> dict:
    """Process a successful checkout.session.completed event.

    `session` is a stripe.checkout.Session object reconstructed from the
    webhook payload. Stripe objects are dict-like via subscription syntax
    (session["id"]) but newer SDK versions removed the legacy `.get()`
    method on them — convert to a plain dict first.
    """
    session = dict(session) if not isinstance(session, dict) else session
    session_id = session.get("id")
    metadata = session.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = dict(metadata)
    user_id = metadata.get("taey_ed_user_id")
    credits_str = metadata.get("taey_ed_credits")
    amount_total = session.get("amount_total", 0)  # cents

    if not (user_id and credits_str and session_id):
        logger.error(
            f"webhook checkout.session.completed missing metadata: "
            f"session={session_id} user={user_id} credits={credits_str}"
        )
        return {"received": True, "credited": False, "reason": "missing_metadata"}

    try:
        credits_int = int(credits_str)
    except ValueError:
        logger.error(f"webhook: invalid credits value in metadata: {credits_str!r}")
        return {"received": True, "credited": False, "reason": "invalid_credits"}

    user = users.get_user_by_id(user_id)
    if not user:
        logger.error(f"webhook: user not found: {user_id}")
        return {"received": True, "credited": False, "reason": "user_not_found"}

    # Deposit credits (idempotent on stripe_session_id)
    entry = credits.add_purchase(
        user_id=user.id,
        credits=credits_int,
        stripe_session_id=session_id,
        metadata={
            "amount_total_cents": amount_total,
            "currency": session.get("currency"),
            "customer_email": session.get("customer_email"),
        },
    )
    logger.info(
        f"webhook: credited {credits_int} to user {user.id} "
        f"(session={session_id}, balance_after={entry.balance_after})"
    )

    # Send receipt (best-effort; don't fail webhook on email send failure)
    try:
        send_purchase_receipt(
            to=user.email,
            credits=credits_int,
            amount_usd=amount_total / 100.0,
            stripe_session_id=session_id,
        )
    except EmailError as e:
        logger.warning(f"webhook: purchase receipt email failed for {user.email}: {e}")

    return {"received": True, "credited": True, "balance_after": entry.balance_after}
