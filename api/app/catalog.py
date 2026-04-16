"""Dynamic catalog: pulls products + one-time prices from Stripe on startup."""

import logging
import re

import stripe
from pydantic import BaseModel

log = logging.getLogger(__name__)


class TokenPack(BaseModel):
    pack_id: str       # Stripe price ID (price_xxx)
    product_id: str    # Stripe product ID (prod_xxx)
    label: str         # Product name from Stripe
    description: str
    tokens: int        # Derived from product name or metadata
    amount_cents: int  # price.unit_amount
    currency: str


_catalog: list[TokenPack] = []


def _extract_token_count(name: str, metadata: dict) -> int | None:
    """Try to get a token/credit count from product metadata or name."""
    if "tokens" in metadata:
        try:
            return int(metadata["tokens"])
        except ValueError:
            pass
    if "credits" in metadata:
        try:
            return int(metadata["credits"])
        except ValueError:
            pass
    m = re.search(r"(\d+)\s*(?:credit|token)", name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def load_catalog(stripe_secret_key: str) -> None:
    """Fetch products and one-time prices from Stripe, build the catalog."""
    global _catalog
    stripe.api_key = stripe_secret_key

    products_map: dict[str, stripe.Product] = {}
    for product in stripe.Product.list(active=True, limit=100).auto_paging_iter():
        products_map[product.id] = product

    packs: list[TokenPack] = []
    for price in stripe.Price.list(active=True, limit=100, type="one_time").auto_paging_iter():
        product = products_map.get(price.product)
        if not product or not price.unit_amount:
            continue

        metadata = dict(product.metadata) if product.metadata else {}
        token_count = _extract_token_count(product.name, metadata)
        if token_count is None:
            token_count = 1

        packs.append(TokenPack(
            pack_id=price.id,
            product_id=product.id,
            label=product.name,
            description=product.description or "",
            tokens=token_count,
            amount_cents=price.unit_amount,
            currency=price.currency,
        ))

    packs.sort(key=lambda p: p.amount_cents)
    _catalog = packs
    log.info("Loaded %d catalog items from Stripe", len(_catalog))
    for p in _catalog:
        log.info("  %s: %s (%d tokens, %d %s)", p.pack_id, p.label, p.tokens, p.amount_cents, p.currency)


def get_pack(pack_id: str) -> TokenPack | None:
    for p in _catalog:
        if p.pack_id == pack_id:
            return p
    return None


def list_catalog() -> list[TokenPack]:
    return list(_catalog)
