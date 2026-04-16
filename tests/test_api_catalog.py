"""Tests for GET /api/v1/catalog."""

import httpx


def test_catalog_returns_items(api: httpx.Client, catalog):
    assert len(catalog) >= 1


def test_catalog_items_have_required_fields(catalog):
    for item in catalog:
        assert item.id, f"Item missing id: {item}"
        assert item.name, f"Item missing name: {item}"
        assert item.amount > 0, f"Item has non-positive amount: {item}"
        assert item.tokens >= 1, f"Item has invalid tokens: {item}"
        assert item.currency == "usd", f"Unexpected currency: {item.currency}"


def test_catalog_sorted_by_price(catalog):
    amounts = [item.amount for item in catalog]
    assert amounts == sorted(amounts), (
        f"Catalog not sorted by price: {amounts}"
    )


def test_catalog_ids_are_stripe_price_ids(catalog):
    for item in catalog:
        assert item.id.startswith("price_"), (
            f"Expected Stripe price ID, got: {item.id}"
        )


def test_catalog_endpoint_returns_json(api: httpx.Client):
    r = api.get("/api/v1/catalog")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert isinstance(data["items"], list)
