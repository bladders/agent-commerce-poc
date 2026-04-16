"""Tests for balance endpoints: GET /api/v1/balance, POST /api/v1/reset-balance, POST /api/v1/consume."""

import httpx
import pytest

from conftest import DEMO_USER, INITIAL_BALANCE


class TestResetBalance:
    def test_reset_to_default(self, api: httpx.Client, reset_balance):
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.status_code == 200
        data = r.json()
        assert data["credits"] == INITIAL_BALANCE
        assert data["balance_display"] == f"{INITIAL_BALANCE} credits"

    def test_reset_to_custom_amount(self, api: httpx.Client):
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": 50})
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.json()["credits"] == 50
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": INITIAL_BALANCE})

    def test_reset_to_zero(self, api: httpx.Client):
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": 0})
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.json()["credits"] == 0
        api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": INITIAL_BALANCE})


class TestBalanceEndpoint:
    def test_balance_has_credits_and_display(self, api: httpx.Client, reset_balance):
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        data = r.json()
        assert "credits" in data
        assert "balance_display" in data
        assert "user_id" in data

    def test_balance_display_format(self, api: httpx.Client, reset_balance):
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        display = r.json()["balance_display"]
        assert "credits" in display


class TestConsume:
    def test_consume_reduces_balance(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": 3, "reason": "test_burn"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["tokens_consumed"] == 3
        assert data["balance"] == INITIAL_BALANCE - 3
        assert data["reason"] == "test_burn"

    def test_consume_multiple_times(self, api: httpx.Client, reset_balance):
        api.post("/api/v1/consume", json={"user_id": DEMO_USER, "tokens": 5, "reason": "a"})
        api.post("/api/v1/consume", json={"user_id": DEMO_USER, "tokens": 5, "reason": "b"})
        r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        assert r.json()["credits"] == INITIAL_BALANCE - 10

    def test_consume_insufficient_balance_caps(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": INITIAL_BALANCE + 1, "reason": "too_much"
        })
        assert r.status_code == 200
        data = r.json()
        assert data["tokens_consumed"] == INITIAL_BALANCE
        assert data["capped"] is True
        assert data["balance"] == 0

    def test_consume_exact_balance(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": INITIAL_BALANCE, "reason": "drain"
        })
        assert r.status_code == 200
        assert r.json()["balance"] == 0

    def test_consume_zero_rejected(self, api: httpx.Client, reset_balance):
        r = api.post("/api/v1/consume", json={
            "user_id": DEMO_USER, "tokens": 0, "reason": "zero"
        })
        assert r.status_code == 422  # ge=1 validation
