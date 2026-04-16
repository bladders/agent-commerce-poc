"""Reusable assertion helpers for the agent-commerce-poc test suite."""

from __future__ import annotations

import httpx

from conftest import DEMO_USER, AgentResponse

API_BASE = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


def assert_balance_is(api: httpx.Client, expected_cents: int, tolerance: int = 0) -> int:
    """Assert the ledger balance equals expected (within tolerance). Returns actual."""
    r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
    r.raise_for_status()
    actual = r.json()["credits"]
    assert abs(actual - expected_cents) <= tolerance, (
        f"Balance mismatch: expected ${expected_cents/100:.2f} "
        f"(±${tolerance/100:.2f}), got ${actual/100:.2f}"
    )
    return actual


def assert_balance_decreased(api: httpx.Client, before: int) -> int:
    """Assert the ledger balance is strictly less than `before`. Returns actual."""
    r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
    r.raise_for_status()
    actual = r.json()["credits"]
    assert actual < before, (
        f"Balance should have decreased from ${before/100:.2f}, but is ${actual/100:.2f}"
    )
    return actual


def assert_balance_increased(api: httpx.Client, before: int) -> int:
    """Assert the ledger balance is strictly greater than `before`. Returns actual."""
    r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
    r.raise_for_status()
    actual = r.json()["credits"]
    assert actual > before, (
        f"Balance should have increased from ${before/100:.2f}, but is ${actual/100:.2f}"
    )
    return actual


def assert_agent_balance_consistent(resp: AgentResponse, api: httpx.Client) -> None:
    """The balance returned by the agent should match the ledger."""
    if resp.balance is None:
        return
    ledger = api.get("/api/v1/balance", params={"user_id": DEMO_USER}).json()["credits"]
    assert resp.balance == ledger, (
        f"Agent balance ${resp.balance/100:.2f} != ledger ${ledger/100:.2f}"
    )


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


def assert_checkout_in_response(resp: AgentResponse, expected_status: str | None = None) -> dict:
    """At least one checkout must be present. Returns the first one."""
    assert resp.checkouts, "Expected at least one checkout in the response"
    co = resp.checkouts[0]
    if expected_status:
        assert co["status"] == expected_status, (
            f"Checkout {co['id']} status: expected '{expected_status}', got '{co['status']}'"
        )
    return co


def assert_no_checkout(resp: AgentResponse) -> None:
    """No checkout should be present in the response."""
    assert not resp.checkouts, (
        f"Expected no checkouts, got {len(resp.checkouts)}: "
        f"{[c['id'] for c in resp.checkouts]}"
    )


def get_checkout_total(checkout: dict) -> int:
    """Extract total amount from a checkout's totals array."""
    total_entry = next(
        (t for t in checkout.get("totals", []) if t["type"] == "total"), None
    )
    assert total_entry is not None, f"Checkout {checkout['id']} has no 'total' entry"
    return total_entry["amount"]


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


def assert_trace_contains_tool(resp: AgentResponse, tool_name: str) -> dict:
    """Assert the trace contains a tool_result for the given tool. Returns the step."""
    for step in resp.trace:
        if step.get("type") == "tool_result" and step.get("name") == tool_name:
            return step
    tool_names = [s.get("name") for s in resp.trace if s.get("type") == "tool_result"]
    raise AssertionError(
        f"Trace missing tool '{tool_name}'. Tools found: {tool_names}"
    )


def assert_trace_tool_succeeded(resp: AgentResponse, tool_name: str) -> dict:
    """Tool was called AND its result has no 'error' key."""
    step = assert_trace_contains_tool(resp, tool_name)
    result = step.get("result", {})
    if isinstance(result, dict) and "error" in result:
        raise AssertionError(
            f"Tool '{tool_name}' returned error: {result['error']}\n"
            f"Detail: {result.get('detail', 'none')}"
        )
    return step


def assert_trace_tool_errored(resp: AgentResponse, tool_name: str) -> dict:
    """Tool was called AND its result contains an 'error' key."""
    step = assert_trace_contains_tool(resp, tool_name)
    result = step.get("result", {})
    assert isinstance(result, dict) and "error" in result, (
        f"Expected tool '{tool_name}' to have errored, but result was: "
        f"{str(result)[:200]}"
    )
    return step


def assert_trace_has_catalog_items(resp: AgentResponse, min_count: int = 1) -> list:
    """list_catalog was called and returned at least min_count items."""
    step = assert_trace_tool_succeeded(resp, "list_catalog")
    result = step.get("result", {})
    items = result.get("items", []) if isinstance(result, dict) else []
    assert len(items) >= min_count, (
        f"Expected at least {min_count} catalog items, got {len(items)}"
    )
    return items


# ---------------------------------------------------------------------------
# Agent reply
# ---------------------------------------------------------------------------


def assert_reply_mentions(resp: AgentResponse, *keywords: str) -> None:
    """Agent reply should mention all given keywords (case-insensitive)."""
    lower = resp.reply.lower()
    missing = [kw for kw in keywords if kw.lower() not in lower]
    if missing:
        raise AssertionError(
            f"Reply missing keywords: {missing}\nReply: {resp.reply[:500]}"
        )


def assert_reply_not_empty(resp: AgentResponse) -> None:
    assert resp.reply.strip(), "Agent reply is empty"


# ---------------------------------------------------------------------------
# Cost / burn
# ---------------------------------------------------------------------------


def assert_burn_charged(resp: AgentResponse) -> None:
    """A cost was charged for this turn."""
    assert resp.cost_cents is not None and resp.cost_cents > 0, (
        f"Expected cost_cents > 0, got {resp.cost_cents}"
    )


def assert_burn_deducted_balance(resp: AgentResponse, balance_before: int) -> None:
    """Balance decreased by at least cost_cents (might be more due to concurrent burns)."""
    if resp.balance is None or resp.cost_cents is None:
        return
    expected_max = balance_before - resp.cost_cents
    assert resp.balance <= expected_max, (
        f"Balance ${resp.balance/100:.2f} should be <= ${expected_max/100:.2f} "
        f"(before=${balance_before/100:.2f}, cost=${resp.cost_cents/100:.2f})"
    )
