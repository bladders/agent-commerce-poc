# Session Log: Building Agent Commerce POC

This document reconstructs the development journey from the original build session — a single ~14-hour session where the system was designed, built, tested, and evolved through an iterative log-observe-fix loop. It serves as a reference for what happened, what should happen when reproducing this scenario, and what to expect.

## The Arc

The session moved through 9 distinct phases. Each one built on discoveries from the last.

### Phase 1: Exploration (messages 0-29)

**What happened:** Started by exploring the Stripe MCP tools available in Cursor. Discovered existing credit-pack products in the Stripe sandbox. Tested buying 10 credits via a Payment Link. Then asked about ACP — clarified this meant Stripe's Agentic Commerce Protocol, not the MCP.

**Key moment:** The distinction between MCP (tool access to Stripe APIs) and ACP (a protocol for agent-mediated commerce) was established. The goal crystallized: enable commerce from within our own agents, and expose commerce to third-party agents.

### Phase 2: Planning (messages 29-67)

**What happened:** Planned the POC with two purchase patterns — fully conversational and UI-component-based. Chose a phased approach: start as a single merchant (one Stripe account), design for marketplace later. Tech stack locked: Docker, Python (FastAPI), React, OpenAI.

**Key decisions:**
- Dual UX tracks: same agent, different confirmation UX
- ACP-aligned seller API (not full spec, but structurally compatible)
- Token/credit purchase as the product model
- Standalone Python agent with OpenAI function-calling (not LangChain)

**What should happen here:** Planning should anchor on the ACP spec endpoints (create, update, complete, cancel) and decide early on the payment confirmation model. The POC scope should resist feature creep — the goal is evaluating the two purchase patterns, not building a production system.

### Phase 3: Building v1 (messages 68-190)

**What happened:** Implemented the full stack — seller API, agent service, React UI, Docker Compose. Hit a long series of configuration issues:
- Stripe key not loading (`.env` formatting, Docker env var precedence over mounted files)
- Restricted key (`rk_test_`) lacking permissions for SPT test helper endpoints
- Cross-origin fetch failures (React on :5173 calling API on :8000)
- Unhandled errors from Stripe SPT endpoint (404 on newer API not enabled on account)

**Resolution:** Simplified the payment flow to use `pm_card_visa` test card directly (bypassing SPT), fixed Docker Compose to pass env vars via interpolation instead of `env_file`, added Vite proxy for API calls.

**What should happen here:** The `scripts/setup_stripe.py` script now handles Stripe catalog setup. The `.env.example` documents the correct key type (`sk_test_`, not `rk_test_`). Docker Compose uses `${STRIPE_SECRET_KEY}` interpolation which avoids the env var precedence bug. These fixes are baked into the current codebase — new users should not hit these issues.

### Phase 4: UX Correction (messages 248-275)

**What happened:** Initial UI had separate panels for "UI purchase" and "chat." The intent was clarified: both patterns should be chat-driven, with the only difference being how confirmation happens (typing "yes" vs clicking a button). The UI was rewritten as a single chat interface with a mode toggle.

Also hit a FastAPI parameter bug (`body` parameter name conflicting with FastAPI internals) and a `from __future__ import annotations` issue breaking Pydantic model resolution.

**What should happen here:** The current UI already implements the correct pattern — single chat interface, mode toggle at top. The FastAPI issues are fixed in the codebase.

### Phase 5: Observability + Dynamic Catalog (messages 276-350)

**What happened:** Added a trace panel showing every tool call the agent makes (function name, arguments, duration, result). Discovered the catalog was hardcoded to 3 packs while Stripe had 9 products — switched to dynamic catalog pulled from Stripe at startup.

Also identified UI rendering issues: the agent was recommending correct products but the UI was displaying individual items instead of composed offers, and arithmetic errors in token calculations.

**What should happen here:** The trace panel is essential for evaluating agent behavior. The dynamic catalog (`api/app/catalog.py`) pulls products from Stripe on startup, so any products created by `setup_stripe.py` will appear automatically. The agent now has a `calculate` tool to avoid LLM arithmetic errors.

### Phase 6: Policy Architecture (messages 379-616)

**What happened:** This was the deepest discovery phase. Added cancellation with reason tracing. Then tackled purchasing policy — started with a flat `merchant_policy` and discovered it needed to split into:
- **Merchant rules** (system-level, immutable per session): refund windows, cancellation requirements, max amounts
- **Buyer preferences** (user-level, mutable mid-session): spending limits, token budgets

Key architectural discoveries:
1. System prompt is immutable state — changing merchant rules requires session reset
2. Mutable buyer preferences need a dedicated slot (`messages[1]`), not append-only messages
3. The agent should be able to update buyer preferences via conversation (not just UI)
4. Policy enforcement happens at three layers: system prompt (guidance), tool injection (API enforcement), API validation (hard 422)
5. Error detail propagation is critical — the agent needs to see "max $1.00 per session" not just "422 error"

**What should happen here:** The policy architecture is fully implemented. See `FINDINGS.md` for the detailed design patterns. New users can set merchant policy via the UI panel and buyer preferences via either the UI or conversation.

### Phase 7: Credits + Consumption Model (messages 631-756)

**What happened:** Explored Stripe's metering and billing credits. Decided metering wasn't the right model (this is prepaid, not consumption-based). Instead: Stripe is system of record for money movement (PaymentIntents, Refunds, Credit Grants), SQLite is system of record for real-time token balance (instant debits).

Added a consumption model: the agent uses tokens when answering questions, balance shown in a UI component, reset to $20 each session.

**What should happen here:** The dual-ledger model (Stripe for audit trail, SQLite for real-time) is the right pattern for this use case. Token consumption costs are configurable in the agent service.

### Phase 8: Test-Driven Refinement (messages 861-998)

**What happened:** Built a simulation harness that loops through scenarios, writes test cases, finds errors, and fixes them. Grew from 22 to 39 test scenarios covering: browse, buy, cancel, refund, policy violations, multi-item purchases, balance tracking, error handling, preference changes, near-zero-balance edge cases, and a calculator tool.

The loop: run scenario → check assertions → read logs → fix agent/API/UI → re-run. This caught issues that manual testing missed (stale policy, double-credit on retry, balance drift).

**What should happen here:** Run `python tests/run_all.py` to execute the full suite. The 102 tests (63 API + 39 agent scenarios) validate the complete surface. Add new scenarios to `tests/scenarios.py` when exploring new patterns.

### Phase 9: Temporal Integration (messages 999-1014)

**What happened:** Evaluated making the agent side idempotent. Discussed Temporal vs LangChain — concluded Temporal is right for payment/fulfillment workflows but wrong for conversation orchestration (LangGraph is better suited there).

Implemented `CheckoutWorkflow` in Temporal: create PI → confirm → fulfill → wait for refund signal (24h) → optional refund + reverse fulfillment. The API tries Temporal first and falls back to inline if unavailable.

**What should happen here:** Temporal is now fully integrated and validated (see current session). The Docker Compose stack includes Temporal server, Postgres, UI, and worker. The workflow handles the durable checkout lifecycle while the conversation stays in the existing Python orchestrator.

## What a New User Should Expect

### First run (~5 minutes)

1. Clone, copy `.env.example` to `.env`, add keys
2. Run `python scripts/setup_stripe.py` to create Stripe products
3. `docker compose up --build -d` — wait ~60s for Temporal schema setup
4. Open http://localhost:5173

### What works immediately

- Browse catalog via chat ("what can I buy?")
- Purchase tokens ("buy 10 credits")
- UI-mode confirmation (click Confirm on checkout card) or conversational ("yes, complete it")
- Cancel with reason tracing
- Refund a completed purchase
- Set buyer preferences via chat ("set my max spend to $10")
- Merchant policy enforcement (set in UI, enforced by API)
- Token consumption on each agent turn
- Balance tracking and reset
- Trace panel showing every tool call

### What to watch for

- **Stripe key type:** Must be `sk_test_` (standard). Restricted keys (`rk_test_`) may lack permissions.
- **Temporal startup:** The worker retries for up to 60s if the Temporal server isn't ready. Check `docker logs agent-commerce-poc-temporal-worker-1`.
- **First checkout latency:** The Temporal workflow path adds 2-4s vs inline. This is expected — the tradeoff is durability.
- **Agent arithmetic:** The agent has a `calculate` tool. If it tries to do math in its head instead of calling the tool, the system prompt may need reinforcement.

## Reproducing the Evaluation

To reproduce the evaluation that drove the original session:

1. **Start fresh** — clean Stripe sandbox, new `.env`, `docker compose up`
2. **Browse and buy** — test both conversational and UI-component confirmation
3. **Set policy** — add merchant rules (max $10/session, require cancel reason), add buyer preferences (max 50 tokens)
4. **Test violations** — try to exceed limits, verify the agent explains why
5. **Cancel and refund** — test the full lifecycle including reason tracing
6. **Check Temporal UI** (http://localhost:8233) — verify workflows are durable
7. **Run the test suite** — `python tests/run_all.py` should pass 102/102
8. **Read the logs** — `docker logs agent-commerce-poc-agent-1` shows every LLM decision

The key insight from the original session: **the value is in the log-observe-fix loop, not the final artifact.** Each phase revealed constraints that couldn't be predicted upfront. The structured logging and test harness make that loop reproducible.
