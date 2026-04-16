"""Stripe payment for the ACP seller.

Production: agent passes a SharedPaymentToken (SPT) in payment_data.token;
seller creates PaymentIntent with payment_method_data.shared_payment_granted_token.

POC: if no SPT is provided, we fall back to pm_card_visa so the demo works on
any Stripe test-mode account.
"""

from __future__ import annotations

import logging
import time

import httpx
import stripe

log = logging.getLogger(__name__)


def _try_spt_test_helper(
    stripe_secret_key: str,
    amount_cents: int,
    currency: str,
) -> str | None:
    """Try the SPT test helper. Returns spt_id or None if unavailable."""
    expires_at = int(time.time()) + 3600
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                "https://api.stripe.com/v1/test_helpers/shared_payment/granted_tokens",
                auth=(stripe_secret_key, ""),
                data={
                    "payment_method": "pm_card_visa",
                    "usage_limits[currency]": currency,
                    "usage_limits[max_amount]": str(amount_cents),
                    "usage_limits[expires_at]": str(expires_at),
                },
            )
        if resp.status_code < 400:
            return resp.json()["id"]
        log.info("SPT test helper unavailable (%s), falling back", resp.status_code)
    except Exception as exc:
        log.info("SPT test helper call failed (%s), falling back", exc)
    return None


def create_payment(
    *,
    stripe_secret_key: str,
    amount_cents: int,
    currency: str,
    metadata: dict[str, str],
    customer: str | None = None,
    idempotency_key: str | None = None,
    spt_token: str | None = None,
) -> stripe.PaymentIntent:
    """Create and confirm a PaymentIntent.

    Priority: explicit spt_token > SPT test helper > pm_card_visa fallback.
    """
    stripe.api_key = stripe_secret_key

    common: dict = {
        "amount": amount_cents,
        "currency": currency,
        "confirm": True,
        "metadata": metadata,
    }
    if customer:
        common["customer"] = customer

    kwargs = {}
    if idempotency_key:
        kwargs["idempotency_key"] = idempotency_key

    if spt_token:
        log.info("Using provided SPT %s (customer=%s)", spt_token, customer)
        return stripe.PaymentIntent.create(
            **common,
            payment_method_data={"shared_payment_granted_token": spt_token},
            **kwargs,
        )

    auto_spt = _try_spt_test_helper(stripe_secret_key, amount_cents, currency)
    if auto_spt:
        log.info("Using auto-generated SPT %s (customer=%s)", auto_spt, customer)
        return stripe.PaymentIntent.create(
            **common,
            payment_method_data={"shared_payment_granted_token": auto_spt},
            **kwargs,
        )

    log.info("Using pm_card_visa fallback (test mode, customer=%s)", customer)
    return stripe.PaymentIntent.create(
        **common,
        payment_method="pm_card_visa",
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        **kwargs,
    )


def verify_webhook_payload(
    *, payload: bytes, sig_header: str | None, webhook_secret: str
) -> stripe.Event:
    if not sig_header:
        raise ValueError("Missing Stripe-Signature header")
    return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
