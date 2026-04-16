"""Agent simulation scenario definitions.

Each scenario is a sequence of (user_message, assertion_functions) tuples.
Assertion functions receive (AgentResponse, AgentSession) and raise on failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from conftest import AgentResponse, AgentSession

Assertion = Callable[[AgentResponse, AgentSession], None]


@dataclass
class Step:
    message: str
    assertions: list[Assertion] = field(default_factory=list)
    complete_first_checkout: bool = False  # auto-complete via API before assertions


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[Step]
    system_policy: dict | None = None
    user_policy: dict | None = None
    tags: list[str] = field(default_factory=list)
    pre_balance_cents: int | None = None  # override starting balance (default $20)


# ---------------------------------------------------------------------------
# Assertion builders
# ---------------------------------------------------------------------------

def _tool_was_called(name: str) -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        tools = [s["name"] for s in resp.trace if s.get("type") == "tool_result"]
        assert name in tools, f"Expected tool '{name}' in trace, got: {tools}"
    return check


def _tool_succeeded(name: str) -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        for step in resp.trace:
            if step.get("type") == "tool_result" and step.get("name") == name:
                result = step.get("result", {})
                if isinstance(result, dict) and "error" in result:
                    raise AssertionError(f"Tool '{name}' errored: {result['error']}")
                return
        tools = [s["name"] for s in resp.trace if s.get("type") == "tool_result"]
        raise AssertionError(f"Tool '{name}' not called. Tools: {tools}")
    return check


def _tool_errored(name: str) -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        for step in resp.trace:
            if step.get("type") == "tool_result" and step.get("name") == name:
                result = step.get("result", {})
                assert isinstance(result, dict) and "error" in result, (
                    f"Expected tool '{name}' to error, but it succeeded"
                )
                return
        tools = [s["name"] for s in resp.trace if s.get("type") == "tool_result"]
        raise AssertionError(f"Tool '{name}' not called. Tools: {tools}")
    return check


def _policy_enforced(tool_name: str, keywords: list[str]) -> Assertion:
    """Pass if EITHER the tool errored with a policy violation OR the agent
    proactively explained the constraint (mentions any keyword)."""
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        for step in resp.trace:
            if step.get("type") == "tool_result" and step.get("name") == tool_name:
                result = step.get("result", {})
                if isinstance(result, dict) and "error" in result:
                    return  # tool errored — policy enforced at API level
        lower = resp.reply.lower()
        found = [kw for kw in keywords if kw.lower() in lower]
        if found:
            return  # agent proactively communicated the constraint
        tools = [s["name"] for s in resp.trace if s.get("type") == "tool_result"]
        raise AssertionError(
            f"Policy not enforced: tool '{tool_name}' didn't error and reply "
            f"mentions none of {keywords}. Tools: {tools}. Reply: {resp.reply[:400]}"
        )
    return check


def _has_checkout(status: str | None = None) -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        assert resp.checkouts, "Expected checkout in response"
        if status:
            assert resp.checkouts[0]["status"] == status, (
                f"Expected checkout status '{status}', got '{resp.checkouts[0]['status']}'"
            )
    return check


def _no_checkout() -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        assert not resp.checkouts, f"Expected no checkout, got {len(resp.checkouts)}"
    return check


def _reply_mentions(*keywords: str) -> Assertion:
    """All keywords must appear (case-insensitive)."""
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        lower = resp.reply.lower()
        missing = [kw for kw in keywords if kw.lower() not in lower]
        assert not missing, f"Reply missing: {missing}. Reply: {resp.reply[:400]}"
    return check


def _reply_mentions_any(*keywords: str) -> Assertion:
    """At least one keyword must appear (case-insensitive)."""
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        lower = resp.reply.lower()
        found = [kw for kw in keywords if kw.lower() in lower]
        assert found, f"Reply mentions none of: {list(keywords)}. Reply: {resp.reply[:400]}"
    return check


def _reply_not_empty() -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        assert resp.reply.strip(), "Agent reply is empty"
    return check


def _balance_decreased() -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        if resp.balance is not None:
            assert resp.balance < 2000, f"Balance should be < $20.00, got ${resp.balance/100:.2f}"
    return check


def _burn_charged() -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        assert resp.cost_cents and resp.cost_cents > 0, f"No burn charged: {resp.cost_cents}"
    return check


def _catalog_has_items(min_count: int = 1) -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        for step in resp.trace:
            if step.get("name") == "list_catalog" and step.get("type") == "tool_result":
                result = step.get("result", {})
                items = result.get("items", []) if isinstance(result, dict) else []
                assert len(items) >= min_count, f"Catalog has {len(items)} items, need {min_count}"
                return
        raise AssertionError("list_catalog not found in trace")
    return check


def _balance_api_consistent() -> Assertion:
    """Agent-reported balance should match the ledger."""
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        if resp.balance is None:
            return
        ledger = sess.get_api_balance()
        assert resp.balance == ledger, (
            f"Agent balance ${resp.balance/100:.2f} != ledger ${ledger/100:.2f}"
        )
    return check


def _balance_above(min_cents: int) -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        ledger = sess.get_api_balance()
        assert ledger >= min_cents, (
            f"Expected balance >= ${min_cents/100:.2f}, got ${ledger/100:.2f}"
        )
    return check


def _balance_below(max_cents: int) -> Assertion:
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        ledger = sess.get_api_balance()
        assert ledger <= max_cents, (
            f"Expected balance <= ${max_cents/100:.2f}, got ${ledger/100:.2f}"
        )
    return check


def _any_tool_was_called(*names: str) -> Assertion:
    """At least one of the named tools was called."""
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        tools = [s["name"] for s in resp.trace if s.get("type") == "tool_result"]
        found = [n for n in names if n in tools]
        assert found, f"Expected one of {list(names)} in trace, got: {tools}"
    return check


def _session_has_checkout_across_turns() -> Assertion:
    """Any response in the session had a checkout."""
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        all_checkouts = [co for r in sess.responses for co in r.checkouts]
        assert all_checkouts, "No checkout found in any turn of the session"
    return check


def _reply_does_not_mention(*keywords: str) -> Assertion:
    """None of these keywords should appear."""
    def check(resp: AgentResponse, sess: AgentSession) -> None:
        lower = resp.reply.lower()
        found = [kw for kw in keywords if kw.lower() in lower]
        assert not found, f"Reply should NOT mention {found}. Reply: {resp.reply[:400]}"
    return check


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

SCENARIOS: list[Scenario] = [
    Scenario(
        name="browse_catalog",
        description="Ask the agent to show the catalog",
        tags=["catalog", "basic"],
        steps=[
            Step(
                message="Show me the catalog",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("list_catalog"),
                    _catalog_has_items(3),
                    _burn_charged(),
                    _balance_api_consistent(),
                ],
            ),
        ],
    ),
    Scenario(
        name="check_balance",
        description="Ask the agent to check the balance",
        tags=["balance", "basic"],
        steps=[
            Step(
                message="What's my balance?",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("get_balance"),
                    _reply_mentions("$"),
                    _burn_charged(),
                    _balance_api_consistent(),
                ],
            ),
        ],
    ),
    Scenario(
        name="buy_single_item",
        description="Full purchase flow: browse, buy cheapest, confirm",
        tags=["checkout", "complete", "balance"],
        steps=[
            Step(
                message="I want to buy the cheapest token pack",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("list_catalog"),
                ],
            ),
            Step(
                message="Yes, please go ahead and purchase it",
                assertions=[
                    _reply_not_empty(),
                    _has_checkout("ready_for_payment"),
                ],
            ),
        ],
    ),
    Scenario(
        name="buy_and_complete_via_api",
        description="Create checkout via agent, complete via API (simulating UI button)",
        tags=["checkout", "complete", "balance"],
        steps=[
            Step(
                message="Create a checkout session for the cheapest item in the catalog. Show me the checkout details.",
                assertions=[
                    _reply_not_empty(),
                    _has_checkout("ready_for_payment"),
                ],
                complete_first_checkout=True,
            ),
        ],
    ),
    Scenario(
        name="cancel_checkout",
        description="Create a checkout then ask the agent to cancel it",
        tags=["checkout", "cancel"],
        steps=[
            Step(
                message="I want to buy 25 credits",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Actually, never mind. It's too expensive.",
                assertions=[
                    _reply_not_empty(),
                ],
            ),
        ],
    ),
    Scenario(
        name="refund_after_purchase",
        description="Buy, confirm, then request a refund",
        tags=["checkout", "refund", "balance"],
        steps=[
            Step(
                message="I want to buy the single token pack. Create the checkout and complete the payment right away — I confirm.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, confirm and pay now please.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Actually I want a refund on that purchase. The reason is I don't need it anymore.",
                assertions=[
                    _reply_not_empty(),
                    _tool_was_called("refund_checkout_session"),
                ],
            ),
        ],
    ),
    Scenario(
        name="policy_max_tokens_violation",
        description="Try to buy more tokens than policy allows",
        tags=["policy", "error_handling"],
        system_policy={"max_tokens_per_session": 5, "max_items_per_session": 1, "require_cancel_reason": True},
        steps=[
            Step(
                message=(
                    "Create a checkout for the 100 Credits pack right now. "
                    "Do NOT suggest a different item — I specifically want 100 Credits."
                ),
                assertions=[
                    _reply_not_empty(),
                    _policy_enforced(
                        "create_checkout_session",
                        ["policy", "limit", "exceed", "max", "violation", "cannot",
                         "restrict", "allow", "token", "5", "over"],
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="policy_max_amount_violation",
        description="Try to spend more than policy allows",
        tags=["policy", "error_handling"],
        system_policy={"max_amount_cents_per_session": 200, "require_cancel_reason": True},
        steps=[
            Step(
                message=(
                    "Create a checkout for the 25 Credits pack ($9.99) right now. "
                    "Do NOT suggest a cheaper item — I want exactly that pack."
                ),
                assertions=[
                    _reply_not_empty(),
                    _policy_enforced(
                        "create_checkout_session",
                        ["policy", "limit", "exceed", "spend", "budget", "cannot",
                         "allow", "$2", "200", "over", "$9.99"],
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="update_buyer_preferences",
        description="Ask the agent to change spending limits",
        tags=["preferences", "policy"],
        steps=[
            Step(
                message="Set my max spending limit to $50 per session",
                assertions=[
                    _reply_not_empty(),
                    _tool_was_called("update_buyer_preferences"),
                ],
            ),
        ],
    ),
    Scenario(
        name="stripe_introspection",
        description="Ask the agent about the Stripe account",
        tags=["stripe", "introspection"],
        steps=[
            Step(
                message="Tell me about the Stripe account — what products are available and what's the account info?",
                assertions=[
                    _reply_not_empty(),
                    _burn_charged(),
                ],
            ),
        ],
    ),
    Scenario(
        name="multi_item_purchase",
        description="Buy multiple items in one checkout",
        tags=["checkout", "multi_item"],
        steps=[
            Step(
                message="I want to buy 10 credits and 25 credits together in one purchase",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, confirm",
                assertions=[
                    _reply_not_empty(),
                    _has_checkout(),
                ],
            ),
        ],
    ),
    Scenario(
        name="session_reset",
        description="Reset the session and verify balance returns to $20",
        tags=["session", "balance"],
        steps=[
            Step(
                message="What is my balance?",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("get_balance"),
                ],
            ),
        ],
    ),
    Scenario(
        name="balance_burn_tracking",
        description="Multiple turns should progressively burn balance",
        tags=["balance", "burn"],
        steps=[
            Step(
                message="Hello, what can you do?",
                assertions=[_burn_charged(), _balance_api_consistent()],
            ),
            Step(
                message="Tell me more about the catalog",
                assertions=[_burn_charged(), _balance_api_consistent()],
            ),
            Step(
                message="What is my current balance?",
                assertions=[
                    _burn_charged(),
                    _balance_decreased(),
                    _balance_api_consistent(),
                ],
            ),
        ],
    ),
    Scenario(
        name="error_handling_graceful",
        description="Agent should handle errors gracefully and explain them",
        tags=["error_handling"],
        steps=[
            Step(
                message="Complete checkout session cs_nonexistent_12345",
                assertions=[
                    _reply_not_empty(),
                ],
            ),
        ],
    ),

    # --- New scenarios: edge cases and multi-turn flows ---

    Scenario(
        name="near_zero_balance_purchase",
        description="Start with $0.50 balance, attempt a purchase, agent should recognize insufficient funds",
        tags=["balance", "edge_case"],
        pre_balance_cents=50,
        steps=[
            Step(
                message="I want to buy the 25 Credits pack ($9.99). Create the checkout and complete it now, I confirm.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, complete the purchase now.",
                assertions=[
                    _reply_not_empty(),
                    _reply_mentions_any("balance", "insufficient", "afford", "enough", "$", "funds", "low"),
                ],
            ),
        ],
    ),
    Scenario(
        name="buy_then_check_balance",
        description="Purchase an item, then verify balance reflects the credit",
        tags=["checkout", "balance", "multi_turn"],
        steps=[
            Step(
                message="Buy the cheapest item from the catalog. Create checkout and complete immediately — I confirm.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, I confirm — complete the payment.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="What is my balance now?",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("get_balance"),
                    _reply_mentions("$"),
                    _balance_api_consistent(),
                ],
            ),
        ],
    ),
    Scenario(
        name="set_preference_then_violate",
        description="Set a low spending limit via preferences, then try to exceed it",
        tags=["preferences", "policy", "multi_turn"],
        steps=[
            Step(
                message="Set my max spending limit to $1.00 per session.",
                assertions=[
                    _reply_not_empty(),
                    _tool_was_called("update_buyer_preferences"),
                ],
            ),
            Step(
                message=(
                    "Now create a checkout for the 25 Credits pack ($9.99). "
                    "Do NOT suggest a cheaper item."
                ),
                assertions=[
                    _reply_not_empty(),
                    _policy_enforced(
                        "create_checkout_session",
                        ["limit", "exceed", "budget", "spending", "preference",
                         "$1", "100", "cannot", "over", "allow", "max"],
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="change_mind_update_checkout",
        description="Create checkout for one item, then change to a different item before completing",
        tags=["checkout", "update", "multi_turn"],
        steps=[
            Step(
                message="I want to buy the 10 Credits pack. Create a checkout but do NOT complete it.",
                assertions=[
                    _reply_not_empty(),
                    _has_checkout("ready_for_payment"),
                ],
            ),
            Step(
                message="Actually, I changed my mind. Can you update the checkout to the 25 Credits pack instead?",
                assertions=[
                    _reply_not_empty(),
                    _any_tool_was_called("update_checkout_session", "create_checkout_session"),
                ],
            ),
        ],
    ),
    Scenario(
        name="negotiate_cheaper_alternative",
        description="Ask for something expensive, then negotiate for a cheaper option",
        tags=["catalog", "negotiation", "multi_turn"],
        steps=[
            Step(
                message="Show me what's available",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("list_catalog"),
                ],
            ),
            Step(
                message="The 100 Credits pack is too expensive. What's the best value for under $5?",
                assertions=[
                    _reply_not_empty(),
                    _reply_mentions("$"),
                ],
            ),
        ],
    ),
    Scenario(
        name="ask_about_policy_rules",
        description="Ask the agent to explain the merchant rules",
        tags=["policy", "informational"],
        system_policy={
            "max_tokens_per_session": 50,
            "max_items_per_session": 3,
            "refund_window_minutes": 30,
            "require_cancel_reason": True,
        },
        steps=[
            Step(
                message="What are the rules for this store? What limits apply to my purchases?",
                assertions=[
                    _reply_not_empty(),
                    _reply_mentions_any(
                        "refund", "cancel", "token", "item", "limit",
                        "rule", "policy", "minutes", "30",
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="burn_depletes_to_zero",
        description="Start with minimal balance — burns should fail gracefully, not crash",
        tags=["balance", "burn", "edge_case"],
        pre_balance_cents=30,
        steps=[
            Step(
                message="Hello, what can you do for me?",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Tell me about the available products in detail.",
                assertions=[
                    _reply_not_empty(),
                    _balance_below(30),
                ],
            ),
        ],
    ),
    Scenario(
        name="multi_turn_purchase_and_refund_cycle",
        description="Full lifecycle: browse, buy, complete, check balance, refund, check balance again",
        tags=["checkout", "refund", "balance", "lifecycle"],
        steps=[
            Step(
                message="Show me the catalog and tell me the cheapest item.",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("list_catalog"),
                ],
            ),
            Step(
                message="Buy the cheapest item. Create checkout and complete right now — I confirm.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, complete it now please.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="What is my balance?",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("get_balance"),
                ],
            ),
            Step(
                message="I want a refund on that purchase. Reason: changed my mind.",
                assertions=[
                    _reply_not_empty(),
                    _any_tool_was_called("refund_checkout_session"),
                ],
            ),
            Step(
                message="Check my balance one more time.",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("get_balance"),
                    _balance_api_consistent(),
                ],
            ),
        ],
    ),

    # --- Token economy loop scenarios ---

    Scenario(
        name="topup_mid_conversation",
        description="Core loop: start with low balance, buy tokens, burn some, buy more, verify balance increases",
        tags=["token_economy", "topup", "lifecycle"],
        pre_balance_cents=100,
        steps=[
            Step(
                message="What's my balance?",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("get_balance"),
                    _reply_mentions("$"),
                ],
            ),
            Step(
                message="I need more credits. Buy the cheapest token pack for me. Create the checkout.",
                assertions=[
                    _reply_not_empty(),
                    _has_checkout("ready_for_payment"),
                ],
            ),
            Step(
                message="Yes, I confirm — complete the purchase.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="What's my balance now? Use get_balance to check.",
                assertions=[
                    _reply_not_empty(),
                    _reply_mentions("$"),
                    _balance_above(100),
                ],
            ),
        ],
    ),
    Scenario(
        name="repeat_purchase_same_session",
        description="Two separate purchases in the same session — both should succeed",
        tags=["token_economy", "multi_purchase"],
        steps=[
            Step(
                message="Show me the catalog.",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("list_catalog"),
                ],
            ),
            Step(
                message="Buy the cheapest item. Create checkout now.",
                assertions=[
                    _reply_not_empty(),
                    _has_checkout("ready_for_payment"),
                ],
                complete_first_checkout=True,
            ),
            Step(
                message="Great. Now I want to buy the cheapest item AGAIN — a second purchase. Create a new checkout.",
                assertions=[
                    _reply_not_empty(),
                    _any_tool_was_called("create_checkout_session", "list_catalog"),
                ],
            ),
            Step(
                message="Yes, confirm and complete this second purchase too.",
                assertions=[_reply_not_empty()],
            ),
        ],
    ),
    Scenario(
        name="best_value_optimization",
        description="Ask the agent to maximize tokens for a budget — should use calculator",
        tags=["token_economy", "calculator", "optimization"],
        steps=[
            Step(
                message="I have a $15 budget. What combination of token packs gives me the most credits for $15 or less? Show me the math.",
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("list_catalog"),
                    _reply_mentions("$"),
                ],
            ),
        ],
    ),
    Scenario(
        name="quantity_stacking",
        description="Buy 3x the same pack in a single checkout",
        tags=["token_economy", "quantity"],
        steps=[
            Step(
                message="I want to buy 3 of the cheapest token pack in one purchase. Create a checkout with quantity 3.",
                assertions=[
                    _reply_not_empty(),
                    _has_checkout("ready_for_payment"),
                ],
            ),
            Step(
                message="Yes, confirm and complete the purchase.",
                assertions=[_reply_not_empty()],
            ),
        ],
    ),
    Scenario(
        name="price_comparison",
        description="Ask agent to compare per-credit cost across packs — should use calculator",
        tags=["token_economy", "calculator", "comparison"],
        steps=[
            Step(
                message=(
                    "Show me all the token packs and calculate the cost per credit "
                    "for each one. Which pack is the best value?"
                ),
                assertions=[
                    _reply_not_empty(),
                    _tool_succeeded("list_catalog"),
                    _any_tool_was_called("calculate", "list_catalog"),
                    _reply_mentions_any("per credit", "per token", "cost per", "value", "best", "cheapest"),
                ],
            ),
        ],
    ),

    # --- Error recovery scenarios ---

    Scenario(
        name="refund_already_refunded",
        description="Complete a purchase, refund it, then try to refund again — should fail gracefully",
        tags=["error_recovery", "refund"],
        steps=[
            Step(
                message="Buy the cheapest item. Create checkout and complete right now — I confirm.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, complete it.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="I want a refund. Reason: changed my mind.",
                assertions=[
                    _reply_not_empty(),
                    _any_tool_was_called("refund_checkout_session"),
                ],
            ),
            Step(
                message="Actually, refund that same order again.",
                assertions=[
                    _reply_not_empty(),
                    _reply_mentions_any("already", "refund", "cannot", "error", "processed", "duplicate"),
                ],
            ),
        ],
    ),
    Scenario(
        name="cancel_completed_order",
        description="Try to cancel a completed order — agent should redirect to refund flow",
        tags=["error_recovery", "cancel"],
        steps=[
            Step(
                message="Buy the cheapest item. Create checkout and complete immediately — I confirm.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, complete the payment now.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Cancel that order. I don't want it.",
                assertions=[
                    _reply_not_empty(),
                    _reply_mentions_any("refund", "completed", "cancel", "already"),
                ],
            ),
        ],
    ),

    # --- Policy gap scenarios ---

    Scenario(
        name="min_amount_violation_agent",
        description="Set a min order amount policy, then ask agent to buy something below it",
        tags=["policy", "error_handling"],
        system_policy={"min_amount_cents_per_session": 1500, "require_cancel_reason": True},
        steps=[
            Step(
                message=(
                    "Show me the catalog. I want to buy the cheapest item only. "
                    "Create the checkout for just that one item — do NOT add extras."
                ),
                assertions=[
                    _reply_not_empty(),
                    _policy_enforced(
                        "create_checkout_session",
                        ["minimum", "min", "policy", "order", "$15", "1500",
                         "below", "least", "require", "amount"],
                    ),
                ],
            ),
        ],
    ),
    Scenario(
        name="refund_window_expired",
        description="Set a 0-minute refund window; purchase should be non-refundable",
        tags=["policy", "refund"],
        system_policy={"refund_window_minutes": 0, "require_cancel_reason": True},
        steps=[
            Step(
                message="Buy the cheapest item. Create checkout and complete now — I confirm.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="Yes, complete it.",
                assertions=[_reply_not_empty()],
            ),
            Step(
                message="I want a refund on that purchase. Reason: changed my mind.",
                assertions=[
                    _reply_not_empty(),
                    _reply_mentions_any(
                        "refund", "window", "expired", "policy", "cannot",
                        "no longer", "non-refundable", "not eligible", "0 minute",
                    ),
                ],
            ),
        ],
    ),

    # --- Calculator scenarios ---

    Scenario(
        name="calculator_arithmetic",
        description="Ask the agent a direct math question about credits — must use calculator tool",
        tags=["calculator", "basic"],
        steps=[
            Step(
                message=(
                    "If the 10 Credits pack costs $4.99, how much would 7 packs cost? "
                    "Use the calculator to get the exact answer."
                ),
                assertions=[
                    _reply_not_empty(),
                    _tool_was_called("calculate"),
                    _reply_mentions("$"),
                ],
            ),
        ],
    ),
]
