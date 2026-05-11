"""Centralized secrets loader for the Taey-Ed central server.

Reads /etc/taey-ed/secrets.json (path overridable via TAEY_ED_SECRETS_PATH).
Provides typed accessors for each subsystem (stripe, taey_ed, etc.).

Production-mode gating:
    Set env TAEY_ED_PRODUCTION=1 to enforce strict secret validation at startup.
    Without it, the loader runs in dev mode and tolerates missing/weak secrets
    by generating ephemeral defaults (logged as warnings).

Required secrets-file shape for the taey_ed section:
    {
      "taey_ed": {
        "jwt_secret": "<32+ random bytes hex-encoded>",
        "internal_api_key": "<optional, localhost-dev only, NOT used in production>"
      },
      "stripe": { ... already present ... },
      ...
    }

Never log secret values. Never write back to the secrets file from this module —
the file is Jesse-curated.
"""

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SECRETS_PATH = Path(os.environ.get(
    "TAEY_ED_SECRETS_PATH",
    "/etc/taey-ed/secrets.json",
))


def is_production() -> bool:
    """True iff TAEY_ED_PRODUCTION=1 (or any truthy non-empty non-'0' value).

    Production mode enforces strict secret validation at startup.
    """
    val = os.environ.get("TAEY_ED_PRODUCTION", "").strip().lower()
    return val not in ("", "0", "false", "no")


@dataclass(frozen=True)
class TaeyEdSecrets:
    """Server-side secrets for the Taey-Ed API + auth + billing layer."""
    jwt_secret: str
    internal_api_key: Optional[str]  # None in production; used only for localhost dev


@dataclass(frozen=True)
class StripeSecrets:
    """Stripe API credentials. Used by the billing module."""
    publishable_key: str
    secret_key: str
    webhook_secret: str


class SecretsError(RuntimeError):
    """Raised when production-mode startup cannot satisfy secret requirements."""


def _load_raw() -> dict:
    """Read the secrets JSON file. Returns empty dict if absent."""
    if not SECRETS_PATH.exists():
        if is_production():
            raise SecretsError(
                f"Secrets file not found at {SECRETS_PATH}. "
                f"Production startup requires this file with a 'taey_ed' section."
            )
        logger.warning(
            f"Secrets file not found at {SECRETS_PATH}. "
            f"Running in DEV mode with ephemeral generated secrets."
        )
        return {}
    try:
        return json.loads(SECRETS_PATH.read_text())
    except json.JSONDecodeError as e:
        raise SecretsError(f"Secrets file is malformed JSON: {e}") from e


def _gen_ephemeral_jwt_secret() -> str:
    """Generate a random JWT secret. Only used in DEV mode when no file value exists.

    Production paths must never reach this function — `validate_for_production()`
    raises before any caller would fall through to ephemeral generation.
    """
    return secrets.token_hex(32)


def get_taey_ed_secrets() -> TaeyEdSecrets:
    """Load the Taey-Ed server section of the secrets file.

    In DEV mode (TAEY_ED_PRODUCTION not set), missing fields fall back to
    ephemeral values with logged warnings. In PRODUCTION mode, missing or
    weak values raise SecretsError before the server can start.
    """
    raw = _load_raw()
    section = raw.get("taey_ed") or {}

    jwt_secret = section.get("jwt_secret")
    internal_api_key = section.get("internal_api_key")

    if is_production():
        # Strict validation: jwt_secret must be present and strong.
        if not jwt_secret:
            raise SecretsError(
                "Production startup: 'taey_ed.jwt_secret' missing from secrets file. "
                f"Add a 32+ byte random hex string at {SECRETS_PATH} under "
                "key path taey_ed.jwt_secret."
            )
        if jwt_secret in (
            "dev-secret-change-in-production",
            "change-me",
            "secret",
            "",
        ):
            raise SecretsError(
                "Production startup: 'taey_ed.jwt_secret' is a known weak/placeholder "
                "value. Replace with strong random (e.g. `python -c \"import secrets;"
                " print(secrets.token_hex(32))\"`)."
            )
        if len(jwt_secret) < 32:
            raise SecretsError(
                f"Production startup: 'taey_ed.jwt_secret' is too short "
                f"({len(jwt_secret)} chars; require >=32). Replace with strong random."
            )
        # internal_api_key is optional in production. If present, log a warning
        # because it should only be used for server-to-server, never user-billable.
        if internal_api_key:
            logger.warning(
                "Production: 'taey_ed.internal_api_key' is set. Ensure it is only "
                "consumed on localhost/server-to-server endpoints, never user-billable."
            )
    else:
        # Dev mode: fall back to ephemeral if missing.
        if not jwt_secret:
            jwt_secret = _gen_ephemeral_jwt_secret()
            logger.warning(
                "DEV mode: no 'taey_ed.jwt_secret' in secrets file. Generated ephemeral "
                "value (will change on restart, invalidating existing tokens)."
            )

    return TaeyEdSecrets(
        jwt_secret=jwt_secret,
        internal_api_key=internal_api_key,
    )


def get_stripe_secrets() -> StripeSecrets:
    """Load Stripe credentials. Used by the billing module.

    Required keys: stripe.publishable_key, stripe.secret_key, stripe.webhook_secret.
    Production startup fails if any are missing.
    """
    raw = _load_raw()
    section = raw.get("stripe") or {}

    publishable_key = section.get("publishable_key", "")
    secret_key = section.get("secret_key", "")
    webhook_secret = section.get("webhook_secret", "")

    if is_production():
        missing = [
            k for k, v in (
                ("publishable_key", publishable_key),
                ("secret_key", secret_key),
                ("webhook_secret", webhook_secret),
            ) if not v
        ]
        if missing:
            raise SecretsError(
                f"Production startup: stripe section missing keys: {missing}. "
                f"Required at {SECRETS_PATH} under stripe.*"
            )

    return StripeSecrets(
        publishable_key=publishable_key,
        secret_key=secret_key,
        webhook_secret=webhook_secret,
    )


def validate_for_production() -> None:
    """Eager validation: call once at server startup. Raises SecretsError if
    production mode is set and any required secrets are missing/weak.

    Idempotent — caller is responsible for only invoking once.
    """
    # Triggering both loaders surfaces missing-secret errors before the app
    # accepts any traffic.
    get_taey_ed_secrets()
    if is_production():
        # Stripe only required in production (dev can defer billing wiring).
        get_stripe_secrets()
    logger.info(
        f"Secrets validation OK (mode={'PRODUCTION' if is_production() else 'DEV'})"
    )
