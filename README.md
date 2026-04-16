# Agent Commerce POC

A multi-service environment for exploring agent-mediated commerce patterns using Stripe's [Agentic Commerce Protocol](https://docs.stripe.com/agentic-commerce/protocol/specification) (ACP), LLM function-calling, and Temporal durable workflows.

## What it does

An LLM agent helps users browse a catalog, purchase token packs via Stripe, and manage refunds -- all through natural conversation. The same checkout flow is also available through a traditional UI. Both paths hit the same seller API and Stripe account.

**Services:**

| Service | Port | Description |
|---------|------|-------------|
| **Seller API** | 8000 | FastAPI -- ACP checkout endpoints, Stripe payments, SQLite token ledger |
| **Agent** | 8080 | Python + OpenAI -- LLM orchestrator with function-calling tools |
| **Web UI** | 5173 | React split-screen: UI purchase (left), agent chat (right) |
| **Temporal Server** | 7233 | Durable workflow engine for checkout lifecycle |
| **Temporal UI** | 8233 | Web dashboard for inspecting workflows |
| **Temporal Worker** | -- | Executes checkout workflows (payment, fulfillment, refund) |
| **Temporal DB** | 5432 | Postgres backing store for Temporal |

## Quick start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (4GB+ RAM allocated)
- A [Stripe test-mode secret key](https://dashboard.stripe.com/test/apikeys) (`sk_test_...`)
- An [OpenAI API key](https://platform.openai.com/api-keys)

### Setup

```bash
git clone https://github.com/bladders/agent-commerce-poc.git
cd agent-commerce-poc

cp .env.example .env
# Edit .env -- fill in STRIPE_SECRET_KEY and OPENAI_API_KEY

docker compose up --build -d
```

First boot takes ~60 seconds (Temporal schema setup, npm install, pip installs).

### Verify

```bash
# All 7 containers should show "Up"
docker ps

# API health
curl http://localhost:8000/health/config

# Temporal worker connected
docker logs agent-commerce-poc-temporal-worker-1
```

### Use it

- **Web UI**: http://localhost:5173 -- left panel is direct purchase, right panel is agent chat
- **API docs**: http://localhost:8000/docs -- interactive OpenAPI explorer
- **Temporal UI**: http://localhost:8233 -- inspect running/completed workflows

### Terminal mode (agent only, no UI)

```bash
docker compose run --rm -e AGENT_MODE=terminal agent
```

## Architecture

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│  React   │────▶│  Seller  │◀────│  Agent   │
│  UI      │     │  API     │     │  (LLM)   │
│  :5173   │     │  :8000   │     │  :8080   │
└──────────┘     └────┬─────┘     └────┬─────┘
                      │                │
              ┌───────┴────────┐       │
              │   Temporal     │       │
              │  ┌──────────┐  │       │
              │  │ Checkout  │  │       │
              │  │ Workflow  │  │       │
              │  └──────────┘  │       │
              │   :7233        │       │
              └───────┬────────┘       │
                      │                │
                      ▼                ▼
                   Stripe API     Stripe API
                  (payments,     (products, prices,
                   refunds)       customers)
```

### Checkout lifecycle (Temporal workflow)

When a checkout is completed, the API starts a Temporal `CheckoutWorkflow` that durably executes:

1. **Create PaymentIntent** -- Stripe charge with idempotency key
2. **Confirm Payment** -- wait for `succeeded` status
3. **Fulfill** -- credit the token ledger + create a Stripe Credit Grant
4. **Wait for refund signal** -- workflow stays open for 24h to receive refund requests
5. **Refund (if signaled)** -- Stripe refund + reverse ledger credit

If Temporal is unavailable, the API falls back to an inline payment path automatically.

## Seller API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/catalog` | Token pack catalog (from Stripe products) |
| POST | `/checkout_sessions` | Create checkout session |
| GET | `/checkout_sessions/:id` | Retrieve session |
| POST | `/checkout_sessions/:id` | Update session |
| POST | `/checkout_sessions/:id/complete` | Complete (triggers payment) |
| POST | `/checkout_sessions/:id/cancel` | Cancel session |
| GET | `/api/v1/balance` | Token balance |
| POST | `/api/v1/consume` | Burn tokens |
| POST | `/api/v1/refund` | Refund a completed session |
| POST | `/api/v1/reset-balance` | Reset balance (dev/test) |
| POST | `/webhooks/stripe` | Stripe webhook receiver |
| GET | `/health` | Health check |

## Agent tools

**ACP tools** (call seller API):
`list_catalog`, `create_checkout`, `get_checkout`, `update_checkout`, `complete_checkout`, `cancel_checkout`, `get_balance`

**Stripe tools** (direct Stripe API):
`stripe_list_products`, `stripe_list_prices`, `stripe_list_payment_intents`, `stripe_list_customers`, `stripe_get_account_info`, `stripe_get_balance`

## Running the tests

Tests run against the live Docker services. Make sure all containers are up first.

```bash
# Full suite (API + agent scenarios) -- ~5 min
python tests/run_all.py

# API tests only -- ~40s
python tests/run_all.py --api-only

# Individual test file
python -m pytest tests/test_api_checkout.py -v
```

**Test coverage:**

| Suite | Tests | What it covers |
|-------|-------|----------------|
| Catalog | 5 | Product listing, field validation, sorting |
| Balance | 10 | Reset, consume, insufficient funds, edge cases |
| Checkout | 17 | Create, retrieve, update, complete, cancel |
| Refund | 6 | Full refund lifecycle, policy windows, balance consistency |
| Policy | 11 | Max tokens/amount, min amount, max items, cancel reasons |
| Edge Cases | 14 | Double-complete, cancel-after-complete, idempotency |
| Agent Scenarios | 39 | 32 parametrized multi-turn conversations + 7 targeted assertions |

## Configuration

All configuration is via environment variables in `.env`:

| Variable | Required | Description |
|----------|----------|-------------|
| `STRIPE_SECRET_KEY` | Yes | Stripe test secret key (`sk_test_...`) |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `OPENAI_MODEL` | No | Model override (default: `gpt-4o`) |
| `STRIPE_WEBHOOK_SECRET` | No | From `stripe listen --forward-to localhost:8000/webhooks/stripe` |
| `POC_API_KEY` | No | Optional API key gate for seller endpoints |

## Project layout

```
agent-commerce-poc/
  api/                  # Seller API: FastAPI + Stripe + SQLite ledger
    app/
      main.py           #   Routes + checkout logic
      temporal_client.py#   Temporal client wrapper
      catalog.py        #   Stripe product sync
      ledger.py         #   SQLite token ledger
      credits.py        #   Stripe Credit Grants
  agent/                # LLM agent: OpenAI function-calling
    app/
      orchestrator.py   #   Tool dispatch + conversation loop
      tools.py          #   ACP + Stripe tool definitions
  temporal/             # Temporal workflows + activities
    workflows/
      checkout.py       #   CheckoutWorkflow (payment → fulfill → refund)
    activities/
      payment.py        #   create_payment_intent, confirm_payment
      fulfillment.py    #   fulfill_payment (ledger + credit grant)
      refund.py         #   process_refund, reverse_fulfillment
    worker.py           #   Worker entrypoint
    shared.py           #   Shared dataclasses
  web/                  # React split-screen UI
  tests/                # Integration test suite (102 tests)
  docker-compose.yml    # All 7 services
  FINDINGS.md           # Design patterns discovered during development
```

## Troubleshooting

**Temporal worker keeps restarting:** Check `docker logs agent-commerce-poc-temporal-1` -- the Temporal server takes ~15s to initialize its schema. The worker retries automatically.

**"Stripe key not configured":** Make sure `.env` has `STRIPE_SECRET_KEY` set to a standard key (starts with `sk_test_`). Restricted keys (`rk_test_`) may lack required permissions.

**Out of memory:** The full stack uses ~600MB. Ensure Docker Desktop has at least 4GB RAM allocated (Settings > Resources).

**Tests failing on `/complete`:** The Temporal workflow path adds a few seconds of latency. If tests timeout, check that the Temporal worker is connected: `docker logs agent-commerce-poc-temporal-worker-1`.
