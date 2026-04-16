"""Tests for merchant policy enforcement on checkout creation and cancellation."""

import httpx
import pytest

from conftest import DEMO_USER, CatalogItem


def _create_with_policy(api: httpx.Client, items: list[dict], policy: dict) -> httpx.Response:
    return api.post("/checkout_sessions", json={
        "line_items": items,
        "user_id": DEMO_USER,
        "merchant_policy": policy,
    })


class TestMaxTokensPolicy:
    def test_exceed_max_tokens(self, api: httpx.Client, catalog: list[CatalogItem], reset_balance):
        big_item = max(catalog, key=lambda c: c.tokens)
        r = _create_with_policy(api, [{"id": big_item.id}], {"max_tokens_per_session": 1})
        assert r.status_code == 422
        assert "max" in r.json()["detail"].lower()
        assert "token" in r.json()["detail"].lower()

    def test_within_max_tokens(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        r = _create_with_policy(api, [{"id": cheapest.id}], {"max_tokens_per_session": 500})
        assert r.status_code == 201


class TestMaxAmountPolicy:
    def test_exceed_max_amount(self, api: httpx.Client, catalog: list[CatalogItem], reset_balance):
        expensive = max(catalog, key=lambda c: c.amount)
        r = _create_with_policy(api, [{"id": expensive.id}], {"max_amount_cents_per_session": 1})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "max" in detail.lower() or "Policy" in detail

    def test_within_max_amount(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        r = _create_with_policy(api, [{"id": cheapest.id}], {"max_amount_cents_per_session": 99999})
        assert r.status_code == 201


class TestMinAmountPolicy:
    def test_below_min_amount(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        r = _create_with_policy(api, [{"id": cheapest.id}], {"min_amount_cents_per_session": 999999})
        assert r.status_code == 422
        assert "minimum" in r.json()["detail"].lower() or "min" in r.json()["detail"].lower()

    def test_above_min_amount(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        r = _create_with_policy(api, [{"id": cheapest.id}], {"min_amount_cents_per_session": 1})
        assert r.status_code == 201


class TestMaxItemsPolicy:
    def test_exceed_max_items(self, api: httpx.Client, catalog: list[CatalogItem], reset_balance):
        items = [{"id": c.id} for c in catalog[:3]]
        r = _create_with_policy(api, items, {"max_items_per_session": 2})
        assert r.status_code == 422
        assert "max" in r.json()["detail"].lower()
        assert "item" in r.json()["detail"].lower()

    def test_within_max_items(self, api: httpx.Client, catalog: list[CatalogItem], reset_balance):
        items = [{"id": catalog[0].id}]
        r = _create_with_policy(api, items, {"max_items_per_session": 5})
        assert r.status_code == 201


class TestCancelReasonPolicy:
    def test_cancel_requires_reason_enforced(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = api.post("/checkout_sessions", json={
            "line_items": [{"id": cheapest.id}],
            "user_id": DEMO_USER,
            "merchant_policy": {"require_cancel_reason": True},
        }).json()
        r = api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        assert r.status_code == 422
        assert "reason" in r.json()["detail"].lower()

    def test_cancel_with_reason_succeeds(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = api.post("/checkout_sessions", json={
            "line_items": [{"id": cheapest.id}],
            "user_id": DEMO_USER,
            "merchant_policy": {"require_cancel_reason": True},
        }).json()
        r = api.post(f"/checkout_sessions/{co['id']}/cancel", json={
            "intent_trace": {"reason_code": "other", "trace_summary": "Testing"}
        })
        assert r.status_code == 200

    def test_cancel_no_policy_no_reason_ok(self, api: httpx.Client, cheapest: CatalogItem, reset_balance):
        co = api.post("/checkout_sessions", json={
            "line_items": [{"id": cheapest.id}], "user_id": DEMO_USER,
        }).json()
        r = api.post(f"/checkout_sessions/{co['id']}/cancel", json={})
        assert r.status_code == 200
