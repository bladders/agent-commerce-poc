"""Tests for POST /api/v1/refund — refund flows and policy window enforcement."""

import httpx
import pytest

from conftest import DEMO_USER, INITIAL_BALANCE_CENTS, CatalogItem


def _buy_and_complete(api: httpx.Client, item: CatalogItem, policy: dict | None = None) -> dict:
    """Create + complete a checkout, returning the completed session."""
    body: dict = {"line_items": [{"id": item.id}], "user_id": DEMO_USER}
    if policy:
        body["merchant_policy"] = policy
    co = api.post("/checkout_sessions", json=body)
    co.raise_for_status()
    cs_id = co.json()["id"]
    result = api.post(f"/checkout_sessions/{cs_id}/complete", json={})
    result.raise_for_status()
    return result.json()


class TestRefund:
    def test_refund_deducts_amount_cents(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        completed = _buy_and_complete(api, cheapest)
        cs_id = completed["id"]
        bal_after_buy = completed["_poc"]["balance_tokens"]
        assert bal_after_buy == INITIAL_BALANCE_CENTS + cheapest.amount

        r = api.post("/api/v1/refund", json={
            "checkout_session_id": cs_id, "reason": "Changed my mind"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "succeeded"
        assert data["balance_tokens"] == INITIAL_BALANCE_CENTS
        assert data["checkout"]["status"] == "refunded"

    def test_refund_non_completed_fails(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = api.post("/checkout_sessions", json={
            "line_items": [{"id": cheapest.id}], "user_id": DEMO_USER,
        }).json()
        r = api.post("/api/v1/refund", json={
            "checkout_session_id": co["id"], "reason": "test"
        })
        assert r.status_code == 422
        assert "Only completed" in r.json()["detail"]

    def test_refund_nonexistent_session(self, api: httpx.Client):
        r = api.post("/api/v1/refund", json={
            "checkout_session_id": "cs_nonexistent", "reason": "test"
        })
        assert r.status_code == 404

    def test_refund_window_zero_blocks(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        """refund_window_minutes=0 means no refunds allowed."""
        policy = {"refund_window_minutes": 0}
        completed = _buy_and_complete(api, cheapest, policy=policy)
        r = api.post("/api/v1/refund", json={
            "checkout_session_id": completed["id"], "reason": "want refund"
        })
        assert r.status_code == 422
        assert "not allowed" in r.json()["detail"].lower()

    def test_refund_within_window_succeeds(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        """refund_window_minutes=60 should allow immediate refund."""
        policy = {"refund_window_minutes": 60}
        completed = _buy_and_complete(api, cheapest, policy=policy)
        r = api.post("/api/v1/refund", json={
            "checkout_session_id": completed["id"], "reason": "within window"
        })
        assert r.status_code == 200


class TestRefundBalanceConsistency:
    def test_buy_refund_balance_returns_to_original(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        bal_start = api.get("/api/v1/balance", params={"user_id": DEMO_USER}).json()["balance_cents"]
        completed = _buy_and_complete(api, cheapest)
        cs_id = completed["id"]
        bal_mid = api.get("/api/v1/balance", params={"user_id": DEMO_USER}).json()["balance_cents"]
        assert bal_mid == bal_start + cheapest.amount

        api.post("/api/v1/refund", json={"checkout_session_id": cs_id, "reason": "undo"})
        bal_end = api.get("/api/v1/balance", params={"user_id": DEMO_USER}).json()["balance_cents"]
        assert bal_end == bal_start, (
            f"Balance after buy+refund: ${bal_end/100:.2f} != original ${bal_start/100:.2f}"
        )
