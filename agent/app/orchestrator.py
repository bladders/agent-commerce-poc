"""OpenAI function-calling orchestrator for the ACP + Stripe agent.

Returns (assistant_message, checkouts, trace) where:
- checkouts: ACP checkout session states from tool calls (for UI cards)
- trace: ordered list of tool calls with arguments and results (for the trace panel)
"""

import json
import os
import time
from typing import Any

from openai import OpenAI

from app.tools import TOOL_SCHEMAS, call_tool

CHECKOUT_TOOLS = {
    "create_checkout_session",
    "get_checkout_session",
    "update_checkout_session",
    "complete_checkout_session",
    "cancel_checkout_session",
    "refund_checkout_session",
}

SYSTEM_PROMPT = """\
You are a commerce assistant for a digital token store, powered by the \
Agentic Commerce Protocol (ACP, version 2026-01-30).

## Your capabilities

**ACP checkout tools** — interact with the seller's ACP checkout endpoints:
- list_catalog: browse available items (pulled from Stripe). Each item has an \
`id` (Stripe price ID), `name`, `description`, `tokens`, `amount` (cents), `currency`.
- create_checkout_session: start a new ACP checkout session with one or MORE items. \
Pass an `items` array where each element has `id` (from catalog) and optional `quantity`. \
You MUST combine multiple items into a SINGLE checkout session — never create separate \
sessions for items in the same purchase. Returns the full CheckoutSession per the ACP spec.
- get_checkout_session: retrieve the current state of a checkout session.
- update_checkout_session: replace all items in a session while it is editable.
- complete_checkout_session: process payment and create an order. Returns a \
CheckoutSessionWithOrder including the `order` object.
- cancel_checkout_session: cancel an active checkout session. MUST include a \
reason_code (from ACP spec: price_sensitivity, shipping_cost, product_fit, \
comparison, timing_deferred, other, etc.) and a trace_summary (the buyer's \
own words). Always ask the user WHY they want to cancel before calling this.
- refund_checkout_session: refund a COMPLETED checkout. Use when the buyer \
wants money back after payment succeeded. Pass the checkout_session_id and reason.
- get_balance: check a user's current token balance (POC-specific).

**Stripe account tools** — query the Stripe account directly:
- stripe_list_products, stripe_list_prices: see what's in the Stripe catalog
- stripe_list_payment_intents: recent payment history
- stripe_list_customers: customer records
- stripe_get_account_info: account details
- stripe_get_balance: available/pending balance

## How to handle purchases

1. When the user wants to buy tokens, first show them the catalog (list_catalog).
2. Figure out the best combination of catalog items to meet the user's need. \
For example, if they want 75 tokens, combine 1x "50 Credits" + 1x "25 Credits" \
in a single checkout. Always optimize for the user (best value, fewest items).
3. Create ONE checkout session with ALL the items: call create_checkout_session \
with the full items array. NEVER create multiple separate checkout sessions for \
one purchase.
4. Summarize the checkout: list each item, quantities, per-item subtotals, and \
the combined total. Show the total tokens they'll receive.
5. Ask for explicit confirmation. DO NOT call complete_checkout_session until the \
user clearly confirms (e.g. "yes", "confirm", "go ahead").
6. On confirmation, call complete_checkout_session to process payment.
7. Report the result: order ID, order status, tokens credited, new balance.

CRITICAL: Never complete a checkout without the user's explicit confirmation. \
Always present what they'll be charged and what they'll receive first.

## How to handle cancellations

1. If the user wants to cancel an ACTIVE (not yet completed) checkout, ask WHY \
before canceling. Summarize their reason in your own words.
2. Call cancel_checkout_session with the appropriate reason_code and trace_summary.
3. Confirm the cancellation to the user.

## How to handle refunds

1. If the user wants to cancel/refund a COMPLETED checkout, use \
refund_checkout_session (not cancel).
2. Ask why they want a refund. Pass their reason.
3. Report the refund status.

## Buyer preferences

The buyer can set personal spending limits (max tokens, max spend per session). \
These are separate from merchant rules. If the buyer asks to change their budget \
or limits, call update_buyer_preferences with the new values. Always confirm the \
change back to the buyer. When recommending purchases, stay within both merchant \
rules AND buyer preferences.

## Merchant policy awareness

Checkout session responses include a `merchant_policy` object when the merchant \
has set constraints. Read it carefully and communicate rules to the buyer BEFORE \
they act. Distinguish between:
- **Merchant rules** (refund window, min order, max items, cancel reason) — the \
buyer cannot change these.
- **Buyer preferences** (max tokens, max spend) — the buyer CAN change these \
at any time by asking.

If a tool call fails with a "Policy violation" error, explain which rule was \
violated and what the buyer can do instead. Never blame the system — frame it \
as the merchant's policy.

## Calculator

You have a `calculate` tool. Use it for ALL arithmetic — totals, per-credit \
costs, quantity × price, change calculations, comparisons. NEVER do mental \
math or estimate; always call calculate for exact results. Examples:
- Total for 3x $4.99: calculate("3 * 499") → 1497 cents
- Cost per credit: calculate("round(999 / 25, 2)") → 39.96 cents/credit
- Remaining balance: calculate("2000 - 999 - 50") → 951 cents

Format currency amounts properly (e.g., $4.99 not 499 cents). Be concise.
"""


def get_model() -> str:
    return os.environ.get("OPENAI_MODEL", "gpt-4o")


def create_client() -> OpenAI:
    return OpenAI()


def chat_completion(
    client: OpenAI,
    messages: list[dict[str, Any]],
    merchant_policy: dict | None = None,
    on_update_user_policy: Any | None = None,
) -> tuple[dict[str, Any], list[dict], list[dict]]:
    """Run completion with tool-call loop.

    Returns (assistant_message_dict, checkouts, trace).
    on_update_user_policy: optional callback(updates_dict) -> new_user_policy
    """
    model = get_model()
    checkouts: list[dict] = []
    trace: list[dict] = []

    while True:
        t0 = time.time()
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )
        llm_ms = int((time.time() - t0) * 1000)
        msg = response.choices[0].message
        usage = response.usage

        if not msg.tool_calls:
            trace.append({
                "type": "llm",
                "model": model,
                "duration_ms": llm_ms,
                "usage": {
                    "prompt_tokens": usage.prompt_tokens if usage else None,
                    "completion_tokens": usage.completion_tokens if usage else None,
                },
                "action": "final_response",
            })
            return {"role": "assistant", "content": msg.content or ""}, checkouts, trace

        trace.append({
            "type": "llm",
            "model": model,
            "duration_ms": llm_ms,
            "usage": {
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": usage.completion_tokens if usage else None,
            },
            "action": "tool_calls",
            "tool_calls": [
                {"name": tc.function.name, "arguments": tc.function.arguments}
                for tc in msg.tool_calls
            ],
        })

        messages.append(msg.model_dump(exclude_none=True))

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            if tc.function.name == "create_checkout_session" and merchant_policy:
                args["merchant_policy"] = merchant_policy

            t1 = time.time()

            if tc.function.name == "update_buyer_preferences" and on_update_user_policy:
                new_policy = on_update_user_policy(args)
                result_str = json.dumps({
                    "status": "updated",
                    "buyer_preferences": new_policy,
                })
                if merchant_policy:
                    merchant_policy.update(args)
            else:
                result_str = call_tool(tc.function.name, args)

            tool_ms = int((time.time() - t1) * 1000)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })

            try:
                result_parsed = json.loads(result_str)
            except (json.JSONDecodeError, TypeError):
                result_parsed = result_str

            trace.append({
                "type": "tool_result",
                "name": tc.function.name,
                "arguments": args,
                "duration_ms": tool_ms,
                "result": result_parsed,
            })

            if tc.function.name in CHECKOUT_TOOLS:
                if isinstance(result_parsed, dict) and "id" in result_parsed and "status" in result_parsed:
                    checkouts.append(result_parsed)
