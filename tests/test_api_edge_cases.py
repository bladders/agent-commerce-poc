"""Tests for edge cases: double-complete, cancel-completed, refund-canceled, unknown IDs."""

import httpx
import pytest

from conftest import DEMO_USER, INITIAL_BALANCE, CatalogItem


def _create(api: httpx.Client, item_id: str, policy: dict | None = None) -> dict:
    body: dict = {"line_items": [{"id": item_id}], "user_id": DEMO_USER}
    if policy:
        body["merchant_policy"] = policy
    r = api.post("/checkout_sessions", json=body)
    r.raise_for_status()
    return r.json()


def _complete(api: httpx.Client, cs_id: str) -> dict:
    r = api.post(f"/checkout_sessions/{cs_id}/complete", json={})
    r.raise_for_status()
    return r.json()


class TestDoubleComplete:
    def test_double_complete_returns_409(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create(api, cheapest.id)
        _complete(api, co["id"])
        r = api.post(f"/checkout_sessions/{co['id']}/complete", json={})
        assert r.status_code == 409
        assert "already completed" in r.json()["detail"].lower()

    def test_double_complete_does_not_double_credit(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create(api, cheapest.id)
        _complete(api, co["id"])
        bal_after_first = api.get("/api/v1/balance", params={"user_id": DEMO_USER}).json()["credits"]
        api.post(f"/checkout_sessions/{co['id']}/complete", json={})
        bal_after_second = api.get("/api/v1/balance", params={"user_id": DEMO_USER}).json()["credits"]
        assert bal_after_first == bal_after_second


class TestCancelCompleted:
    def test_cancel_completed_returns_405(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create(api, cheapest.id)
        _complete(api, co["id"])
        r = api.post(f"/checkout_sessions/{co['id']}/cancel", json={
            "intent_trace": {"reason_code": "other", "trace_summary": "test"}
        })
        assert r.status_code == 405

    def test_cancel_canceled_returns_405(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create(api, cheapest.id)
        api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        r = api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        assert r.status_code == 405


class TestCompleteCanceled:
    def test_complete_canceled_returns_409(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create(api, cheapest.id)
        api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        r = api.post(f"/checkout_sessions/{co['id']}/complete", json={})
        assert r.status_code == 409


class TestRefundEdgeCases:
    def test_refund_already_refunded(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create(api, cheapest.id)
        _complete(api, co["id"])
        api.post("/api/v1/refund", json={"checkout_session_id": co["id"], "reason": "first"})
        r = api.post("/api/v1/refund", json={"checkout_session_id": co["id"], "reason": "second"})
        assert r.status_code == 422

    def test_refund_canceled_session(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create(api, cheapest.id)
        api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        r = api.post("/api/v1/refund", json={"checkout_session_id": co["id"], "reason": "test"})
        assert r.status_code == 422


class TestUnknownSessionIDs:
    def test_get_unknown(self, api: httpx.Client):
        assert api.get("/checkout_sessions/cs_fake").status_code == 404

    def test_complete_unknown(self, api: httpx.Client):
        assert api.post("/checkout_sessions/cs_fake/complete", json={}).status_code == 404

    def test_cancel_unknown(self, api: httpx.Client):
        assert api.post("/checkout_sessions/cs_fake/cancel", json={}).status_code == 404

    def test_update_unknown(self, api: httpx.Client):
        assert api.post("/checkout_sessions/cs_fake", json={"line_items": []}).status_code == 404

    def test_refund_unknown(self, api: httpx.Client):
        assert api.post("/api/v1/refund", json={"checkout_session_id": "cs_fake"}).status_code == 404


class TestUpdateEdgeCases:
    def test_update_completed_session(self, api: httpx.Client, cheapest: CatalogItem, catalog: list[CatalogItem], reset_balance):
        co = _create(api, cheapest.id)
        _complete(api, co["id"])
        r = api.post(f"/checkout_sessions/{co['id']}", json={
            "line_items": [{"id": catalog[1].id}]
        })
        assert r.status_code == 422

    def test_update_canceled_session(self, api: httpx.Client, cheapest: CatalogItem, catalog: list[CatalogItem], reset_balance):
        co = _create(api, cheapest.id)
        api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        r = api.post(f"/checkout_sessions/{co['id']}", json={
            "line_items": [{"id": catalog[1].id}]
        })
        assert r.status_code == 422
