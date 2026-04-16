# Findings: Agent Policy Management in ACP Commerce

## Context

This document captures the design patterns and architectural insights discovered while building an LLM-powered agent that mediates commerce through Stripe's Agentic Commerce Protocol (ACP). The work was driven by an iterative log → observe → fix loop, where each interaction revealed a deeper constraint about how policy, state, and context should be managed in agent systems.

## The Core Problem

An agent needs to operate within constraints — some set by the merchant (business rules), some set by the buyer (personal preferences). These constraints govern what the agent can do, what it should recommend, and how it explains limitations. Getting this wrong leads to an agent that ignores rules, gives stale answers, or can't adapt when the user changes their mind.

## Key Findings

### 1. Policy Has Two Owners With Different Lifecycles

**Discovery:** We started with a single flat `merchant_policy` object. Testing revealed that some fields are merchant-owned rules (refund windows, cancellation requirements) while others are buyer-owned preferences (spending limits, token budgets). These have fundamentally different lifecycles.

**Pattern:**

| | Merchant Rules (system) | Buyer Preferences (user) |
|---|---|---|
| Owner | Merchant, set before session | Buyer, set during session |
| Mutability | Immutable per session | Mutable mid-session |
| Injection point | System prompt (`messages[0]`) | Dedicated slot (`messages[1]`) |
| Change impact | Requires session reset | Updates in place, no reset |

**Why it matters:** Treating all policy as one thing either makes it too rigid (buyer can't adjust preferences) or too loose (buyer could change merchant rules). The split maps to real-world commerce: a store sets its return policy, a customer sets their budget.

### 2. System Prompt Is Immutable State — And That's a Feature

**Discovery:** We initially tried to put all policy into the system prompt. When we needed to update buyer preferences mid-session, we couldn't — the system prompt was already written.

**Insight:** This is actually correct behavior. The system prompt represents the session's immutable context — the "contract" the agent operates under. Merchant rules belong here precisely because they shouldn't change. The constraint forced us to find the right architecture rather than hack around it.

**Implication:** Changing system-level policy (merchant rules) should reset the session. This isn't a limitation — it's the correct behavior. A session operates under a fixed set of merchant rules. New rules mean a new session.

### 3. Mutable Context Needs a Dedicated Slot, Not Append-Only Messages

**Discovery:** Our first approach to mutable buyer preferences was to append new system messages when values changed:

```
messages[0]: system prompt
messages[2]: [BUYER PREFS] max_tokens: 500    ← stale
messages[5]: [BUYER PREFS] max_tokens: 1000   ← current
messages[8]: [BUYER PREFS] max_tokens: 750    ← current
```

The LLM had to reconcile multiple contradictory messages. It sometimes used stale values.

**Pattern:** Reserve a fixed slot (`messages[1]`) for mutable context. Overwrite it in place before every LLM call. The LLM always sees exactly one version — the current truth.

```
messages[0]: system prompt + merchant rules  (frozen)
messages[1]: buyer preferences               (overwritten each turn)
messages[2:]: conversation history            (append-only)
```

**Why it matters:** Append-only is the wrong model for state that represents "what IS" rather than "what happened." Conversation history is append-only because it records events. Current preferences are state — they should be a single authoritative representation.

### 4. Current State and State History Serve Different Purposes

**Discovery:** After implementing the single-slot overwrite, we realized the agent lost the ability to acknowledge transitions ("you just raised your limit from 500 to 1000"). Pure overwrite forgets what came before.

**Pattern:** The slot contains both:
- **Current state** at the top — the truth for enforcement and recommendations
- **Change log** below — an indexed history of prior states with sources

```
[BUYER PREFERENCES — CURRENT]
- Max tokens per session: 1000 tokens
- Max spend per session: $20.00

Change log:
  [0] session start: Max tokens: 500, Max spend: Unlimited
  [1] buyer (chat): Max tokens: 500, Max spend: $10.00
```

**Why it matters:** Current state answers "what are the rules?" History answers "how did we get here?" and enables natural dialogue ("you doubled your token limit"). The LLM is instructed to use current values for enforcement and the log for conversational context.

### 5. The Buyer Should Be Able to Set Preferences Through Conversation

**Discovery:** When a user said "can I up my limit to 2000 and 1000 tokens?" — the agent had no mechanism to act. It could only report the current limits. The buyer's preferences lived in a UI panel they couldn't reach from the chat.

**Pattern:** Give the agent an `update_buyer_preferences` tool. When the LLM calls it:
1. The callback updates the authoritative state (`_session_user_policy`)
2. The policy slot (`messages[1]`) is overwritten with new current + updated history
3. The merged policy (system + user) is refreshed for API enforcement
4. The tool result confirms the change to the LLM
5. The UI detects the tool call in the response trace and syncs its local state

**Why it matters:** If the buyer can set preferences in the UI, they should be able to set them through conversation too. The agent is the buyer's interface — restricting preference changes to a separate panel breaks the conversational model.

### 6. Policy Enforcement Happens at Multiple Layers

Policy is checked at three points, each serving a different purpose:

| Layer | What it checks | Why |
|---|---|---|
| **System prompt** | Agent knows the rules | Guides recommendations, prevents suggesting invalid actions |
| **Agent tool injection** | Merged policy attached to `create_checkout_session` | Ensures the API receives the full constraint set |
| **API enforcement** | Hard validation with 422 responses | Server-side truth — the agent can't bypass this |

**Why it matters:** The agent is not trusted to enforce policy alone (it's an LLM, it can hallucinate or ignore instructions). The API is the final authority. But the agent knowing the policy means it can explain violations proactively rather than just relaying error messages.

### 7. Error Detail Propagation Is Critical for Agent Reasoning

**Discovery:** When the API returned a 422 for a policy violation, the agent only saw "Client error '422 Unprocessable Entity'" — the response body with the specific violation ("max $1.00 per session") was discarded by the HTTP client error handler.

**Fix:** The `call_tool` error handler was updated to extract and forward the response body:

```python
except httpx.HTTPStatusError as e:
    detail = e.response.json()  # "Policy violation: max $1.00 per session"
    return json.dumps({"error": str(e), "detail": detail})
```

**Why it matters:** An agent that sees "422 error" can only say "something went wrong." An agent that sees "Policy violation: max $1.00 per session" can say "this merchant limits sessions to $1.00 — would you like to reduce your order?" The detail transforms a failure into a useful interaction.

## The Log → Observe → Fix Loop

Every finding above came from the same process:

1. **Build** the feature based on current understanding
2. **Test** through the UI, triggering the exact scenario
3. **Read the logs** — structured agent logs show every LLM decision, tool call, argument, and result
4. **Identify the gap** between expected and actual behavior
5. **Fix** the architecture, not just the symptom
6. **Repeat**

The structured logging was essential. Without seeing exactly what the LLM received in its context, what tools it chose, and what results came back, the policy issues would have been invisible — the agent would have just given "reasonable-sounding but wrong" answers.

## Architecture Summary

```
┌─────────────────────────────────────────────────┐
│ UI (React)                                      │
│                                                 │
│  ┌──────────────┐    ┌───────────────────────┐  │
│  │ Merchant Rules│    │  Buyer Preferences    │  │
│  │ (system)      │    │  (user)               │  │
│  │ change→reset  │    │  change→no reset      │  │
│  └──────┬───────┘    └───────────┬───────────┘  │
│         │                        │               │
│         │  POST /chat { system_policy, user_policy, message }
│         └────────────┬───────────┘               │
└──────────────────────┼───────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────┐
│ Agent Service                                     │
│                                                   │
│  messages[0]: system prompt + merchant rules      │
│  messages[1]: buyer prefs (current + change log)  │
│  messages[2:]: conversation history               │
│                                                   │
│  Tools: create_checkout, update_buyer_preferences │
│         → merged policy injected into API calls   │
└──────────────────────┬───────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────┐
│ Seller API (ACP)                                  │
│                                                   │
│  Receives merged merchant_policy on session       │
│  Hard enforcement: 422 on violation               │
│  Policy attached to CheckoutSession (immutable)   │
└───────────────────────────────────────────────────┘
```

## Open Questions

- **Policy versioning across sessions:** Should the UI preserve a history of merchant rule changes across session resets? Currently, session reset clears everything.
- **Policy negotiation:** Could a buyer request exceptions to merchant rules? (e.g., "can I get a longer refund window?") This would require a merchant approval flow.
- **Policy from Stripe:** In production, merchant rules should come from the Stripe dashboard or API, not a UI panel. The current approach is an evaluation tool.
