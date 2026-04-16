"""Tool definitions and implementations for the ACP + Stripe agent.

ACP tools call the seller's checkout_sessions endpoints (ACP spec 2026-01-30).
Stripe tools call the Stripe API directly for account introspection.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import stripe

SELLER_API = os.environ.get("SELLER_API_URL", "http://localhost:8000")

_http = httpx.Client(base_url=SELLER_API, timeout=30.0)

# ---------------------------------------------------------------------------
# ACP tool implementations (call the seller's ACP endpoints)
# ---------------------------------------------------------------------------


def acp_list_catalog() -> dict:
    r = _http.get("/api/v1/catalog")
    r.raise_for_status()
    return r.json()


def acp_create_checkout(
    items: list[dict[str, Any]],
    user_id: str = "demo_user",
    merchant_policy: dict[str, Any] | None = None,
) -> dict:
    """POST /checkout_sessions — ACP create. Accepts multiple items.

    items: list of dicts, each with "id" (required) and optional "quantity" (default 1).
    merchant_policy: optional policy dict attached to the session for enforcement.
    """
    line_items = []
    for item in items:
        li: dict[str, Any] = {"id": item["id"]}
        if item.get("quantity", 1) != 1:
            li["quantity"] = item["quantity"]
        line_items.append(li)
    body: dict[str, Any] = {
        "line_items": line_items,
        "currency": "usd",
        "user_id": user_id,
    }
    if merchant_policy:
        body["merchant_policy"] = merchant_policy
    r = _http.post("/checkout_sessions", json=body)
    r.raise_for_status()
    return r.json()


def acp_get_checkout(checkout_session_id: str) -> dict:
    """GET /checkout_sessions/{id} — ACP retrieve."""
    r = _http.get(f"/checkout_sessions/{checkout_session_id}")
    r.raise_for_status()
    return r.json()


def acp_update_checkout(checkout_session_id: str, items: list[dict[str, Any]]) -> dict:
    """POST /checkout_sessions/{id} — ACP update. Replaces all items."""
    line_items = []
    for item in items:
        li: dict[str, Any] = {"id": item["id"]}
        if item.get("quantity", 1) != 1:
            li["quantity"] = item["quantity"]
        line_items.append(li)
    body: dict[str, Any] = {"line_items": line_items}
    r = _http.post(f"/checkout_sessions/{checkout_session_id}", json=body)
    r.raise_for_status()
    return r.json()


def acp_complete_checkout(checkout_session_id: str, spt_token: str | None = None) -> dict:
    """POST /checkout_sessions/{id}/complete — ACP complete."""
    body: dict[str, Any] = {}
    if spt_token:
        body["payment_data"] = {
            "handler_id": "handler_stripe_card",
            "instrument": {
                "type": "card",
                "credential": {"type": "spt", "token": spt_token},
            },
        }
    r = _http.post(f"/checkout_sessions/{checkout_session_id}/complete", json=body)
    r.raise_for_status()
    return r.json()


def acp_cancel_checkout(
    checkout_session_id: str,
    reason_code: str = "other",
    trace_summary: str = "",
) -> dict:
    """POST /checkout_sessions/{id}/cancel — ACP cancel with intent_trace."""
    body: dict[str, Any] = {
        "intent_trace": {
            "reason_code": reason_code,
            "trace_summary": trace_summary,
        }
    }
    r = _http.post(f"/checkout_sessions/{checkout_session_id}/cancel", json=body)
    r.raise_for_status()
    return r.json()


def acp_refund(checkout_session_id: str, reason: str = "") -> dict:
    """POST /api/v1/refund — refund a completed checkout session."""
    body: dict[str, Any] = {
        "checkout_session_id": checkout_session_id,
        "reason": reason,
    }
    r = _http.post("/api/v1/refund", json=body)
    r.raise_for_status()
    return r.json()


def acp_get_balance(user_id: str = "demo_user") -> dict:
    r = _http.get("/api/v1/balance", params={"user_id": user_id})
    r.raise_for_status()
    return r.json()


def acp_consume_tokens(
    user_id: str = "demo_user",
    tokens: int = 1,
    reason: str = "llm_usage",
) -> dict:
    """POST /api/v1/consume — burn tokens from the real-time ledger."""
    r = _http.post("/api/v1/consume", json={
        "user_id": user_id,
        "tokens": tokens,
        "reason": reason,
    })
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Stripe account tool implementations (direct Stripe API)
# ---------------------------------------------------------------------------


def _ensure_stripe_key() -> None:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if key:
        stripe.api_key = key


def stripe_list_products(limit: int = 10) -> dict:
    _ensure_stripe_key()
    products = stripe.Product.list(limit=limit)
    return {"products": [{"id": p.id, "name": p.name, "description": p.description} for p in products.data]}


def stripe_list_prices(product_id: str | None = None, limit: int = 10) -> dict:
    _ensure_stripe_key()
    params: dict[str, Any] = {"limit": limit}
    if product_id:
        params["product"] = product_id
    prices = stripe.Price.list(**params)
    return {
        "prices": [
            {
                "id": p.id,
                "product": p.product,
                "unit_amount": p.unit_amount,
                "currency": p.currency,
                "type": p.type,
                "recurring": dict(p.recurring) if p.recurring else None,
            }
            for p in prices.data
        ]
    }


def stripe_list_payment_intents(user_id: str = "demo_user", limit: int = 5) -> dict:
    """List PaymentIntents scoped to a specific user via metadata filter."""
    _ensure_stripe_key()
    results = []
    for pi in stripe.PaymentIntent.list(limit=100).auto_paging_iter():
        meta = pi.metadata.to_dict() if hasattr(pi.metadata, "to_dict") else dict(pi.metadata or {})
        if meta.get("poc_user_id") == user_id:
            results.append({
                "id": pi.id,
                "amount": pi.amount,
                "currency": pi.currency,
                "status": pi.status,
                "created": pi.created,
            })
            if len(results) >= limit:
                break
    return {"payment_intents": results, "user_id": user_id}


def stripe_get_account_info() -> dict:
    """Return only public-safe merchant info (no balances, no customer data)."""
    _ensure_stripe_key()
    try:
        acct = stripe.Account.retrieve()
    except stripe.PermissionError:
        return {"error": "Insufficient key permissions to read account info"}
    return {
        "country": acct.country,
        "default_currency": acct.default_currency,
    }


# ---------------------------------------------------------------------------
# Calculator tool (avoids LLM math hallucinations)
# ---------------------------------------------------------------------------


def calculate(expression: str) -> dict:
    """Evaluate a math expression safely. Supports +, -, *, /, //, %, **,
    round(), min(), max(), abs(), and parentheses. No imports or side effects."""
    allowed = set("0123456789.+-*/%() ,")
    allowed_names = {"round": round, "min": min, "max": max, "abs": abs, "int": int, "float": float}
    cleaned = expression.strip()
    for ch in cleaned:
        if ch not in allowed and not ch.isalpha():
            return {"error": f"Disallowed character: {ch!r}"}
    try:
        result = eval(cleaned, {"__builtins__": {}}, allowed_names)  # noqa: S307
    except Exception as e:
        return {"error": f"Calculation failed: {e}"}
    return {"expression": cleaned, "result": result}


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS = {
    "list_catalog": acp_list_catalog,
    "create_checkout_session": acp_create_checkout,
    "get_checkout_session": acp_get_checkout,
    "update_checkout_session": acp_update_checkout,
    "complete_checkout_session": acp_complete_checkout,
    "cancel_checkout_session": acp_cancel_checkout,
    "refund_checkout_session": acp_refund,
    "get_balance": acp_get_balance,
    "stripe_list_products": stripe_list_products,
    "stripe_list_prices": stripe_list_prices,
    "stripe_list_payment_intents": stripe_list_payment_intents,
    "stripe_get_account_info": stripe_get_account_info,
    "calculate": calculate,
}


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool by name with the given arguments, return JSON string."""
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = fn(**arguments)
        return json.dumps(result, default=str)
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        return json.dumps({"error": str(e), "detail": detail})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# OpenAI function schemas
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_catalog",
            "description": "List available items from the seller's catalog (pulled from Stripe). Returns items with id, name, description, tokens, amount (cents), currency.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_checkout_session",
            "description": "Create an ACP checkout session with one or more items. Each item uses the 'id' from list_catalog. You can combine multiple items in one session (e.g. 1x '50 Credits' + 1x '25 Credits' = 75 tokens). Returns the full CheckoutSession.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Array of items. Each has 'id' (catalog price ID) and optional 'quantity' (default 1).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Item id from catalog (Stripe price ID)"},
                                "quantity": {"type": "integer", "description": "Quantity (default 1)"},
                            },
                            "required": ["id"],
                        },
                    },
                    "user_id": {"type": "string", "description": "User id for the token ledger (default: demo_user)"},
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_checkout_session",
            "description": "Retrieve the current state of an ACP checkout session by its id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "checkout_session_id": {"type": "string", "description": "Checkout session id (cs_xxx)"},
                },
                "required": ["checkout_session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_checkout_session",
            "description": "Update an ACP checkout session (replace all items). Only works while status is editable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "checkout_session_id": {"type": "string", "description": "Checkout session id (cs_xxx)"},
                    "items": {
                        "type": "array",
                        "description": "New items array (replaces all existing items).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Item id from catalog"},
                                "quantity": {"type": "integer", "description": "Quantity (default 1)"},
                            },
                            "required": ["id"],
                        },
                    },
                },
                "required": ["checkout_session_id", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_checkout_session",
            "description": "Complete the ACP checkout: process payment and create an order. Returns CheckoutSessionWithOrder including the order object. Optionally pass an SPT token.",
            "parameters": {
                "type": "object",
                "properties": {
                    "checkout_session_id": {"type": "string", "description": "Checkout session id (cs_xxx)"},
                    "spt_token": {"type": "string", "description": "Shared Payment Token (optional in test mode)"},
                },
                "required": ["checkout_session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_checkout_session",
            "description": "Cancel an active ACP checkout session. Must provide a reason_code and trace_summary explaining why the buyer is canceling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "checkout_session_id": {"type": "string", "description": "Checkout session id (cs_xxx)"},
                    "reason_code": {
                        "type": "string",
                        "description": "ACP reason code for cancellation",
                        "enum": ["price_sensitivity", "shipping_cost", "shipping_speed", "product_fit", "trust_security", "returns_policy", "payment_options", "comparison", "timing_deferred", "other"],
                    },
                    "trace_summary": {"type": "string", "description": "Brief explanation of why the buyer canceled, in their own words"},
                },
                "required": ["checkout_session_id", "reason_code", "trace_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refund_checkout_session",
            "description": "Refund a COMPLETED checkout session. Use this when the buyer wants their money back after a purchase was already completed. Processes a Stripe refund.",
            "parameters": {
                "type": "object",
                "properties": {
                    "checkout_session_id": {"type": "string", "description": "Checkout session id (cs_xxx) of a completed session"},
                    "reason": {"type": "string", "description": "Reason for the refund request"},
                },
                "required": ["checkout_session_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Check a user's current credit balance. Returns 'credits' (integer) and 'balance_display' (e.g. '20 credits'). Use balance_display when showing to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User id (default: demo_user)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stripe_list_products",
            "description": "List products from the Stripe account.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stripe_list_prices",
            "description": "List prices from the Stripe account, optionally filtered by product.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "Filter by product id"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stripe_list_payment_intents",
            "description": "List recent PaymentIntents for the current user (payment history, scoped by user_id).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User id to filter by (default: demo_user)"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stripe_get_account_info",
            "description": "Get public info about the merchant (country, currency).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_buyer_preferences",
            "description": "Update the buyer's session preferences (spending/token limits). Use when the buyer asks to change their budget, token limit, or spending cap. Only updates fields the buyer explicitly requests to change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_tokens_per_session": {
                        "type": "integer",
                        "description": "New max tokens per session (0 = unlimited). Only include if buyer wants to change this.",
                    },
                    "max_amount_cents_per_session": {
                        "type": "integer",
                        "description": "New max spend in cents per session (0 = unlimited). Only include if buyer wants to change this.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a math expression and return the exact result. Use this for ANY arithmetic: totals, per-unit costs, comparisons, change calculations. Supports +, -, *, /, round(), min(), max(). Example: 'round(999 / 25, 2)' → 39.96 (cost per credit in cents).",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Math expression to evaluate, e.g. '3 * 499 + 999' or 'round(2999 / 100, 2)'",
                    },
                },
                "required": ["expression"],
            },
        },
    },
]
