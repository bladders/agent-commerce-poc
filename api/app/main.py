"""ACP-compliant seller API for digital token packs — FastAPI.

Implements the Agentic Commerce Protocol (version 2026-01-30):
  POST   /checkout_sessions              — create (supports multiple line_items)
  GET    /checkout_sessions/{id}         — retrieve
  POST   /checkout_sessions/{id}         — update
  POST   /checkout_sessions/{id}/complete — complete (returns order)
  POST   /checkout_sessions/{id}/cancel   — cancel

Plus POC-specific endpoints outside the ACP spec:
  GET    /api/v1/catalog   — browse token packs
  GET    /api/v1/balance   — check token balance
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any

import stripe
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.catalog import TokenPack, get_pack, list_catalog, load_catalog
from app.config import Settings, get_settings
from app.credits import (
    create_credit_grant,
    ensure_customer,
    void_credit_grant,
)
from app.ledger import add_tokens_idempotent, deduct_tokens, get_balance, init_db, set_balance
from app.stripe_service import create_payment, verify_webhook_payload
from app import temporal_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

ACP_VERSION = "2026-01-30"

# ---------------------------------------------------------------------------
# ACP checkout session (in-memory store) — supports multiple line items
# ---------------------------------------------------------------------------


@dataclass
class SessionLineItem:
    pack: TokenPack
    quantity: int = 1


@dataclass
class CheckoutSession:
    id: str
    user_id: str
    items: list[SessionLineItem]
    status: str = "ready_for_payment"
    payment_intent_id: str | None = None
    order_id: str | None = None
    intent_trace: dict[str, Any] | None = None
    merchant_policy: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    workflow_id: str | None = None

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    @property
    def total_amount(self) -> int:
        return sum(li.pack.amount_cents * li.quantity for li in self.items)

    @property
    def total_tokens(self) -> int:
        return sum(li.pack.tokens * li.quantity for li in self.items)

    @property
    def currency(self) -> str:
        return self.items[0].pack.currency if self.items else "usd"


_sessions: dict[str, CheckoutSession] = {}


def _capabilities() -> dict:
    """ACP capabilities object — declares our payment handler."""
    return {
        "payment": {
            "handlers": [
                {
                    "id": "handler_stripe_card",
                    "name": "dev.acp.tokenized.card",
                    "version": ACP_VERSION,
                    "spec": "https://docs.stripe.com/agentic-commerce/protocol/specification",
                    "requires_delegate_payment": False,
                    "requires_pci_compliance": False,
                    "psp": "stripe",
                    "config_schema": "",
                    "instrument_schemas": [],
                    "config": {},
                }
            ]
        },
        "interventions": {"supported": []},
        "extensions": [],
    }


def _acp_response(sess: CheckoutSession, order: dict | None = None) -> dict:
    """Build an ACP-spec CheckoutSession (or CheckoutSessionWithOrder) response."""
    sess.updated_at = datetime.now(timezone.utc).isoformat()

    line_items_out: list[dict[str, Any]] = []
    all_li_ids: list[str] = []
    for idx, sli in enumerate(sess.items):
        p = sli.pack
        li_id = f"li_{sess.id[3:9]}_{idx}"
        all_li_ids.append(li_id)
        li_subtotal = p.amount_cents * sli.quantity
        line_items_out.append({
            "id": li_id,
            "item": {"id": p.pack_id, "name": p.label, "unit_amount": p.amount_cents},
            "quantity": sli.quantity,
            "name": p.label,
            "description": p.description,
            "unit_amount": p.amount_cents,
            "availability_status": "in_stock",
            "totals": [
                {"type": "subtotal", "display_text": "Subtotal", "amount": li_subtotal},
            ],
        })

    total_amount = sess.total_amount
    total_tokens = sess.total_tokens

    fulfillment_option: dict[str, Any] = {
        "type": "digital",
        "id": "digital_instant",
        "title": "Instant delivery",
        "description": f"{total_tokens} tokens credited immediately",
        "totals": [
            {"type": "fulfillment", "display_text": "Digital Delivery", "amount": 0},
        ],
    }

    resp: dict[str, Any] = {
        "id": sess.id,
        "protocol": {"version": ACP_VERSION},
        "capabilities": _capabilities(),
        "status": sess.status,
        "currency": sess.currency,
        "line_items": line_items_out,
        "fulfillment_options": [fulfillment_option],
        "selected_fulfillment_options": [
            {"type": "digital", "option_id": "digital_instant", "item_ids": all_li_ids},
        ],
        "totals": [
            {"type": "subtotal", "display_text": "Subtotal", "amount": total_amount},
            {"type": "total", "display_text": "Total", "amount": total_amount},
        ],
        "messages": sess.messages,
        "links": [],
        "created_at": sess.created_at,
        "updated_at": sess.updated_at,
    }

    if order:
        resp["order"] = order

    if sess.intent_trace:
        resp["intent_trace"] = sess.intent_trace

    if sess.merchant_policy:
        resp["merchant_policy"] = sess.merchant_policy

    item_labels = ", ".join(
        f"{sli.quantity}x {sli.pack.label}" if sli.quantity > 1 else sli.pack.label
        for sli in sess.items
    )
    resp["_poc"] = {
        "user_id": sess.user_id,
        "pack_label": item_labels,
        "tokens": total_tokens,
        "payment_intent_id": sess.payment_intent_id,
    }

    return resp


# ---------------------------------------------------------------------------
# Request models (ACP-compliant)
# ---------------------------------------------------------------------------


class ACPItem(BaseModel):
    id: str
    name: str | None = None
    unit_amount: int | None = None
    quantity: int = 1


class ACPCredential(BaseModel):
    type: str = "spt"
    token: str | None = None


class ACPInstrument(BaseModel):
    type: str = "card"
    credential: ACPCredential | None = None


class ACPPaymentData(BaseModel):
    handler_id: str | None = None
    instrument: ACPInstrument | None = None
    billing_address: dict | None = None


class ACPCapabilities(BaseModel):
    payment: dict | None = None
    interventions: dict | None = None
    extensions: list | None = None


class CreateCheckoutBody(BaseModel):
    line_items: list[ACPItem] = Field(default_factory=list)
    currency: str = "usd"
    capabilities: ACPCapabilities | None = None
    buyer: dict | None = None
    fulfillment_details: dict | None = None
    merchant_policy: dict | None = None
    # POC convenience shorthand (single item)
    pack_id: str | None = Field(None, description="Shorthand: pass pack_id instead of line_items")
    user_id: str = Field("demo_user", description="POC user id for the token ledger")


class UpdateCheckoutBody(BaseModel):
    line_items: list[ACPItem] | None = None
    buyer: dict | None = None
    fulfillment_details: dict | None = None
    selected_fulfillment_options: list | None = None
    pack_id: str | None = None


class CompleteCheckoutBody(BaseModel):
    payment_data: ACPPaymentData | None = None
    buyer: dict | None = None


class CancelSessionBody(BaseModel):
    intent_trace: dict | None = None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def require_bearer(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
    x_poc_key: Annotated[str | None, Header()] = None,
) -> None:
    """Accept either Authorization: Bearer <key> or legacy X-POC-Key header."""
    key = settings.poc_api_key
    if not key:
        return
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_poc_key:
        token = x_poc_key
    if token != key:
        raise HTTPException(status_code=401, detail="Missing or invalid authentication credentials")


def get_db_path(settings: Annotated[Settings, Depends(get_settings)]) -> str:
    return settings.database_path


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Agent Commerce POC — ACP Seller", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    settings = get_settings()
    init_db(settings.database_path)
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
        log.info("Stripe configured (key prefix: %s…)", settings.stripe_secret_key[:12])
        try:
            load_catalog(settings.stripe_secret_key)
        except Exception as e:
            log.error("Failed to load catalog from Stripe: %s", e)
    else:
        log.warning("STRIPE_SECRET_KEY is empty — set it in .env then restart")
    if settings.temporal_address:
        temporal_client.configure(settings.temporal_address)
        log.info("Temporal configured at %s", settings.temporal_address)
    else:
        log.info("Temporal not configured — using inline payment path")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/config")
def health_config(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    key = settings.stripe_secret_key
    return {
        "stripe_secret_key_configured": bool(key),
        "stripe_key_prefix": (key[:12] + "…") if key and len(key) >= 12 else None,
    }


# ---------------------------------------------------------------------------
# Catalog (POC-specific, not part of ACP spec)
# ---------------------------------------------------------------------------


@app.get("/api/v1/catalog", dependencies=[Depends(require_bearer)])
def get_catalog() -> dict:
    return {
        "items": [
            {
                "id": p.pack_id,
                "product_id": p.product_id,
                "name": p.label,
                "description": p.description,
                "tokens": p.tokens,
                "amount": p.amount_cents,
                "currency": p.currency,
            }
            for p in list_catalog()
        ]
    }


# ---------------------------------------------------------------------------
# ACP Checkout Session endpoints (multi-item support)
# ---------------------------------------------------------------------------


def _resolve_items(line_items: list[ACPItem] | None, pack_id: str | None) -> list[SessionLineItem]:
    """Resolve SessionLineItems from ACP line_items array or shorthand pack_id."""
    resolved: list[SessionLineItem] = []

    if line_items:
        for acp_item in line_items:
            pack = get_pack(acp_item.id)
            if not pack:
                raise HTTPException(status_code=400, detail=f"Unknown item: {acp_item.id}")
            resolved.append(SessionLineItem(pack=pack, quantity=acp_item.quantity))

    if not resolved and pack_id:
        pack = get_pack(pack_id)
        if not pack:
            raise HTTPException(status_code=400, detail=f"Unknown item: {pack_id}")
        resolved.append(SessionLineItem(pack=pack, quantity=1))

    if not resolved:
        raise HTTPException(status_code=400, detail="Provide at least one item in line_items")

    return resolved


def _enforce_policy_on_create(sess: CheckoutSession) -> None:
    """Check merchant_policy constraints on session creation. Raises HTTPException on violation."""
    policy = sess.merchant_policy
    if not policy:
        return

    total_tokens = sess.total_tokens
    total_amount = sess.total_amount
    num_items = len(sess.items)

    max_tokens = policy.get("max_tokens_per_session", 0)
    if max_tokens and total_tokens > max_tokens:
        raise HTTPException(
            status_code=422,
            detail=f"Policy violation: max {max_tokens} tokens per session (requested {total_tokens})",
        )

    max_amount = policy.get("max_amount_cents_per_session", 0)
    if max_amount and total_amount > max_amount:
        raise HTTPException(
            status_code=422,
            detail=f"Policy violation: max ${max_amount / 100:.2f} per session (total ${total_amount / 100:.2f})",
        )

    min_amount = policy.get("min_amount_cents_per_session", 0)
    if min_amount and total_amount < min_amount:
        raise HTTPException(
            status_code=422,
            detail=f"Policy violation: minimum ${min_amount / 100:.2f} per session (total ${total_amount / 100:.2f})",
        )

    max_items = policy.get("max_items_per_session", 0)
    if max_items and num_items > max_items:
        raise HTTPException(
            status_code=422,
            detail=f"Policy violation: max {max_items} distinct items per session (got {num_items})",
        )


@app.post("/checkout_sessions", dependencies=[Depends(require_bearer)], status_code=201)
def create_checkout_session(req: CreateCheckoutBody = Body()) -> dict:
    items = _resolve_items(req.line_items or None, req.pack_id)
    cid = f"cs_{uuid.uuid4().hex[:24]}"
    sess = CheckoutSession(id=cid, user_id=req.user_id, items=items, merchant_policy=req.merchant_policy)
    _enforce_policy_on_create(sess)
    _sessions[cid] = sess
    log.info("CREATE %s | policy=%s", cid, json.dumps(req.merchant_policy) if req.merchant_policy else "none")
    return _acp_response(sess)


@app.get("/checkout_sessions/{checkout_session_id}", dependencies=[Depends(require_bearer)])
def retrieve_checkout_session(checkout_session_id: str) -> dict:
    sess = _sessions.get(checkout_session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Checkout session does not exist")
    return _acp_response(sess)


@app.post("/checkout_sessions/{checkout_session_id}", dependencies=[Depends(require_bearer)])
def update_checkout_session(
    checkout_session_id: str,
    req: UpdateCheckoutBody = Body(),
) -> dict:
    sess = _sessions.get(checkout_session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Checkout session does not exist")
    if sess.status not in ("ready_for_payment", "not_ready_for_payment", "incomplete"):
        raise HTTPException(status_code=422, detail="Checkout session not editable in current status")
    if req.line_items or req.pack_id:
        sess.items = _resolve_items(req.line_items, req.pack_id)
    return _acp_response(sess)


@app.post("/checkout_sessions/{checkout_session_id}/cancel", dependencies=[Depends(require_bearer)])
def cancel_checkout_session(
    checkout_session_id: str,
    req: CancelSessionBody = Body(CancelSessionBody()),
) -> dict:
    sess = _sessions.get(checkout_session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Checkout session does not exist")
    if sess.status in ("completed", "canceled"):
        raise HTTPException(status_code=405, detail="Checkout session is already completed or canceled")

    policy = sess.merchant_policy or {}
    if policy.get("require_cancel_reason", False) and not req.intent_trace:
        raise HTTPException(
            status_code=422,
            detail="Policy violation: cancellation reason required. Provide intent_trace with reason_code and trace_summary.",
        )

    sess.status = "canceled"
    if req.intent_trace:
        sess.intent_trace = req.intent_trace
    reason = req.intent_trace.get("reason_code", "unknown") if req.intent_trace else "unknown"
    summary = req.intent_trace.get("trace_summary", "") if req.intent_trace else ""
    cancel_msg = "Checkout session canceled."
    if summary:
        cancel_msg = f"Checkout session canceled: {summary}"
    sess.messages.append({
        "type": "info",
        "severity": "info",
        "content_type": "plain",
        "content": cancel_msg,
    })
    log.info("CANCEL %s | reason=%s | summary=%s", checkout_session_id, reason, summary)
    return _acp_response(sess)


def _find_session_by_pi(payment_intent_id: str) -> CheckoutSession | None:
    """Reverse-lookup a session by its PaymentIntent ID."""
    for sess in _sessions.values():
        if sess.payment_intent_id == payment_intent_id:
            return sess
    return None


def _fulfill_payment(db_path: str, sess: CheckoutSession, pi_id: str) -> None:
    """Idempotent fulfillment: credit SQLite ledger + create Stripe Credit Grant.

    Called by the synchronous path (test mode) and by the webhook
    (production path). Safe to call twice — the ledger is idempotent
    on payment_intent_id.

    Credits the monetary amount (cents) to the ledger, NOT token count,
    so the balance stays in consistent cents units.
    """
    amount_cents = sess.total_amount
    new_bal, was_new = add_tokens_idempotent(
        db_path,
        payment_intent_id=pi_id,
        user_id=sess.user_id,
        tokens=amount_cents,
    )
    if not was_new:
        log.info("FULFILL %s — already processed (balance=$%.2f)", pi_id, new_bal / 100)
        return

    log.info("FULFILL %s — credited $%.2f (balance=$%.2f)", pi_id, amount_cents / 100, new_bal / 100)

    try:
        cust_id = ensure_customer(sess.user_id)
        grant = create_credit_grant(
            customer_id=cust_id,
            amount_cents=sess.total_amount,
            currency=sess.currency,
            metadata={
                "payment_intent_id": pi_id,
                "checkout_session_id": sess.id,
                "tokens": str(sess.total_tokens),
                "poc_user_id": sess.user_id,
            },
        )
        log.info("FULFILL %s — Credit Grant %s", pi_id, grant.id)
    except Exception as e:
        log.warning("FULFILL %s — Credit Grant creation skipped: %s", pi_id, e)


def _reverse_payment(db_path: str, pi_id: str, user_id: str, amount_cents: int) -> None:
    """Reverse fulfillment on refund: deduct SQLite ledger + void Credit Grant.

    amount_cents is the monetary value to reverse (the ledger's idempotency
    will use the originally-credited amount from processed_payments).
    """
    new_bal, was_applied = deduct_tokens(
        db_path,
        payment_intent_id=pi_id,
        user_id=user_id,
        tokens=amount_cents,
    )
    if was_applied:
        log.info("REVERSE %s — deducted $%.2f (balance=$%.2f)", pi_id, amount_cents / 100, new_bal / 100)
    else:
        log.info("REVERSE %s — already reversed or never credited", pi_id)

    try:
        cust_id = ensure_customer(user_id)
        grants = stripe.billing.CreditGrant.list(customer=cust_id, limit=100)
        for g in grants.data:
            if g.metadata.get("payment_intent_id") == pi_id:
                void_credit_grant(g.id)
                log.info("REVERSE %s — voided Credit Grant %s", pi_id, g.id)
                break
    except Exception as e:
        log.warning("REVERSE %s — Credit Grant void skipped: %s", pi_id, e)


@app.post("/checkout_sessions/{checkout_session_id}/complete", dependencies=[Depends(require_bearer)])
async def complete_checkout_session(
    checkout_session_id: str,
    req: CompleteCheckoutBody = Body(CompleteCheckoutBody()),
    settings: Annotated[Settings, Depends(get_settings)] = ...,
    db_path: Annotated[str, Depends(get_db_path)] = ...,
) -> dict:
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=503,
            detail="STRIPE_SECRET_KEY not configured — add it to .env and restart.",
        )

    sess = _sessions.get(checkout_session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Checkout session does not exist")
    if sess.status == "completed":
        raise HTTPException(status_code=409, detail="Checkout session already completed")
    if sess.status == "canceled":
        raise HTTPException(status_code=409, detail="Checkout session already canceled")

    spt_token = None
    if req.payment_data and req.payment_data.instrument and req.payment_data.instrument.credential:
        spt_token = req.payment_data.instrument.credential.token

    total_amount = sess.total_amount
    total_tokens = sess.total_tokens
    item_ids = [sli.pack.pack_id for sli in sess.items]

    metadata = {
        "poc_user_id": sess.user_id,
        "tokens": str(total_tokens),
        "amount_cents": str(total_amount),
        "checkout_session_id": sess.id,
        "item_ids": ",".join(item_ids),
    }

    # --- Try Temporal workflow path ---
    workflow_id = f"checkout_{sess.id}"
    temporal_result = await _try_temporal_complete(
        workflow_id=workflow_id,
        sess=sess,
        settings=settings,
        db_path=db_path,
        metadata=metadata,
    )

    if temporal_result is not None:
        sess.payment_intent_id = temporal_result.payment_intent_id
        sess.order_id = temporal_result.order_id
        sess.workflow_id = workflow_id
        sess.status = "completed" if temporal_result.status == "fulfilled" else "payment_pending"
        order_status = "confirmed" if sess.status == "completed" else "processing"
        order = {
            "id": sess.order_id or f"ord_{uuid.uuid4().hex[:16]}",
            "checkout_session_id": sess.id,
            "permalink_url": f"/orders/{sess.order_id}",
            "status": order_status,
        }
        resp = _acp_response(sess, order=order)
        if temporal_result.new_balance is not None:
            resp["_poc"]["balance_tokens"] = temporal_result.new_balance
        log.info("COMPLETE %s via Temporal workflow %s", sess.id, workflow_id)
        return resp

    # --- Inline fallback (Temporal unavailable) ---
    log.info("COMPLETE %s via inline path (Temporal unavailable)", sess.id)
    return _inline_complete(sess, settings, db_path, spt_token, metadata)


async def _try_temporal_complete(
    workflow_id: str,
    sess: CheckoutSession,
    settings: Settings,
    db_path: str,
    metadata: dict[str, str],
) -> Any:
    """Attempt to run checkout via Temporal. Returns CheckoutResult or None."""
    try:
        handle = await temporal_client.start_checkout_workflow(
            workflow_id,
            temporal_client.CheckoutInput(
                checkout_session_id=sess.id,
                user_id=sess.user_id,
                total_cents=sess.total_amount,
                total_tokens=sess.total_tokens,
                currency=sess.currency,
                metadata=metadata,
                stripe_secret_key=settings.stripe_secret_key,
                db_path=db_path,
            ),
        )
        if handle is None:
            return None
        result = await temporal_client.poll_until_fulfilled(workflow_id, timeout=30.0)
        return result
    except Exception as e:
        log.warning("Temporal checkout failed for %s, falling back: %s", sess.id, e)
        return None


def _inline_complete(
    sess: CheckoutSession,
    settings: Settings,
    db_path: str,
    spt_token: str | None,
    metadata: dict[str, str],
) -> dict:
    """Original inline payment path — used when Temporal is unavailable."""
    try:
        cust_id = ensure_customer(sess.user_id)
    except Exception:
        cust_id = None

    idempotency_key = f"pi_{sess.id}"

    try:
        pi = create_payment(
            stripe_secret_key=settings.stripe_secret_key,
            amount_cents=sess.total_amount,
            currency=sess.currency,
            metadata=metadata,
            customer=cust_id,
            idempotency_key=idempotency_key,
            spt_token=spt_token,
        )
    except stripe.StripeError as e:
        raise HTTPException(
            status_code=402,
            detail=getattr(e, "user_message", None) or str(e),
        ) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment failed: {e!s}") from e

    sess.payment_intent_id = pi.id

    order_id = f"ord_{uuid.uuid4().hex[:16]}"
    sess.order_id = order_id

    if pi.status == "succeeded":
        _fulfill_payment(db_path, sess, pi.id)
        sess.status = "completed"
        order_status = "confirmed"
    elif pi.status == "requires_action":
        sess.status = "payment_pending"
        order_status = "pending"
        log.info("PAYMENT PENDING %s — requires additional action (3DS etc.)", sess.id)
    else:
        sess.status = "payment_pending"
        order_status = "processing"
        log.info("PAYMENT PROCESSING %s — status=%s", sess.id, pi.status)

    order = {
        "id": order_id,
        "checkout_session_id": sess.id,
        "permalink_url": f"/orders/{order_id}",
        "status": order_status,
    }

    resp = _acp_response(sess, order=order)
    if sess.status == "completed":
        resp["_poc"]["balance_tokens"] = get_balance(db_path, sess.user_id)
    return resp


# ---------------------------------------------------------------------------
# Balance (POC-specific, not part of ACP spec)
# ---------------------------------------------------------------------------


@app.get("/api/v1/balance", dependencies=[Depends(require_bearer)])
def read_balance(
    user_id: str = "demo_user",
    db_path: Annotated[str, Depends(get_db_path)] = ...,
) -> dict:
    bal = get_balance(db_path, user_id)
    return {
        "user_id": user_id,
        "balance_cents": bal,
        "balance_display": f"${bal / 100:.2f}",
    }


DEMO_SESSION_BALANCE = 2000  # $20.00 in cents


class ResetBalanceBody(BaseModel):
    user_id: str = "demo_user"
    amount: int = Field(default=DEMO_SESSION_BALANCE, description="Balance in cents to reset to")


@app.post("/api/v1/reset-balance", dependencies=[Depends(require_bearer)])
def reset_balance(
    req: ResetBalanceBody = Body(),
    db_path: Annotated[str, Depends(get_db_path)] = ...,
) -> dict:
    new_bal = set_balance(db_path, req.user_id, req.amount)
    log.info("RESET-BALANCE user=%s → %d cents ($%.2f)", req.user_id, new_bal, new_bal / 100)
    return {"user_id": req.user_id, "balance": new_bal}


# ---------------------------------------------------------------------------
# Consume (token burn — deducts SQLite balance in cents)
# ---------------------------------------------------------------------------


class ConsumeBody(BaseModel):
    user_id: str = "demo_user"
    tokens: int = Field(ge=1, description="Amount in cents to consume")
    reason: str = "llm_usage"


@app.post("/api/v1/consume", dependencies=[Depends(require_bearer)])
def consume_tokens(
    req: ConsumeBody = Body(),
    db_path: Annotated[str, Depends(get_db_path)] = ...,
) -> dict:
    current_balance = get_balance(db_path, req.user_id)
    if current_balance < req.tokens:
        raise HTTPException(
            status_code=422,
            detail=f"Insufficient balance: have ${current_balance/100:.2f}, need ${req.tokens/100:.2f}",
        )

    from app.ledger import _connect, _lock

    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute(
                "UPDATE balances SET tokens = MAX(0, tokens - ?) WHERE user_id = ?",
                (req.tokens, req.user_id),
            )
            row = conn.execute(
                "SELECT tokens FROM balances WHERE user_id = ?", (req.user_id,),
            ).fetchone()
            conn.commit()
            new_balance = int(row["tokens"]) if row else 0
        finally:
            conn.close()

    log.info(
        "CONSUME user=%s | tokens=%d | reason=%s | balance=%d",
        req.user_id, req.tokens, req.reason, new_balance,
    )
    return {
        "user_id": req.user_id,
        "tokens_consumed": req.tokens,
        "balance": new_balance,
        "reason": req.reason,
    }


# ---------------------------------------------------------------------------
# Refund (POC-specific — wraps Stripe refund API)
# ---------------------------------------------------------------------------


class RefundBody(BaseModel):
    checkout_session_id: str
    reason: str | None = None


@app.post("/api/v1/refund", dependencies=[Depends(require_bearer)])
async def create_refund(
    req: RefundBody = Body(),
    settings: Annotated[Settings, Depends(get_settings)] = ...,
    db_path: Annotated[str, Depends(get_db_path)] = ...,
) -> dict:
    sess = _sessions.get(req.checkout_session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Checkout session does not exist")
    if sess.status != "completed":
        raise HTTPException(status_code=422, detail="Only completed sessions can be refunded")
    if not sess.payment_intent_id:
        raise HTTPException(status_code=422, detail="No payment to refund")

    policy = sess.merchant_policy or {}
    refund_window = policy.get("refund_window_minutes", -1)
    if refund_window == 0:
        raise HTTPException(
            status_code=422,
            detail="Policy violation: refunds are not allowed by this merchant.",
        )
    if refund_window > 0:
        from datetime import datetime, timezone
        created = datetime.fromisoformat(sess.created_at)
        elapsed_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
        if elapsed_min > refund_window:
            raise HTTPException(
                status_code=422,
                detail=f"Policy violation: refund window expired ({refund_window} minutes). Session created {int(elapsed_min)} minutes ago.",
            )

    # --- Try Temporal signal path ---
    if sess.workflow_id:
        signaled = await temporal_client.signal_refund(
            sess.workflow_id, req.reason or "Refund requested"
        )
        if signaled:
            result = await temporal_client.wait_for_workflow(sess.workflow_id, timeout=30.0)
            if result is not None and hasattr(result, "refund_id") and result.refund_id:
                new_balance = result.new_balance or get_balance(db_path, sess.user_id)
                sess.status = "refunded"
                sess.intent_trace = {
                    "reason_code": "other",
                    "trace_summary": req.reason or "Refund requested after completion",
                }
                sess.messages.append({
                    "type": "info",
                    "severity": "info",
                    "content_type": "plain",
                    "content": f"Refund processed via workflow: {result.refund_id}. ${sess.total_amount/100:.2f} deducted.",
                })
                log.info("REFUND %s via Temporal workflow %s", sess.id, sess.workflow_id)
                return {
                    "refund_id": result.refund_id,
                    "status": "succeeded",
                    "amount": sess.total_amount,
                    "currency": sess.currency,
                    "checkout_session_id": sess.id,
                    "amount_refunded_cents": sess.total_amount,
                    "balance_tokens": new_balance,
                    "checkout": _acp_response(sess),
                }

    # --- Inline fallback ---
    log.info("REFUND %s via inline path", sess.id)
    try:
        refund = stripe.Refund.create(
            payment_intent=sess.payment_intent_id,
            reason="requested_by_customer",
            metadata={"checkout_session_id": sess.id, "refund_reason": req.reason or ""},
        )
    except stripe.StripeError as e:
        raise HTTPException(status_code=402, detail=str(e)) from e

    _reverse_payment(db_path, sess.payment_intent_id, sess.user_id, sess.total_amount)
    new_balance = get_balance(db_path, sess.user_id)

    sess.status = "refunded"
    sess.intent_trace = {
        "reason_code": "other",
        "trace_summary": req.reason or "Refund requested after completion",
    }
    sess.messages.append({
        "type": "info",
        "severity": "info",
        "content_type": "plain",
        "content": f"Refund processed: {refund.id} ({refund.status}). ${sess.total_amount/100:.2f} deducted from balance.",
    })
    log.info(
        "REFUND %s | pi=%s | refund=%s | amount_cents=%d | new_balance=$%.2f | reason=%s",
        sess.id, sess.payment_intent_id, refund.id, sess.total_amount, new_balance / 100, req.reason,
    )

    return {
        "refund_id": refund.id,
        "status": refund.status,
        "amount": refund.amount,
        "currency": refund.currency,
        "checkout_session_id": sess.id,
        "amount_refunded_cents": sess.total_amount,
        "balance_tokens": new_balance,
        "checkout": _acp_response(sess),
    }


# ---------------------------------------------------------------------------
# Stripe webhook — authoritative fulfillment path
# ---------------------------------------------------------------------------


@app.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db_path: Annotated[str, Depends(get_db_path)],
) -> JSONResponse:
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET not configured")

    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = verify_webhook_payload(
            payload=payload, sig_header=sig, webhook_secret=settings.stripe_webhook_secret
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    event_type = event["type"]
    obj = event["data"]["object"]
    log.info("WEBHOOK %s — %s", event_type, obj.get("id", "?"))

    if event_type == "payment_intent.succeeded":
        pi_id = obj.get("id")
        meta = obj.get("metadata") or {}
        user_id = meta.get("poc_user_id", "demo_user")
        try:
            amount_cents = int(meta.get("amount_cents", "0")) or obj.get("amount", 0)
        except ValueError:
            amount_cents = obj.get("amount", 0)

        if pi_id and amount_cents > 0:
            sess = _find_session_by_pi(pi_id)
            if sess and sess.workflow_id:
                log.info("WEBHOOK %s — managed by Temporal workflow %s, skipping inline fulfillment", pi_id, sess.workflow_id)
            elif sess:
                _fulfill_payment(db_path, sess, pi_id)
                if sess.status == "payment_pending":
                    sess.status = "completed"
                    log.info("WEBHOOK promoted %s to completed", sess.id)
            else:
                add_tokens_idempotent(
                    db_path, payment_intent_id=pi_id, user_id=user_id, tokens=amount_cents
                )
                log.info("WEBHOOK fulfilled orphan pi=%s (no session) — $%.2f for %s", pi_id, amount_cents / 100, user_id)

    elif event_type == "charge.refunded":
        pi_id = obj.get("payment_intent")
        meta = obj.get("metadata") or {}
        user_id = meta.get("poc_user_id")

        if not user_id and pi_id:
            sess = _find_session_by_pi(pi_id)
            if sess:
                user_id = sess.user_id

        if not user_id:
            user_id = "demo_user"

        # Use amount_cents from metadata or session total_amount
        try:
            amount_cents = int(meta.get("amount_cents", "0"))
        except ValueError:
            amount_cents = 0

        if not amount_cents and pi_id:
            sess = _find_session_by_pi(pi_id)
            if sess:
                amount_cents = sess.total_amount

        if pi_id and amount_cents > 0:
            _reverse_payment(db_path, pi_id, user_id, amount_cents)
            sess = _find_session_by_pi(pi_id)
            if sess and sess.status != "refunded":
                sess.status = "refunded"
                log.info("WEBHOOK marked %s as refunded", sess.id)
    else:
        log.info("WEBHOOK ignored event type: %s", event_type)

    return JSONResponse({"received": True})
