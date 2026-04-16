"""Stripe Billing Credits — transaction record via Credit Grants.

Stripe is the system of record for money movement:
  - PaymentIntents: track payments
  - Refunds: track reversals
  - Credit Grants: track purchased monetary value (created on payment, voided on refund)

SQLite is the system of record for real-time token balance (instant debits).
"""

import logging

import stripe

log = logging.getLogger(__name__)

_customer_cache: dict[str, str] = {}


def ensure_customer(user_id: str) -> str:
    """Get or create a Stripe Customer for the given user_id.

    Uses an in-process cache to avoid Stripe Search eventual-consistency
    race conditions that can create duplicate customers.
    """
    if user_id in _customer_cache:
        return _customer_cache[user_id]

    existing = stripe.Customer.search(
        query=f'metadata["poc_user_id"]:"{user_id}"',
        limit=1,
    )
    if existing.data:
        cid = existing.data[0].id
        _customer_cache[user_id] = cid
        return cid

    customer = stripe.Customer.create(
        name=f"POC User: {user_id}",
        metadata={"poc_user_id": user_id},
    )
    log.info("Created Stripe Customer %s for user %s", customer.id, user_id)
    _customer_cache[user_id] = customer.id
    return customer.id


def create_credit_grant(
    customer_id: str,
    amount_cents: int,
    currency: str = "usd",
    metadata: dict[str, str] | None = None,
) -> stripe.billing.CreditGrant:
    """Create a Credit Grant after a successful token purchase.

    The grant amount matches the payment amount in monetary terms.
    Serves as Stripe's record of what was purchased.
    """
    grant = stripe.billing.CreditGrant.create(
        customer=customer_id,
        name=f"Token purchase (${amount_cents / 100:.2f})",
        category="paid",
        amount={
            "type": "monetary",
            "monetary": {
                "value": amount_cents,
                "currency": currency,
            },
        },
        applicability_config={
            "scope": {"price_type": "metered"},
        },
        metadata=metadata or {},
    )
    log.info(
        "Created Credit Grant %s for %s: $%s %s",
        grant.id, customer_id, amount_cents / 100, currency,
    )
    return grant


def void_credit_grant(grant_id: str) -> stripe.billing.CreditGrant:
    """Void a credit grant (e.g., on refund).

    Only works if the grant hasn't been partially or fully applied to an invoice.
    Falls back to expire if void fails.
    """
    try:
        grant = stripe.billing.CreditGrant.void_grant(grant_id)
        log.info("Voided Credit Grant %s", grant_id)
        return grant
    except stripe.StripeError as e:
        log.warning("Void failed for %s (%s), trying expire", grant_id, e)
        grant = stripe.billing.CreditGrant.expire_grant(grant_id)
        log.info("Expired Credit Grant %s", grant_id)
        return grant
