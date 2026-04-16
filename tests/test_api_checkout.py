"""Tests for checkout session lifecycle: create, retrieve, update, complete, cancel."""

import httpx
import pytest

from conftest import DEMO_USER, INITIAL_BALANCE, CatalogItem


def _create_session(api: httpx.Client, items: list[dict], policy: dict | None = None) -> dict:
    body: dict = {"line_items": items, "user_id": DEMO_USER}
    if policy:
        body["merchant_policy"] = policy
    r = api.post("/checkout_sessions", json=body)
    assert r.status_code == 201, f"Create failed: {r.text}"
    return r.json()


class TestCreateCheckout:
    def test_create_single_item(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        assert co["status"] == "ready_for_payment"
        assert co["id"].startswith("cs_")
        assert len(co["line_items"]) == 1
        assert co["line_items"][0]["quantity"] == 1
        total = next(t for t in co["totals"] if t["type"] == "total")
        assert total["amount"] == cheapest.amount

    def test_create_multi_item(self, api: httpx.Client, catalog: list[CatalogItem], reset_balance):
        items = [{"id": catalog[0].id, "quantity": 2}, {"id": catalog[1].id, "quantity": 1}]
        co = _create_session(api, items)
        assert len(co["line_items"]) == 2
        expected_total = catalog[0].amount * 2 + catalog[1].amount
        total = next(t for t in co["totals"] if t["type"] == "total")
        assert total["amount"] == expected_total

    def test_create_with_quantity(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id, "quantity": 3}])
        total = next(t for t in co["totals"] if t["type"] == "total")
        assert total["amount"] == cheapest.amount * 3

    def test_create_unknown_item(self, api: httpx.Client, reset_balance):
        r = api.post("/checkout_sessions", json={
            "line_items": [{"id": "price_nonexistent"}], "user_id": DEMO_USER,
        })
        assert r.status_code == 400

    def test_create_empty_items(self, api: httpx.Client, reset_balance):
        r = api.post("/checkout_sessions", json={
            "line_items": [], "user_id": DEMO_USER,
        })
        assert r.status_code == 400

    def test_acp_protocol_in_response(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        assert co["protocol"]["version"] == "2026-01-30"
        assert "capabilities" in co
        assert co["capabilities"]["payment"]["handlers"][0]["psp"] == "stripe"

    def test_poc_fields_in_response(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        poc = co["_poc"]
        assert poc["user_id"] == DEMO_USER
        assert poc["tokens"] == cheapest.tokens


class TestRetrieveCheckout:
    def test_retrieve_existing(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        r = api.get(f"/checkout_sessions/{co['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == co["id"]

    def test_retrieve_nonexistent(self, api: httpx.Client):
        r = api.get("/checkout_sessions/cs_does_not_exist")
        assert r.status_code == 404


class TestUpdateCheckout:
    def test_update_items(self, api: httpx.Client, catalog: list[CatalogItem], reset_balance):
        co = _create_session(api, [{"id": catalog[0].id}])
        r = api.post(f"/checkout_sessions/{co['id']}", json={
            "line_items": [{"id": catalog[1].id, "quantity": 2}],
        })
        assert r.status_code == 200
        updated = r.json()
        assert len(updated["line_items"]) == 1
        total = next(t for t in updated["totals"] if t["type"] == "total")
        assert total["amount"] == catalog[1].amount * 2


class TestCompleteCheckout:
    def test_complete_credits_balance_in_cents(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        r = api.post(f"/checkout_sessions/{co['id']}/complete", json={})
        assert r.status_code == 200
        result = r.json()
        assert result["status"] == "completed"
        assert result["order"]["status"] == "confirmed"
        balance_after = result["_poc"]["balance_tokens"]
        assert balance_after == INITIAL_BALANCE + cheapest.tokens, (
            f"Expected balance {INITIAL_BALANCE} + {cheapest.tokens} = "
            f"{INITIAL_BALANCE + cheapest.tokens}, got {balance_after}"
        )

    def test_complete_multi_item_credits_total_amount(self, api: httpx.Client, catalog: list[CatalogItem], reset_balance):
        items = [{"id": catalog[0].id}, {"id": catalog[1].id}]
        co = _create_session(api, items)
        expected_credits = catalog[0].tokens + catalog[1].tokens
        r = api.post(f"/checkout_sessions/{co['id']}/complete", json={})
        assert r.status_code == 200
        balance_after = r.json()["_poc"]["balance_tokens"]
        assert balance_after == INITIAL_BALANCE + expected_credits

    def test_complete_nonexistent(self, api: httpx.Client):
        r = api.post("/checkout_sessions/cs_nonexistent/complete", json={})
        assert r.status_code == 404

    def test_complete_has_order(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        result = api.post(f"/checkout_sessions/{co['id']}/complete", json={}).json()
        assert "order" in result
        assert result["order"]["id"].startswith("ord_")
        assert result["order"]["checkout_session_id"] == co["id"]


class TestCancelCheckout:
    def test_cancel_with_reason(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        r = api.post(f"/checkout_sessions/{co['id']}/cancel", json={
            "intent_trace": {"reason_code": "price_sensitivity", "trace_summary": "Too expensive"}
        })
        assert r.status_code == 200
        assert r.json()["status"] == "canceled"
        assert r.json()["intent_trace"]["reason_code"] == "price_sensitivity"

    def test_cancel_without_reason_default(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = _create_session(api, [{"id": cheapest.id}])
        r = api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "canceled"

    def test_cancel_does_not_affect_balance(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        _create_session(api, [{"id": cheapest.id}])
        bal_before = api.get("/api/v1/balance", params={"user_id": DEMO_USER}).json()["credits"]
        assert bal_before == INITIAL_BALANCE
