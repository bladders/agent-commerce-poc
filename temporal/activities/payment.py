"""Payment activities — create and confirm Stripe PaymentIntents."""

from __future__ import annotations

import logging
import time

import httpx
import stripe
from temporalio import activity

from temporal.shared import PaymentInput, PaymentResult

log = logging.getLogger(__name__)


def _try_spt_test_helper(
    stripe_secret_key: str, amount_cents: int, currency: str
) -> str | None:
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


@activity.defn
async def create_payment_intent(input: PaymentInput) -> PaymentResult:
    """Create and confirm a Stripe PaymentIntent."""
    stripe.api_key = input.stripe_secret_key

    common: dict = {
        "amount": input.amount_cents,
        "currency": input.currency,
        "confirm": True,
        "metadata": input.metadata,
    }
    if input.customer_id:
        common["customer"] = input.customer_id

    kwargs = {}
    if input.idempotency_key:
        kwargs["idempotency_key"] = input.idempotency_key

    if input.spt_token:
        log.info("Using provided SPT %s", input.spt_token)
        pi = stripe.PaymentIntent.create(
            **common,
            payment_method_data={"shared_payment_granted_token": input.spt_token},
            **kwargs,
        )
    else:
        auto_spt = _try_spt_test_helper(
            input.stripe_secret_key, input.amount_cents, input.currency
        )
        if auto_spt:
            log.info("Using auto-generated SPT %s", auto_spt)
            pi = stripe.PaymentIntent.create(
                **common,
                payment_method_data={"shared_payment_granted_token": auto_spt},
                **kwargs,
            )
        else:
            log.info("Using pm_card_visa fallback (test mode)")
            pi = stripe.PaymentIntent.create(
                **common,
                payment_method="pm_card_visa",
                automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                **kwargs,
            )

    log.info("Created PaymentIntent %s (status=%s)", pi.id, pi.status)
    return PaymentResult(payment_intent_id=pi.id, status=pi.status, amount=pi.amount)


@activity.defn
async def confirm_payment(payment_intent_id: str) -> PaymentResult:
    """Confirm a PaymentIntent that requires additional action.

    In test mode, PaymentIntents usually auto-confirm. This activity
    handles the edge case where status is 'requires_confirmation'.
    """
    pi = stripe.PaymentIntent.retrieve(payment_intent_id)
    if pi.status in ("succeeded", "processing"):
        return PaymentResult(payment_intent_id=pi.id, status=pi.status, amount=pi.amount)

    if pi.status == "requires_confirmation":
        pi = stripe.PaymentIntent.confirm(payment_intent_id)
        log.info("Confirmed PaymentIntent %s -> %s", pi.id, pi.status)

    return PaymentResult(payment_intent_id=pi.id, status=pi.status, amount=pi.amount)
