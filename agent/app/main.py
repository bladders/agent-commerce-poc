"""Agent service: terminal mode or HTTP server with /chat endpoint.

Includes rich structured logging for every interaction:
- Full user message and session context
- Each LLM decision (model, tokens, latency, tool calls chosen)
- Each tool call with full arguments and results
- Final reply text
- Checkout session states returned to UI
"""

import json
import logging
import os
import sys
from typing import Any

from pydantic import BaseModel

from app.orchestrator import SYSTEM_PROMPT, create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent")


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    system_policy: dict | None = None
    user_policy: dict | None = None


def _log_trace(trace: list[dict], checkouts: list[dict], session_id: str) -> None:
    """Emit detailed structured logs for every step in the agent trace."""
    for i, step in enumerate(trace):
        if step["type"] == "llm":
            usage = step.get("usage", {})
            pt = usage.get("prompt_tokens", "?")
            ct = usage.get("completion_tokens", "?")
            if step.get("action") == "tool_calls":
                calls = step.get("tool_calls", [])
                call_summary = []
                for tc in calls:
                    try:
                        args_parsed = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else tc["arguments"]
                    except (json.JSONDecodeError, TypeError):
                        args_parsed = tc["arguments"]
                    call_summary.append(f'{tc["name"]}({json.dumps(args_parsed, default=str)})')
                log.info(
                    "STEP %d | LLM decision → %d tool call(s) | model=%s latency=%dms tokens=%s→%s\n    calls: %s",
                    i, len(calls), step.get("model"), step["duration_ms"], pt, ct,
                    "\n           ".join(call_summary),
                )
            else:
                log.info(
                    "STEP %d | LLM → final_response | model=%s latency=%dms tokens=%s→%s",
                    i, step.get("model"), step["duration_ms"], pt, ct,
                )
        elif step["type"] == "tool_result":
            result_json = json.dumps(step.get("result", ""), default=str)
            truncated = len(result_json) > 800
            if truncated:
                result_json = result_json[:800] + "...[truncated]"
            log.info(
                "STEP %d | TOOL %s | args=%s | latency=%dms\n    result: %s",
                i, step["name"],
                json.dumps(step.get("arguments", {}), default=str),
                step["duration_ms"],
                result_json,
            )

    if checkouts:
        for co in checkouts:
            items_desc = []
            for li in co.get("line_items", []):
                qty = li.get("quantity", 1)
                name = li.get("name") or li.get("item", {}).get("name", "?")
                items_desc.append(f"{qty}x {name}")
            total_entry = next((t for t in co.get("totals", []) if t["type"] == "total"), None)
            total_str = f"${total_entry['amount']/100:.2f}" if total_entry else "?"
            order = co.get("order")
            order_str = f" → order={order['id']}({order['status']})" if order else ""
            log.info(
                "CHECKOUT %s | status=%s | items=[%s] | total=%s%s",
                co["id"], co["status"], ", ".join(items_desc), total_str, order_str,
            )


def run_terminal() -> None:
    """Interactive terminal chat loop (non-streaming)."""
    from app.orchestrator import chat_completion

    client = create_client()
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    print("Agent Commerce Assistant (type 'quit' to exit)")
    print("=" * 50)
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        messages.append({"role": "user", "content": user_input})
        reply, checkouts, trace = chat_completion(client, messages)
        messages.append(reply)

        for step in trace:
            if step["type"] == "tool_result":
                print(f"  [{step['name']}({step['arguments']}) → {step['duration_ms']}ms]")
        print(f"Assistant: {reply['content']}")
        if checkouts:
            for co in checkouts:
                print(f"  [Checkout {co['id']}: {co['status']}]")
        print()


def run_server() -> None:
    """HTTP server with /chat endpoint that returns text + any checkout states."""
    import uvicorn
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from app.orchestrator import chat_completion

    app = FastAPI(title="Agent Commerce Chat", version="0.3.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _sessions: dict[str, list[dict[str, Any]]] = {}
    _session_system_policy: dict[str, dict | None] = {}
    _session_user_policy: dict[str, dict | None] = {}
    _session_user_policy_history: dict[str, list[tuple[str, dict]]] = {}  # session → [(source, snapshot), ...]

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "server"}

    # -- System policy → baked into system prompt (immutable per session) --

    _SYSTEM_POLICY_LABELS = {
        "refund_window_minutes": ("Refund window", "minutes", {0: "No refunds allowed", -1: "Unlimited"}),
        "min_amount_cents_per_session": ("Min order value", "cents", {0: "No minimum"}),
        "require_cancel_reason": ("Cancel reason required", "", {}),
        "max_items_per_session": ("Max items per session", "items", {0: "Unlimited"}),
    }

    def _build_system_prompt(sys_policy: dict | None) -> str:
        if not sys_policy:
            return SYSTEM_PROMPT
        lines = [SYSTEM_PROMPT, "", "## Merchant rules (immutable for this session)", ""]
        for key, val in sys_policy.items():
            label, unit, specials = _SYSTEM_POLICY_LABELS.get(key, (key, "", {}))
            if val in specials:
                lines.append(f"- {label}: {specials[val]}")
            elif key == "min_amount_cents_per_session" and isinstance(val, (int, float)) and val > 0:
                lines.append(f"- {label}: ${val / 100:.2f}")
            elif isinstance(val, bool):
                lines.append(f"- {label}: {'Yes' if val else 'No'}")
            else:
                lines.append(f"- {label}: {val} {unit}".rstrip())
        lines.append("")
        lines.append("These are merchant rules. Enforce them and explain them when relevant.")
        return "\n".join(lines)

    # -- User policy → conversation context message (mutable mid-session) --

    _USER_POLICY_LABELS = {
        "max_tokens_per_session": ("Max tokens per session", "tokens", {0: "Unlimited"}),
        "max_amount_cents_per_session": ("Max spend per session", "cents", {0: "Unlimited"}),
    }

    def _fmt_policy_values(policy: dict) -> list[str]:
        parts = []
        for key, val in policy.items():
            label, unit, specials = _USER_POLICY_LABELS.get(key, (key, "", {}))
            if val in specials:
                parts.append(f"{label}: {specials[val]}")
            elif key == "max_amount_cents_per_session" and isinstance(val, (int, float)) and val > 0:
                parts.append(f"{label}: ${val / 100:.2f}")
            else:
                parts.append(f"{label}: {val} {unit}".rstrip())
        return parts

    def _format_user_policy_slot(session_id: str) -> str:
        current = _session_user_policy.get(session_id) or {}
        history = _session_user_policy_history.get(session_id, [])

        lines = ["[BUYER PREFERENCES — CURRENT]"]
        for part in _fmt_policy_values(current):
            lines.append(f"- {part}")

        if history:
            lines.append("")
            lines.append("Change log:")
            for i, (source, snapshot) in enumerate(history):
                vals = ", ".join(_fmt_policy_values(snapshot))
                lines.append(f"  [{i}] {source}: {vals}")

        lines.append("")
        lines.append("Use CURRENT values for enforcement. Reference the change log "
                      "to acknowledge transitions (e.g. 'you raised your limit from X to Y'). "
                      "Proactively mention budget limits before the buyer over-commits.")
        return "\n".join(lines)

    def _record_user_policy_change(session_id: str, source: str, snapshot: dict) -> None:
        if session_id not in _session_user_policy_history:
            _session_user_policy_history[session_id] = []
        _session_user_policy_history[session_id].append((source, dict(snapshot)))

    def _merge_policy(session_id: str) -> dict | None:
        """Merge system + user policy into one dict for API enforcement."""
        sp = _session_system_policy.get(session_id) or {}
        up = _session_user_policy.get(session_id) or {}
        merged = {**sp, **up}
        return merged if merged else None

    _USER_POLICY_SLOT = 1  # messages[1] is always the current buyer preferences

    @app.post("/chat")
    def chat_endpoint(req: ChatRequest):
        is_new = req.session_id not in _sessions

        if req.system_policy is not None:
            _session_system_policy[req.session_id] = req.system_policy

        if req.user_policy is not None:
            prev = _session_user_policy.get(req.session_id)
            if prev is not None and req.user_policy != prev:
                _record_user_policy_change(req.session_id, "buyer (UI)", dict(prev))
            _session_user_policy[req.session_id] = req.user_policy

        if is_new:
            sys_pol = _session_system_policy.get(req.session_id)
            user_pol = _session_user_policy.get(req.session_id) or {}
            _record_user_policy_change(req.session_id, "session start", dict(user_pol))
            _sessions[req.session_id] = [
                {"role": "system", "content": _build_system_prompt(sys_pol)},
                {"role": "system", "content": _format_user_policy_slot(req.session_id)},
            ]
            if sys_pol:
                log.info("SESSION [%s] system_policy: %s", req.session_id, json.dumps(sys_pol))
            log.info("SESSION [%s] user_policy: %s", req.session_id, json.dumps(user_pol))

        messages = _sessions[req.session_id]

        # Overwrite the user_policy slot with current truth + history
        messages[_USER_POLICY_SLOT] = {"role": "system", "content": _format_user_policy_slot(req.session_id)}

        msg_count = len([m for m in messages if m["role"] == "user"])

        log.info("=" * 70)
        log.info(
            "REQUEST [session=%s] msg#%d %s| user: %s",
            req.session_id, msg_count + 1,
            "(new session) " if is_new else "",
            req.message[:500],
        )

        messages.append({"role": "user", "content": req.message})

        def _on_update_user_policy(updates: dict) -> dict:
            current = _session_user_policy.get(req.session_id) or {}
            _record_user_policy_change(req.session_id, "buyer (chat)", dict(current))
            updated = {**current, **updates}
            _session_user_policy[req.session_id] = updated
            messages[_USER_POLICY_SLOT] = {"role": "system", "content": _format_user_policy_slot(req.session_id)}
            log.info("SESSION [%s] user_policy updated via agent: %s", req.session_id, json.dumps(updated))
            return updated

        client = create_client()
        merged = _merge_policy(req.session_id)
        reply, checkouts, trace = chat_completion(
            client, messages, merchant_policy=merged,
            on_update_user_policy=_on_update_user_policy,
        )
        messages.append(reply)

        _log_trace(trace, checkouts, req.session_id)

        # Auto-burn: calculate cost in cents and consume
        total_llm_tokens = 0
        for step in trace:
            if step["type"] == "llm":
                usage = step.get("usage", {})
                total_llm_tokens += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)

        cost_cents = max(10, total_llm_tokens // 100)
        burn_result = None
        try:
            from app.tools import acp_consume_tokens
            burn_result = acp_consume_tokens(
                user_id="demo_user",
                tokens=cost_cents,
                reason=f"chat:{req.session_id}",
            )
            log.info(
                "BURN [session=%s] llm_tokens=%d → $%.2f | balance=$%.2f",
                req.session_id, total_llm_tokens, cost_cents / 100,
                burn_result.get("balance", 0) / 100,
            )
        except Exception as e:
            log.warning("Token burn failed: %s", e)

        reply_preview = reply["content"][:400]
        if len(reply["content"]) > 400:
            reply_preview += "..."
        log.info("REPLY [session=%s]: %s", req.session_id, reply_preview)
        log.info(
            "RESPONSE SUMMARY | steps=%d tool_calls=%d checkouts=%d reply_len=%d",
            len(trace),
            sum(1 for s in trace if s["type"] == "tool_result"),
            len(checkouts),
            len(reply["content"]),
        )

        return {
            "reply": reply["content"],
            "checkouts": checkouts,
            "trace": trace,
            "session_id": req.session_id,
            "cost_cents": cost_cents,
            "llm_tokens_used": total_llm_tokens,
            "balance": burn_result.get("balance") if burn_result else None,
        }

    @app.post("/chat/reset")
    def reset_session(req: ChatRequest):
        _sessions.pop(req.session_id, None)
        _session_system_policy.pop(req.session_id, None)
        _session_user_policy.pop(req.session_id, None)
        _session_user_policy_history.pop(req.session_id, None)

        balance = None
        try:
            from app.tools import _http
            r = _http.post("/api/v1/reset-balance", json={"user_id": "demo_user"})
            r.raise_for_status()
            balance = r.json().get("balance")
            log.info("SESSION RESET [%s] — balance reset to $%.2f", req.session_id, (balance or 0) / 100)
        except Exception as e:
            log.warning("SESSION RESET [%s] — balance reset failed: %s", req.session_id, e)

        return {"reset": True, "session_id": req.session_id, "balance": balance}

    port = int(os.environ.get("AGENT_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    mode = os.environ.get("AGENT_MODE", "server")
    if mode == "terminal" or "--terminal" in sys.argv:
        run_terminal()
    else:
        run_server()
