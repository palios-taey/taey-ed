"""Transactional email via Resend API.

Sends:
  - email verification on signup
  - purchase receipts (post-Stripe-webhook)
  - password reset (future)

In DEV mode, if no Resend key is configured, emails are logged but not sent.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from spark.secrets_loader import _load_raw, is_production

logger = logging.getLogger(__name__)

# Public-facing app URL for verification/receipt links (env-overridable).
APP_BASE_URL = os.environ.get("TAEY_ED_APP_URL", "https://app.taey.ai")


class EmailError(RuntimeError):
    """Raised on send failure."""


def _get_config() -> tuple[Optional[str], str]:
    """Returns (api_key, from_email) from secrets file. api_key may be None
    in DEV mode if not configured."""
    raw = _load_raw()
    section = raw.get("resend") or {}
    return section.get("api_key"), section.get("from_email", "noreply@taey.ai")


def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
) -> None:
    """Send a transactional email via Resend.

    In DEV with no API key configured, logs the email and returns silently.
    In PRODUCTION, raises EmailError on failure.
    """
    api_key, from_email = _get_config()

    if not api_key:
        if is_production():
            raise EmailError("Resend API key missing in production")
        logger.warning(
            f"DEV: no Resend key; would have sent to={to} subject={subject!r}"
        )
        return

    payload = {
        "from": from_email,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend sits behind Cloudflare; Python urllib's default UA is
            # `Python-urllib/3.x`, which CF flags as a bot signature and
            # rejects with error 1010 (HTTP 403). Identify ourselves so
            # the request looks like a normal API client.
            "User-Agent": "taey-ed/1.0 (transactional)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            logger.info(f"Email sent: to={to} subject={subject!r} id={resp.get('id')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise EmailError(f"Resend HTTP {e.code}: {body}") from e
    except Exception as e:
        raise EmailError(f"Resend request failed: {e}") from e


def send_verification_email(to: str, token: str, base_url: str = APP_BASE_URL) -> None:
    """Send the post-signup email verification link.

    The link points at the static verify.html page on app.taey.ai (NOT at
    the API). That page reads the token from the query string and calls
    GET https://taey-ed-api.taey.ai/auth/verify-email?token=…
    in the user's browser. This way the user lands on a friendly
    "Email confirmed" page with a button back to their account, not on
    a raw API JSON response.
    """
    verify_url = f"{base_url}/verify.html?token={token}"
    html = f"""
    <p>Welcome to Taey-Ed.</p>
    <p>Click the link below to verify your email address:</p>
    <p><a href="{verify_url}">{verify_url}</a></p>
    <p>If you didn't sign up for Taey-Ed, ignore this email.</p>
    """
    text = (
        f"Welcome to Taey-Ed.\n\n"
        f"Verify your email: {verify_url}\n\n"
        f"If you didn't sign up, ignore this message."
    )
    send_email(to, "Verify your Taey-Ed email", html, text)


def send_purchase_receipt(
    to: str, credits: int, amount_usd: float, stripe_session_id: str,
) -> None:
    """Send a purchase confirmation receipt."""
    html = f"""
    <p>Thanks for your purchase.</p>
    <p>You added <strong>{credits} credits</strong> to your Taey-Ed account
    (${amount_usd:.2f}).</p>
    <p>Credits are charged only when Taey-Ed completes a screen for you. If
    something gets stuck or doesn't advance, no credit is charged.</p>
    <p>Reference: {stripe_session_id}</p>
    """
    text = (
        f"Thanks for your purchase.\n\n"
        f"Added {credits} credits to your Taey-Ed account (${amount_usd:.2f}).\n\n"
        f"Credits are charged only when Taey-Ed completes a screen.\n\n"
        f"Reference: {stripe_session_id}"
    )
    send_email(to, "Taey-Ed: credits added", html, text)
