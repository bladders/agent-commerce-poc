#!/usr/bin/env python3
"""Set up Stripe test-mode products and prices for the Agent Commerce POC.

Run once after creating a fresh Stripe test account:

    pip install stripe
    STRIPE_SECRET_KEY=sk_test_... python scripts/setup_stripe.py

Or if your key is already in .env:

    python scripts/setup_stripe.py

Creates 5 token-pack products with one-time prices. Safe to re-run —
skips products that already exist (matched by name).
"""

import os
import sys

try:
    import stripe
except ImportError:
    print("stripe package not installed. Run: pip install stripe")
    sys.exit(1)

PACKS = [
    {"name": "10 Credits",   "description": "Starter credit pack - perfect for trying out",  "tokens": 10,  "amount_cents": 499},
    {"name": "25 Credits",   "description": "Popular credit pack - Best value per credit!",  "tokens": 25,  "amount_cents": 999},
    {"name": "50 Credits",   "description": "Power user credit pack - for frequent planners","tokens": 50,  "amount_cents": 1799},
    {"name": "100 Credits",  "description": "Ultimate credit pack - maximum value",          "tokens": 100, "amount_cents": 2999},
    {"name": "single token", "description": "One token for quick testing",                   "tokens": 1,   "amount_cents": 100},
]


def load_key() -> str:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("STRIPE_SECRET_KEY=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip()
    return ""


def main():
    key = load_key()
    if not key or not key.startswith(("sk_test_", "rk_test_")):
        print("Error: STRIPE_SECRET_KEY not found or not a test key.")
        print("Set it via environment variable or in .env")
        sys.exit(1)

    stripe.api_key = key
    print(f"Using Stripe key: {key[:12]}...{key[-4:]}")
    print()

    existing_by_name: dict[str, stripe.Product] = {}
    for product in stripe.Product.list(active=True, limit=100).auto_paging_iter():
        existing_by_name[product.name] = product

    created = 0
    skipped = 0

    for pack in PACKS:
        if pack["name"] in existing_by_name:
            prod = existing_by_name[pack["name"]]
            prices = list(stripe.Price.list(product=prod.id, active=True, limit=5).auto_paging_iter())
            price_id = prices[0].id if prices else "no price"
            print(f"  SKIP    {pack['name']:15s}  (exists: {prod.id}, price: {price_id})")
            skipped += 1
            continue

        product = stripe.Product.create(
            name=pack["name"],
            description=pack["description"],
            metadata={
                "source": "agent_commerce_poc",
                "tokens": str(pack["tokens"]),
            },
        )

        price = stripe.Price.create(
            product=product.id,
            unit_amount=pack["amount_cents"],
            currency="usd",
        )

        print(f"  CREATE  {pack['name']:15s}  product={product.id}  price={price.id}  ${pack['amount_cents']/100:.2f}")
        created += 1

    print()
    print(f"Done. Created {created}, skipped {skipped} (already existed).")

    if created > 0:
        print()
        print("Restart the API to pick up new catalog:")
        print("  docker compose restart api")


if __name__ == "__main__":
    main()
