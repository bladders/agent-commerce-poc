"""Fulfillment activity — credit the SQLite ledger + create Stripe Credit Grant."""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import stripe
from temporalio import activity

from temporal.shared import FulfillInput, FulfillResult

log = logging.getLogger(__name__)

_lock = threading.Lock()
_customer_cache: dict[str, str] = {}


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _add_tokens_idempotent(
    path: str, *, payment_intent_id: str, user_id: str, tokens: int
) -> tuple[int, bool]:
    with _lock:
        conn = _connect(path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM processed_payments WHERE payment_intent_id = ?",
                (payment_intent_id,),
            ).fetchone()
            if existing:
                row = conn.execute(
                    "SELECT tokens FROM balances WHERE user_id = ?", (user_id,)
                ).fetchone()
                bal = int(row["tokens"]) if row else 0
                return bal, False

            conn.execute(
                "INSERT INTO processed_payments (payment_intent_id, user_id, tokens) VALUES (?,?,?)",
                (payment_intent_id, user_id, tokens),
            )
            conn.execute(
                """
                INSERT INTO balances (user_id, tokens) VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET tokens = tokens + excluded.tokens
                """,
                (user_id, tokens),
            )
            row = conn.execute(
                "SELECT tokens FROM balances WHERE user_id = ?", (user_id,)
            ).fetchone()
            conn.commit()
            return int(row["tokens"]), True
        finally:
            conn.close()


def _ensure_customer(user_id: str) -> str:
    if user_id in _customer_cache:
        return _customer_cache[user_id]

    existing = stripe.Customer.search(
        query=f'metadata["poc_user_id"]:"{user_id}"', limit=1
    )
    if existing.data:
        cid = existing.data[0].id
        _customer_cache[user_id] = cid
        return cid

    customer = stripe.Customer.create(
        name=f"POC User: {user_id}", metadata={"poc_user_id": user_id}
    )
    log.info("Created Stripe Customer %s for user %s", customer.id, user_id)
    _customer_cache[user_id] = customer.id
    return customer.id


@activity.defn
async def fulfill_payment(input: FulfillInput) -> FulfillResult:
    """Idempotent fulfillment: credit ledger + create Stripe Credit Grant."""
    new_bal, was_new = _add_tokens_idempotent(
        input.db_path,
        payment_intent_id=input.payment_intent_id,
        user_id=input.user_id,
        tokens=input.amount_cents,
    )

    if not was_new:
        log.info(
            "FULFILL %s — already processed (balance=$%.2f)",
            input.payment_intent_id, new_bal / 100,
        )
        return FulfillResult(new_balance=new_bal, was_new=False)

    log.info(
        "FULFILL %s — credited $%.2f (balance=$%.2f)",
        input.payment_intent_id, input.amount_cents / 100, new_bal / 100,
    )

    credit_grant_id = None
    try:
        cust_id = _ensure_customer(input.user_id)
        grant = stripe.billing.CreditGrant.create(
            customer=cust_id,
            name=f"Token purchase (${input.amount_cents / 100:.2f})",
            category="paid",
            amount={
                "type": "monetary",
                "monetary": {"value": input.amount_cents, "currency": input.currency},
            },
            applicability_config={"scope": {"price_type": "metered"}},
            metadata={
                "payment_intent_id": input.payment_intent_id,
                "checkout_session_id": input.checkout_session_id,
                "tokens": str(input.total_tokens),
                "poc_user_id": input.user_id,
            },
        )
        credit_grant_id = grant.id
        log.info("FULFILL %s — Credit Grant %s", input.payment_intent_id, grant.id)
    except Exception as e:
        log.warning("FULFILL %s — Credit Grant creation skipped: %s", input.payment_intent_id, e)

    return FulfillResult(
        new_balance=new_bal, was_new=True, credit_grant_id=credit_grant_id
    )
