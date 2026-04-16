"""Agent simulation tests — multi-turn conversations with the LLM agent.

Each test corresponds to a scenario defined in scenarios.py. The agent
is called via HTTP, and structured assertions validate tool usage,
balance consistency, checkout states, and reply quality.

These tests are slower (LLM round-trips) and non-deterministic.
Scenarios are retried up to MAX_RETRIES times before being marked as failed.
"""

from __future__ import annotations

import time
import pytest
import httpx

from conftest import AgentSession, INITIAL_BALANCE, DEMO_USER
from scenarios import SCENARIOS, Scenario

MAX_RETRIES = 2  # total attempts = 1 + MAX_RETRIES


def _run_scenario(scenario: Scenario, api: httpx.Client, agent: httpx.Client) -> list[str]:
    """Run a scenario and return a list of failure messages (empty = pass)."""
    sid = f"sim_{scenario.name}_{int(time.time() * 1000)}"
    sess = AgentSession(session_id=sid, agent=agent, api=api)

    if scenario.system_policy:
        sess.system_policy = scenario.system_policy
    if scenario.user_policy:
        sess.user_policy = scenario.user_policy

    sess.reset()

    if scenario.pre_balance_credits is not None:
        api.post(
            "/api/v1/reset-balance",
            json={"user_id": DEMO_USER, "amount": scenario.pre_balance_credits},
        )

    failures: list[str] = []

    for step_idx, step in enumerate(scenario.steps):
        try:
            resp = sess.send(step.message, timeout=60.0)
        except Exception as e:
            failures.append(f"Step {step_idx} send failed: {e}")
            break

        if step.complete_first_checkout and resp.checkouts:
            co = resp.checkouts[0]
            if co["status"] == "ready_for_payment":
                try:
                    sess.complete_checkout_via_api(co["id"])
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 409:
                        pass
                    else:
                        failures.append(f"Step {step_idx} checkout complete failed: {e}")

        for assertion in step.assertions:
            try:
                assertion(resp, sess)
            except AssertionError as e:
                failures.append(f"Step {step_idx} ({step.message[:50]}): {e}")

    return failures


def _run_with_retries(
    scenario: Scenario, api: httpx.Client, agent: httpx.Client, max_retries: int = MAX_RETRIES,
) -> list[str]:
    """Run a scenario with retries. Returns failures from the last attempt."""
    for attempt in range(1 + max_retries):
        failures = _run_scenario(scenario, api, agent)
        if not failures:
            return []
        if attempt < max_retries:
            api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": INITIAL_BALANCE})
    return failures


# ---------------------------------------------------------------------------
# Parametrized test: one test per scenario
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "scenario",
    SCENARIOS,
    ids=[s.name for s in SCENARIOS],
)
def test_scenario(scenario: Scenario, api: httpx.Client, agent: httpx.Client, reset_balance):
    failures = _run_with_retries(scenario, api, agent)
    if failures:
        msg = f"\n{'='*60}\nScenario: {scenario.name}\n{scenario.description}\n{'='*60}\n"
        for f in failures:
            msg += f"  FAIL: {f}\n"
        pytest.fail(msg)


# ---------------------------------------------------------------------------
# Direct targeted tests for important flows
# ---------------------------------------------------------------------------

class TestCatalogBrowse:
    def test_catalog_returns_items_in_trace(self, agent_session: AgentSession):
        resp = agent_session.send("Show me the catalog")
        tools = [s["name"] for s in resp.trace if s.get("type") == "tool_result"]
        assert "list_catalog" in tools, f"list_catalog not called. Tools: {tools}"
        for step in resp.trace:
            if step.get("name") == "list_catalog":
                items = step["result"].get("items", [])
                assert len(items) >= 3, f"Expected 3+ catalog items, got {len(items)}"


class TestBalanceCheck:
    def test_balance_returns_dollars(self, agent_session: AgentSession):
        resp = agent_session.send("What's my balance?")
        assert "$" in resp.reply, f"Reply should mention $: {resp.reply[:200]}"

    def test_balance_consistent_with_api(self, agent_session: AgentSession):
        resp = agent_session.send("Check my balance")
        if resp.balance is not None:
            api_bal = agent_session.get_api_balance()
            assert resp.balance == api_bal


class TestPurchaseFlow:
    def test_agent_asks_for_confirmation(self, agent_session: AgentSession):
        agent_session.send("I want to buy 10 credits")
        resp = agent_session.send("Yes, go ahead")
        assert resp.checkouts or "confirm" in resp.reply.lower() or "checkout" in resp.reply.lower(), (
            f"Expected checkout or confirmation request: {resp.reply[:300]}"
        )

    def test_checkout_has_acp_fields(self, agent_session: AgentSession):
        agent_session.send("Buy the cheapest item")
        resp = agent_session.send("Confirm")
        if resp.checkouts:
            co = resp.checkouts[0]
            assert "protocol" in co
            assert "line_items" in co
            assert "totals" in co


class TestBurnConsistency:
    def test_each_turn_burns(self, agent_session: AgentSession):
        for msg in ["Hello", "What can you do?", "Thanks"]:
            resp = agent_session.send(msg)
            assert resp.cost_cents and resp.cost_cents > 0, f"No burn for '{msg}'"

    def test_balance_decreases_each_turn(self, agent_session: AgentSession):
        balances = []
        for msg in ["Hi", "Show catalog", "Check balance"]:
            resp = agent_session.send(msg)
            if resp.balance is not None:
                balances.append(resp.balance)
        if len(balances) >= 2:
            assert balances[-1] < balances[0], (
                f"Balance should decrease over turns: {balances}"
            )
