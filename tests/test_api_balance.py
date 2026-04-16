"""Tests for balance endpoints: GET /api/v1/balance, POST /api/v1/reset-balance, POST /api/v1/consume."""

import httpx
import pytest

from conftest import DEMO_USER, INITIAL_BALANCE_CENTS


class TestResetBalance:
    def test_reset_to_default(self, api: httpx.Client, reset_balance):
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.status_code == 200
        data = r.json()
        assert data["balance_cents"] == INITIAL_BALANCE_CENTS
        assert data["balance_display"] == "$20.00"

    def test_reset_to_custom_amount(self, api: httpx.Client):
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": 5000})
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.json()["balance_cents"] == 5000
        # cleanup
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": INITIAL_BALANCE_CENTS})

    def test_reset_to_zero(self, api: httpx.Client):
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": 0})
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.json()["balance_cents"] == 0
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": INITIAL_BALANCE_CENTS})


class TestBalanceEndpoint:
    def test_balance_has_cents_and_display(self, api: httpx.Client, reset_balance):
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        data = r.json()
        assert "balance_cents" in data
        assert "balance_display" in data
        assert "user_id" in data

    def test_balance_display_format(self, api: httpx.Client, reset_balance):
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        display = r.json()["balance_display"]
        assert display.startswith("$")
        float(display.replace("$", ""))  # should parse


class TestConsume:
    def test_consume_reduces_balance(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": 225, "reason": "test_burn"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["tokens_consumed"] == 225
        assert data["balance"] == INITIAL_BALANCE_CENTS - 225
        assert data["reason"] == "test_burn"

    def test_consume_multiple_times(self, api: httpx.Client, reset_balance):
        api.post("/api/v1/consume", json={"user_id": DEMO_USER, "tokens": 500, "reason": "a"})
        api.post("/api/v1/consume", json={"user_id": DEMO_USER, "tokens": 500, "reason": "b"})
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.json()["balance_cents"] == INITIAL_BALANCE_CENTS - 1000

    def test_consume_insufficient_balance_caps(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": INITIAL_BALANCE_CENTS + 1, "reason": "too_much"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["tokens_consumed"] == INITIAL_BALANCE_CENTS
        assert data["capped"] is True
        assert data["balance"] == 0

    def test_consume_exact_balance(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": INITIAL_BALANCE_CENTS, "reason": "drain"
        })
        assert r.status_code == 200
        assert r.json()["balance"] == 0

    def test_consume_zero_rejected(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": 0, "reason": "zero"
        })
        assert r.status_code == 422  # ge=1 validation
