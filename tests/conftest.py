"""Shared fixtures for the agent-commerce-poc test suite.

All tests run against the live Docker services:
  - API (seller): http://localhost:8000
  - Agent:        http://localhost:8080
"""

import time
from dataclasses import dataclass, field

import httpx
import pytest

API_BASE = "http://localhost:8000"
AGENT_BASE = "http://localhost:8080"
DEMO_USER = "demo_user"
INITIAL_BALANCE = 20  # 20 credits


@dataclass
class CatalogItem:
    id: str
    product_id: str
    name: str
    description: str
    tokens: int
    amount: int
    currency: str


@dataclass
class AgentResponse:
    """Parsed response from POST /chat."""
    reply: str
    checkouts: list[dict]
    trace: list[dict]
    session_id: str
    cost_credits: int | None
    llm_tokens_used: int | None
    balance: int | None
    raw: dict


@dataclass
class AgentSession:
    """Stateful agent session for multi-turn tests."""
    session_id: str
    agent: httpx.Client
    api: httpx.Client
    responses: list[AgentResponse] = field(default_factory=list)
    system_policy: dict | None = None
    user_policy: dict | None = None

    def send(self, message: str, timeout: float = 30.0) -> AgentResponse:
        body: dict = {"message": message, "session_id": self.session_id}
        if self.system_policy:
            body["system_policy"] = self.system_policy
        if self.user_policy:
            body["user_policy"] = self.user_policy
        r = self.agent.post("/chat", json=body, timeout=timeout)
        r.raise_for_status()
        j = r.json()
        resp = AgentResponse(
            reply=j.get("reply", ""),
            checkouts=j.get("checkouts", []),
            trace=j.get("trace", []),
            session_id=j.get("session_id", self.session_id),
            cost_credits=j.get("cost_credits"),
            llm_tokens_used=j.get("llm_tokens_used"),
            balance=j.get("balance"),
            raw=j,
        )
        self.responses.append(resp)
        return resp

    def reset(self) -> dict:
        r = self.agent.post(
            "/chat/reset",
            json={"message": "", "session_id": self.session_id},
        )
        r.raise_for_status()
        self.responses.clear()
        return r.json()

    def get_api_balance(self) -> int:
        r = self.api.get("/api/v1/balance", params={"user_id": DEMO_USER})
        r.raise_for_status()
        return r.json()["credits"]

    def complete_checkout_via_api(self, checkout_id: str) -> dict:
        r = self.api.post(
            f"/checkout_sessions/{checkout_id}/complete",
            json={},
        )
        r.raise_for_status()
        return r.json()

    @property
    def last(self) -> AgentResponse:
        return self.responses[-1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def api() -> httpx.Client:
    """HTTP client pointed at the seller API."""
    client = httpx.Client(base_url=API_BASE, timeout=35.0)
    r = client.get("/health")
    assert r.status_code == 200, f"API not healthy: {r.text}"
    yield client
    client.close()


@pytest.fixture(scope="session")
def agent() -> httpx.Client:
    """HTTP client pointed at the agent service."""
    client = httpx.Client(base_url=AGENT_BASE, timeout=60.0)
    r = client.get("/health")
    assert r.status_code == 200, f"Agent not healthy: {r.text}"
    yield client
    client.close()


@pytest.fixture(scope="session")
def catalog(api) -> list[CatalogItem]:
    """Fetch the live catalog once per session."""
    r = api.get("/api/v1/catalog")
    r.raise_for_status()
    items = r.json()["items"]
    assert len(items) > 0, "Catalog is empty — check Stripe products"
    return [CatalogItem(**item) for item in items]


@pytest.fixture
def cheapest(catalog) -> CatalogItem:
    return min(catalog, key=lambda c: c.amount)


@pytest.fixture
def reset_balance(api):
    """Reset the demo user balance to 20 credits before each test."""
    api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": INITIAL_BALANCE})
    yield
    api.post("/api/v1/reset-balance", json={"user_id": DEMO_USER, "amount": INITIAL_BALANCE})


@pytest.fixture
def balance(api, reset_balance) -> int:
    """Return starting balance (after reset)."""
    r = api.get("/api/v1/balance", params={"user_id": DEMO_USER})
    r.raise_for_status()
    return r.json()["credits"]


@pytest.fixture
def agent_session(api, agent, reset_balance) -> AgentSession:
    """A fresh agent session with balance reset."""
    sid = f"test_{int(time.time() * 1000)}"
    sess = AgentSession(session_id=sid, agent=agent, api=api)
    sess.reset()
    return sess
