"""Shared dataclasses for Temporal workflow I/O.

These are used by both the API (Temporal client) and the worker (workflow/activities).
All types must be serializable via Temporal's default JSON data converter.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CheckoutInput:
    """Input to start a CheckoutWorkflow."""
    checkout_session_id: str
    user_id: str
    total_cents: int
    total_tokens: int
    currency: str = "usd"
    metadata: dict[str, str] = field(default_factory=dict)
    stripe_secret_key: str = ""
    db_path: str = ""


@dataclass
class PaymentInput:
    stripe_secret_key: str
    amount_cents: int
    currency: str
    metadata: dict[str, str]
    customer_id: str | None = None
    idempotency_key: str | None = None
    spt_token: str | None = None


@dataclass
class PaymentResult:
    payment_intent_id: str
    status: str
    amount: int


@dataclass
class FulfillInput:
    db_path: str
    user_id: str
    payment_intent_id: str
    amount_cents: int
    total_tokens: int
    currency: str = "usd"
    checkout_session_id: str = ""


@dataclass
class FulfillResult:
    new_balance: int
    was_new: bool
    credit_grant_id: str | None = None


@dataclass
class RefundInput:
    stripe_secret_key: str
    payment_intent_id: str
    checkout_session_id: str
    reason: str = ""


@dataclass
class RefundResult:
    refund_id: str
    status: str
    amount: int
    currency: str


@dataclass
class ReverseFulfillInput:
    db_path: str
    user_id: str
    payment_intent_id: str
    amount_cents: int


@dataclass
class ReverseFulfillResult:
    new_balance: int
    was_applied: bool


@dataclass
class CheckoutResult:
    status: str
    payment_intent_id: str | None = None
    order_id: str | None = None
    new_balance: int | None = None
    refund_id: str | None = None


TASK_QUEUE = "checkout-workflows"
