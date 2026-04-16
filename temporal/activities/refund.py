"""Refund and compensation activities."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

import stripe
from temporalio import activity

from temporal.shared import (
    RefundInput,
    RefundResult,
    ReverseFulfillInput,
    ReverseFulfillResult,
)

log = logging.getLogger(__name__)


def _get_stripe_key() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set in worker environment")
    return key

_lock = threading.Lock()
_customer_cache: dict[str, str] = {}


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@activity.defn
async def process_refund(input: RefundInput) -> RefundResult:
    """Create a Stripe Refund for a PaymentIntent."""
    stripe.api_key = _get_stripe_key()
    refund = stripe.Refund.create(
        payment_intent=input.payment_intent_id,
        reason="requested_by_customer",
        metadata={
            "checkout_session_id": input.checkout_session_id,
            "refund_reason": input.reason,
        },
    )
    log.info("REFUND %s — refund %s (%s)", input.payment_intent_id, refund.id, refund.status)
    return RefundResult(
        refund_id=refund.id,
        status=refund.status,
        amount=refund.amount,
        currency=refund.currency,
    )


@activity.defn
async def reverse_fulfillment(input: ReverseFulfillInput) -> ReverseFulfillResult:
    """Reverse ledger credit + void Stripe Credit Grant (saga compensation)."""
    with _lock:
        conn = _connect(input.db_path)
        try:
            existing = conn.execute(
                "SELECT tokens FROM processed_payments WHERE payment_intent_id = ?",
                (input.payment_intent_id,),
            ).fetchone()
            if not existing:
                row = conn.execute(
                    "SELECT tokens FROM balances WHERE user_id = ?", (input.user_id,)
                ).fetchone()
                bal = int(row["tokens"]) if row else 0
                return ReverseFulfillResult(new_balance=bal, was_applied=False)

            credited = int(existing["tokens"])
            conn.execute(
                "DELETE FROM processed_payments WHERE payment_intent_id = ?",
                (input.payment_intent_id,),
            )
            conn.execute(
                "UPDATE balances SET tokens = MAX(0, tokens - ?) WHERE user_id = ?",
                (credited, input.user_id),
            )
            row = conn.execute(
                "SELECT tokens FROM balances WHERE user_id = ?", (input.user_id,)
            ).fetchone()
            conn.commit()
            new_bal = int(row["tokens"])
        finally:
            conn.close()

    log.info(
        "REVERSE %s — deducted $%.2f (balance=$%.2f)",
        input.payment_intent_id, input.amount_cents / 100, new_bal / 100,
    )

    try:
        if input.user_id in _customer_cache:
            cust_id = _customer_cache[input.user_id]
        else:
            existing_cust = stripe.Customer.search(
                query=f'metadata["poc_user_id"]:"{input.user_id}"', limit=1
            )
            cust_id = existing_cust.data[0].id if existing_cust.data else None

        if cust_id:
            grants = stripe.billing.CreditGrant.list(customer=cust_id, limit=100)
            for g in grants.data:
                if g.metadata.get("payment_intent_id") == input.payment_intent_id:
                    try:
                        stripe.billing.CreditGrant.void_grant(g.id)
                        log.info("REVERSE %s — voided Credit Grant %s", input.payment_intent_id, g.id)
                    except stripe.StripeError:
                        stripe.billing.CreditGrant.expire_grant(g.id)
                        log.info("REVERSE %s — expired Credit Grant %s", input.payment_intent_id, g.id)
                    break
    except Exception as e:
        log.warning("REVERSE %s — Credit Grant void skipped: %s", input.payment_intent_id, e)

    return ReverseFulfillResult(new_balance=new_bal, was_applied=True)
