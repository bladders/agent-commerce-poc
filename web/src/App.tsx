import { Fragment, useEffect, useRef, useState } from "react";

const API_BASE = (() => {
  const v = import.meta.env.VITE_API_URL;
  if (v !== undefined && String(v).trim() !== "") return String(v).replace(/\/$/, "");
  return "";
})();

const AGENT_BASE = (() => {
  const v = import.meta.env.VITE_AGENT_URL;
  if (v !== undefined && String(v).trim() !== "") return String(v).replace(/\/$/, "");
  return "";
})();

const INITIAL_BALANCE = 2000; // $20.00

/* ---------- Types ---------- */

type ACPCheckout = {
  id: string;
  status: string;
  currency: string;
  protocol?: { version: string };
  capabilities?: unknown;
  line_items: Array<{
    id: string;
    item: { id: string; name?: string; unit_amount?: number };
    quantity: number;
    name?: string;
    unit_amount?: number;
    totals?: Array<{ type: string; display_text: string; amount: number }>;
  }>;
  totals: Array<{ type: string; display_text: string; amount: number }>;
  fulfillment_options?: Array<{ type: string; id: string; title: string; description?: string }>;
  order?: { id: string; checkout_session_id: string; permalink_url: string; status: string };
  intent_trace?: { reason_code: string; trace_summary?: string };
  merchant_policy?: Record<string, unknown>;
  messages?: Array<{ type: string; content: string }>;
  links?: Array<{ type: string; url: string }>;
  _poc?: { user_id?: string; pack_label?: string; tokens?: number; balance_tokens?: number; payment_intent_id?: string | null };
};

type TraceStep = {
  type: "llm" | "tool_result";
  name?: string;
  arguments?: Record<string, unknown>;
  result?: unknown;
  duration_ms?: number;
  model?: string;
  action?: string;
  usage?: { prompt_tokens?: number; completion_tokens?: number };
  tool_calls?: Array<{ name: string; arguments: string }>;
};

type ChatEntry = {
  role: "user" | "assistant";
  content: string;
  checkouts?: ACPCheckout[];
  trace?: TraceStep[];
  costCents?: number;
  llmTokensUsed?: number;
  balance?: number | null;
};

/* ---------- Policy ---------- */

type SystemPolicy = {
  refund_window_minutes: number;
  min_amount_cents_per_session: number;
  require_cancel_reason: boolean;
  max_items_per_session: number;
};

type UserPolicy = {
  max_tokens_per_session: number;
  max_amount_cents_per_session: number;
};

const DEFAULT_SYSTEM_POLICY: SystemPolicy = {
  refund_window_minutes: 60,
  min_amount_cents_per_session: 0,
  require_cancel_reason: true,
  max_items_per_session: 10,
};

const DEFAULT_USER_POLICY: UserPolicy = {
  max_tokens_per_session: 500,
  max_amount_cents_per_session: 0,
};

function loadSystemPolicy(): SystemPolicy {
  try {
    const raw = localStorage.getItem("system_policy");
    if (raw) return { ...DEFAULT_SYSTEM_POLICY, ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return { ...DEFAULT_SYSTEM_POLICY };
}

function loadUserPolicy(): UserPolicy {
  try {
    const raw = localStorage.getItem("user_policy");
    if (raw) return { ...DEFAULT_USER_POLICY, ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return { ...DEFAULT_USER_POLICY };
}

/* ---------- Structured data from trace ---------- */

type CatalogItem = {
  id: string;
  product_id?: string;
  name: string;
  description: string;
  tokens: number;
  amount: number;
  currency: string;
};

function extractCatalogItems(trace?: TraceStep[]): CatalogItem[] | null {
  if (!trace) return null;
  for (const step of trace) {
    if (step.type === "tool_result" && step.name === "list_catalog") {
      const res = step.result as { items?: CatalogItem[] } | undefined;
      if (res?.items && res.items.length > 0) return res.items;
    }
  }
  return null;
}

function extractBalanceData(trace?: TraceStep[]): { user_id: string; cents: number } | null {
  if (!trace) return null;
  for (const step of trace) {
    if (step.type === "tool_result" && step.name === "get_balance") {
      const res = step.result as { user_id?: string; balance_cents?: number; tokens?: number } | undefined;
      const cents = res?.balance_cents ?? res?.tokens;
      if (res && cents != null) return { user_id: res.user_id || "demo_user", cents };
    }
  }
  return null;
}

/* ---------- Structured data: refunds, errors, Stripe introspection ---------- */

type RefundData = {
  refund_id: string;
  status: string;
  amount: number;
  currency: string;
  checkout_session_id: string;
  amount_refunded_cents: number;
  balance_tokens: number;
};

function extractRefundData(trace?: TraceStep[]): RefundData | null {
  if (!trace) return null;
  for (const step of trace) {
    if (step.type === "tool_result" && step.name === "refund_checkout_session") {
      const res = step.result as RefundData | undefined;
      if (res?.refund_id) return res;
    }
  }
  return null;
}

type ToolError = {
  toolName: string;
  error: string;
  detail?: string | { detail?: string };
};

function extractToolErrors(trace?: TraceStep[]): ToolError[] {
  if (!trace) return [];
  const errors: ToolError[] = [];
  for (const step of trace) {
    if (step.type === "tool_result" && step.name) {
      const res = step.result as Record<string, unknown> | undefined;
      if (res && "error" in res) {
        const detailRaw = res.detail;
        let detail: string | undefined;
        if (typeof detailRaw === "string") detail = detailRaw;
        else if (detailRaw && typeof detailRaw === "object" && "detail" in (detailRaw as Record<string, unknown>))
          detail = String((detailRaw as Record<string, string>).detail);
        errors.push({ toolName: step.name, error: String(res.error), detail });
      }
    }
  }
  return errors;
}

type StripeIntrospection = {
  type: "products" | "prices" | "account" | "balance" | "customers" | "payments";
  data: Record<string, unknown>;
};

function extractStripeIntrospection(trace?: TraceStep[]): StripeIntrospection[] {
  if (!trace) return [];
  const results: StripeIntrospection[] = [];
  const map: Record<string, StripeIntrospection["type"]> = {
    stripe_list_products: "products",
    stripe_list_prices: "prices",
    stripe_get_account_info: "account",
    stripe_get_balance: "balance",
    stripe_list_customers: "customers",
    stripe_list_payment_intents: "payments",
  };
  for (const step of trace) {
    if (step.type === "tool_result" && step.name && step.name in map) {
      const res = step.result;
      if (res && typeof res === "object" && !("error" in (res as Record<string, unknown>))) {
        results.push({ type: map[step.name], data: res as Record<string, unknown> });
      }
    }
  }
  return results;
}

/* ---------- Helpers ---------- */

function apiHeaders(): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  const key = import.meta.env.VITE_POC_API_KEY;
  if (key) h["X-POC-API-KEY"] = key;
  return h;
}

const fmt = (cents: number, cur: string) =>
  new Intl.NumberFormat(undefined, { style: "currency", currency: cur.toUpperCase() }).format(cents / 100);

const fmtUsd = (cents: number) => `$${(cents / 100).toFixed(2)}`;

/* ========== Balance Pill ========== */

function BalancePill({ cents }: { cents: number }) {
  const color = cents > 1000 ? "#059669" : cents > 500 ? "#d97706" : "#dc2626";
  const bg = cents > 1000 ? "#ecfdf5" : cents > 500 ? "#fffbeb" : "#fef2f2";
  const pulse = cents <= 500 && cents > 0;

  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "6px 14px", borderRadius: 20,
      background: bg, border: `1.5px solid ${color}`,
      fontWeight: 700, fontSize: "0.9rem", color,
      fontVariantNumeric: "tabular-nums",
      animation: pulse ? "pulse 2s ease-in-out infinite" : undefined,
    }}>
      <span style={{ fontSize: "0.7rem", opacity: 0.7, fontWeight: 500 }}>BAL</span>
      {fmtUsd(cents)}
    </div>
  );
}

/* ========== Catalog Grid ========== */

const PACK_ICONS: Record<number, string> = { 1: "1", 10: "10", 25: "25", 50: "50", 100: "100" };

function CatalogGrid({ items, onBuy }: { items: CatalogItem[]; onBuy: (item: CatalogItem) => void }) {
  return (
    <div style={{
      display: "grid",
      gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
      gap: 12, padding: "4px 0", maxWidth: 580, marginTop: 8,
      whiteSpace: "normal",
    }}>
      {items.map((item) => {
        const badge = PACK_ICONS[item.tokens] || String(item.tokens);
        return (
          <div key={item.id} style={{
            background: "#fff", borderRadius: 14, padding: 16,
            boxShadow: "0 2px 8px rgba(0,0,0,0.06)",
            display: "flex", flexDirection: "column", gap: 8,
            border: "1px solid #e2e8f0", transition: "box-shadow 0.15s, transform 0.15s",
            cursor: "pointer",
          }}
            onMouseEnter={(e) => {
              e.currentTarget.style.boxShadow = "0 4px 16px rgba(3,105,161,0.15)";
              e.currentTarget.style.transform = "translateY(-2px)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.boxShadow = "0 2px 8px rgba(0,0,0,0.06)";
              e.currentTarget.style.transform = "translateY(0)";
            }}
          >
            <div style={{
              width: 44, height: 44, borderRadius: 12,
              background: "linear-gradient(135deg, #0ea5e9, #0284c7)",
              display: "flex", alignItems: "center", justifyContent: "center",
              color: "#fff", fontWeight: 800, fontSize: badge.length > 2 ? "0.7rem" : "0.9rem",
              letterSpacing: "-0.02em",
            }}>
              {badge}
            </div>
            <div>
              <div style={{ fontWeight: 700, fontSize: "0.88rem", color: "#0f172a", lineHeight: 1.3 }}>
                {item.name}
              </div>
              {item.description && (
                <div style={{ fontSize: "0.75rem", color: "#94a3b8", marginTop: 2, lineHeight: 1.3 }}>
                  {item.description}
                </div>
              )}
            </div>
            <div style={{ display: "flex", alignItems: "baseline", gap: 4, marginTop: "auto" }}>
              <span style={{ fontSize: "1.1rem", fontWeight: 800, color: "#0f172a" }}>
                {fmt(item.amount, item.currency)}
              </span>
              <span style={{ fontSize: "0.7rem", color: "#94a3b8" }}>
                {item.tokens} {item.tokens === 1 ? "credit" : "credits"}
              </span>
            </div>
            <button type="button" onClick={() => onBuy(item)} style={{
              width: "100%", padding: "8px 0", borderRadius: 8, border: "none",
              background: "linear-gradient(135deg, #0369a1, #0284c7)",
              color: "#fff", fontWeight: 700, fontSize: "0.78rem", cursor: "pointer",
              transition: "opacity 0.15s",
            }}
              onMouseEnter={(e) => { e.currentTarget.style.opacity = "0.9"; }}
              onMouseLeave={(e) => { e.currentTarget.style.opacity = "1"; }}
            >Buy</button>
          </div>
        );
      })}
    </div>
  );
}

/* ========== Balance Card (inline in chat) ========== */

function BalanceInline({ cents }: { cents: number }) {
  const color = cents > 1000 ? "#059669" : cents > 500 ? "#d97706" : "#dc2626";
  const bg = cents > 1000 ? "#ecfdf5" : cents > 500 ? "#fffbeb" : "#fef2f2";
  return (
    <div style={{
      display: "inline-flex", alignItems: "center", gap: 10,
      padding: "12px 20px", borderRadius: 14, marginTop: 8,
      background: bg, border: `1.5px solid ${color}`,
      maxWidth: 300, whiteSpace: "normal",
    }}>
      <div style={{
        width: 40, height: 40, borderRadius: 10,
        background: color, display: "flex", alignItems: "center", justifyContent: "center",
        color: "#fff", fontWeight: 800, fontSize: "1rem",
      }}>$</div>
      <div>
        <div style={{ fontSize: "0.72rem", color, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>
          Current Balance
        </div>
        <div style={{ fontSize: "1.3rem", fontWeight: 800, color: "#0f172a", fontVariantNumeric: "tabular-nums" }}>
          {fmtUsd(cents)}
        </div>
      </div>
    </div>
  );
}

/* ========== Refund Card (inline in chat) ========== */

function RefundInline({ data }: { data: RefundData }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 12,
      padding: "14px 20px", borderRadius: 14, marginTop: 8,
      background: "#fef2f2", border: "1.5px solid #fca5a5",
      maxWidth: 380, whiteSpace: "normal",
    }}>
      <div style={{
        width: 40, height: 40, borderRadius: 10, flexShrink: 0,
        background: "#dc2626", display: "flex", alignItems: "center", justifyContent: "center",
        color: "#fff", fontSize: "1.1rem",
      }}>&#8617;</div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: "0.72rem", color: "#dc2626", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>
          Refund {data.status === "succeeded" ? "Processed" : data.status}
        </div>
        <div style={{ fontSize: "1.2rem", fontWeight: 800, color: "#0f172a", fontVariantNumeric: "tabular-nums" }}>
          -{fmtUsd(data.amount_refunded_cents)}
        </div>
        <div style={{ fontSize: "0.72rem", color: "#94a3b8", marginTop: 2 }}>
          {data.refund_id} &middot; Balance: {fmtUsd(data.balance_tokens)}
        </div>
      </div>
    </div>
  );
}

/* ========== Error Alert (inline in chat) ========== */

function ErrorAlerts({ errors }: { errors: ToolError[] }) {
  if (errors.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 8, whiteSpace: "normal" }}>
      {errors.map((err, i) => {
        const detail = err.detail || "";
        const isPolicy = typeof detail === "string" && detail.toLowerCase().includes("policy");
        return (
          <div key={i} style={{
            display: "flex", alignItems: "flex-start", gap: 10,
            padding: "10px 14px", borderRadius: 10,
            background: isPolicy ? "#fffbeb" : "#fef2f2",
            border: `1px solid ${isPolicy ? "#fcd34d" : "#fca5a5"}`,
            maxWidth: 440,
          }}>
            <div style={{
              width: 28, height: 28, borderRadius: 7, flexShrink: 0, marginTop: 1,
              background: isPolicy ? "#f59e0b" : "#ef4444",
              display: "flex", alignItems: "center", justifyContent: "center",
              color: "#fff", fontWeight: 800, fontSize: "0.85rem",
            }}>
              {isPolicy ? "!" : "\u2717"}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{
                fontSize: "0.76rem", fontWeight: 700,
                color: isPolicy ? "#92400e" : "#991b1b",
              }}>
                {isPolicy ? "Policy Violation" : `Error: ${err.toolName}`}
              </div>
              <div style={{ fontSize: "0.8rem", color: "#475569", marginTop: 2, lineHeight: 1.4 }}>
                {typeof detail === "string" ? detail : err.error}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ========== Stripe Introspection Cards (inline in chat) ========== */

function StripeIntrospectionCards({ items }: { items: StripeIntrospection[] }) {
  if (items.length === 0) return null;

  const typeLabels: Record<string, string> = {
    products: "Stripe Products",
    prices: "Stripe Prices",
    account: "Account Info",
    balance: "Stripe Balance",
    customers: "Customers",
    payments: "Recent Payments",
  };
  const typeColors: Record<string, string> = {
    products: "#6366f1",
    prices: "#0ea5e9",
    account: "#8b5cf6",
    balance: "#059669",
    customers: "#d97706",
    payments: "#0369a1",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8, whiteSpace: "normal" }}>
      {items.map((item, i) => {
        const color = typeColors[item.type] || "#64748b";
        const listKey = Object.keys(item.data).find(k => Array.isArray(item.data[k]));
        const listItems = listKey ? (item.data[listKey] as Record<string, unknown>[]) : null;

        return (
          <div key={i} style={{
            padding: "12px 16px", borderRadius: 12,
            background: "#fff", border: `1px solid #e2e8f0`,
            boxShadow: "0 1px 4px rgba(0,0,0,0.04)",
            maxWidth: 440,
          }}>
            <div style={{
              display: "flex", alignItems: "center", gap: 8, marginBottom: 8,
            }}>
              <div style={{
                width: 8, height: 8, borderRadius: 4, background: color,
              }} />
              <span style={{ fontSize: "0.78rem", fontWeight: 700, color }}>
                {typeLabels[item.type] || item.type}
              </span>
              {listItems && (
                <span style={{ fontSize: "0.7rem", color: "#94a3b8", marginLeft: "auto" }}>
                  {listItems.length} {listItems.length === 1 ? "item" : "items"}
                </span>
              )}
            </div>

            {listItems ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {listItems.slice(0, 5).map((row, j) => (
                  <div key={j} style={{
                    display: "flex", justifyContent: "space-between", alignItems: "center",
                    padding: "6px 10px", borderRadius: 8, background: "#f8fafc",
                    fontSize: "0.8rem",
                  }}>
                    <span style={{ color: "#0f172a", fontWeight: 600 }}>
                      {String(row.name || row.id || `#${j + 1}`)}
                    </span>
                    {row.unit_amount != null && (
                      <span style={{ color: "#64748b", fontWeight: 500 }}>
                        {fmtUsd(row.unit_amount as number)}
                      </span>
                    )}
                    {row.amount != null && !("unit_amount" in row) && (
                      <span style={{ color: "#64748b", fontWeight: 500 }}>
                        {fmtUsd(row.amount as number)}
                      </span>
                    )}
                    {"email" in row && row.email ? (
                      <span style={{ color: "#94a3b8", fontSize: "0.75rem" }}>
                        {String(row.email)}
                      </span>
                    ) : null}
                    {"status" in row && row.status ? (
                      <span style={{
                        fontSize: "0.7rem", fontWeight: 600, padding: "1px 8px", borderRadius: 6,
                        background: row.status === "succeeded" ? "#dcfce7" : "#f1f5f9",
                        color: row.status === "succeeded" ? "#166534" : "#64748b",
                      }}>
                        {String(row.status)}
                      </span>
                    ) : null}
                  </div>
                ))}
                {listItems.length > 5 && (
                  <div style={{ fontSize: "0.72rem", color: "#94a3b8", textAlign: "center", padding: 4 }}>
                    +{listItems.length - 5} more
                  </div>
                )}
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", fontSize: "0.8rem" }}>
                {Object.entries(item.data).slice(0, 8).map(([k, v]) => (
                  <Fragment key={k}>
                    <span style={{ color: "#94a3b8", fontWeight: 500 }}>{k}</span>
                    <span style={{ color: "#0f172a", fontWeight: 600 }}>
                      {typeof v === "boolean" ? (v ? "Yes" : "No") : String(v ?? "—")}
                    </span>
                  </Fragment>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ========== Session Burn Bar ========== */

function SessionBurnBar({ spent, total }: { spent: number; total: number }) {
  const pct = Math.min(100, (spent / total) * 100);
  const color = pct > 75 ? "#dc2626" : pct > 40 ? "#d97706" : "#0369a1";
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "0 24px 0",
    }}>
      <div style={{
        flex: 1, height: 4, borderRadius: 2, background: "rgba(255,255,255,0.1)",
        overflow: "hidden",
      }}>
        <div style={{
          width: `${pct}%`, height: "100%", borderRadius: 2,
          background: color, transition: "width 0.4s ease, background 0.3s",
        }} />
      </div>
      <span style={{
        fontSize: "0.68rem", color: "rgba(255,255,255,0.5)", fontWeight: 600,
        fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap",
      }}>
        {fmtUsd(spent)} used
      </span>
    </div>
  );
}

/* ========== App ========== */

export default function App() {
  const [mode, setMode] = useState<"conversational" | "ui">("ui");
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(() => `s_${Date.now()}`);
  const [traceOpen, setTraceOpen] = useState(true);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [systemPolicy, setSystemPolicy] = useState<SystemPolicy>(loadSystemPolicy);
  const [userPolicy, setUserPolicy] = useState<UserPolicy>(loadUserPolicy);
  const [balance, setBalance] = useState<number>(INITIAL_BALANCE);
  const [totalBurned, setTotalBurned] = useState(0);
  const chatBottomRef = useRef<HTMLDivElement>(null);
  const traceBottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => { chatBottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, loading]);
  useEffect(() => { traceBottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  // Seed balance on first load
  useEffect(() => {
    fetch(`${AGENT_BASE}/agent/chat/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: "", session_id: sessionId }),
    })
      .then((r) => r.json())
      .then((j) => { if (j.balance != null) setBalance(j.balance); })
      .catch(() => {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function resetSession() {
    const newId = `s_${Date.now()}`;
    try {
      const r = await fetch(`${AGENT_BASE}/agent/chat/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: "", session_id: sessionId }),
      });
      const j = await r.json();
      if (j.balance != null) setBalance(j.balance);
    } catch { /* ignore */ }
    setSessionId(newId);
    setMessages([]);
    setTotalBurned(0);
  }

  function commitSystemPolicy(patch: Partial<SystemPolicy>) {
    setSystemPolicy((prev) => {
      const next = { ...prev, ...patch };
      localStorage.setItem("system_policy", JSON.stringify(next));
      return next;
    });
    resetSession();
  }

  function commitUserPolicy(patch: Partial<UserPolicy>) {
    setUserPolicy((prev) => {
      const next = { ...prev, ...patch };
      localStorage.setItem("user_policy", JSON.stringify(next));
      return next;
    });
  }

  async function send(text?: string) {
    const msg = (text ?? input).trim();
    if (!msg || loading) return;
    if (!text) setInput("");
    setMessages((m) => [...m, { role: "user", content: msg }]);
    setLoading(true);

    try {
      const r = await fetch(`${AGENT_BASE}/agent/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg, session_id: sessionId, system_policy: systemPolicy, user_policy: userPolicy }),
      });
      if (!r.ok) {
        const errText = await r.text();
        setMessages((m) => [...m, { role: "assistant", content: `Error: ${errText}` }]);
        return;
      }
      const j = await r.json();

      if (j.trace) {
        for (const step of j.trace as TraceStep[]) {
          if (step.type === "tool_result" && step.name === "update_buyer_preferences") {
            const res = step.result as Record<string, unknown> | undefined;
            const bp = res?.buyer_preferences as Record<string, number> | undefined;
            if (bp) {
              setUserPolicy((prev) => {
                const next = { ...prev, ...bp };
                localStorage.setItem("user_policy", JSON.stringify(next));
                return next;
              });
            }
          }
        }
      }

      if (j.balance != null) setBalance(j.balance);
      if (j.cost_cents) setTotalBurned((prev) => prev + j.cost_cents);

      setMessages((m) => [
        ...m,
        {
          role: "assistant", content: j.reply, checkouts: j.checkouts, trace: j.trace,
          costCents: j.cost_cents, llmTokensUsed: j.llm_tokens_used, balance: j.balance,
        },
      ]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: "assistant", content: `Connection error: ${e instanceof Error ? e.message : String(e)}` },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function handleCheckoutAction(
    checkoutId: string,
    action: "complete" | "cancel",
    intentTrace?: { reason_code: string; trace_summary: string },
  ) {
    setLoading(true);
    try {
      const body: Record<string, unknown> = {};
      if (action === "cancel" && intentTrace) {
        body.intent_trace = intentTrace;
      }
      const r = await fetch(`${API_BASE}/checkout_sessions/${checkoutId}/${action}`, {
        method: "POST", headers: apiHeaders(), body: JSON.stringify(body),
      });
      const result = await r.json();
      if (!r.ok) {
        setMessages((m) => [...m, { role: "assistant", content: `Action failed: ${result.detail || JSON.stringify(result)}` }]);
        return;
      }
      const verb = action === "complete" ? "completed" : "canceled";
      let summary = `Checkout ${verb}.`;
      if (action === "complete" && result._poc?.balance_tokens != null) {
        setBalance(result._poc.balance_tokens);
        summary += ` Balance: ${fmtUsd(result._poc.balance_tokens)}.`;
      }
      if (action === "complete" && result.order)
        summary += ` Order: ${result.order.id} (${result.order.status}).`;
      if (action === "cancel" && intentTrace)
        summary += ` Reason: ${intentTrace.trace_summary || intentTrace.reason_code}.`;
      setMessages((m) => [...m, {
        role: "assistant", content: summary, checkouts: [result],
        trace: [{ type: "tool_result", name: `${action}_checkout_session`, arguments: { checkout_session_id: checkoutId, ...intentTrace }, result, duration_ms: 0 }],
      }]);
      const agentMsg = action === "cancel" && intentTrace
        ? `[System: user canceled checkout session ${checkoutId} via UI — reason: ${intentTrace.reason_code} — "${intentTrace.trace_summary}"]`
        : `[System: user ${verb} checkout session ${checkoutId} via UI button]`;
      await fetch(`${AGENT_BASE}/agent/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: agentMsg, session_id: sessionId }),
      });
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", content: `Error: ${e instanceof Error ? e.message : String(e)}` }]);
    } finally {
      setLoading(false);
    }
  }

  async function handleRefund(checkoutId: string, reason: string) {
    setLoading(true);
    try {
      const r = await fetch(`${API_BASE}/api/v1/refund`, {
        method: "POST", headers: apiHeaders(),
        body: JSON.stringify({ checkout_session_id: checkoutId, reason }),
      });
      const result = await r.json();
      if (!r.ok) {
        setMessages((m) => [...m, { role: "assistant", content: `Refund failed: ${result.detail || JSON.stringify(result)}` }]);
        return;
      }
      if (result.balance_tokens != null) setBalance(result.balance_tokens);
      const summary = `Refund processed: ${fmtUsd(result.amount_refunded_cents ?? result.amount ?? 0)} returned.`;
      setMessages((m) => [...m, {
        role: "assistant", content: summary, checkouts: result.checkout ? [result.checkout] : [],
        trace: [{ type: "tool_result", name: "refund_checkout_session", arguments: { checkout_session_id: checkoutId, reason }, result, duration_ms: 0 }],
      }]);
      await fetch(`${AGENT_BASE}/agent/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: `[System: user refunded checkout session ${checkoutId} via UI — reason: "${reason}"]`, session_id: sessionId }),
      });
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", content: `Error: ${e instanceof Error ? e.message : String(e)}` }]);
    } finally {
      setLoading(false);
    }
  }

  const allTrace = messages.flatMap((m) => (m.trace || []).map((t) => ({ ...t, _msg: m })));

  return (
    <div style={{ display: "flex", height: "100vh", fontFamily: "'Inter', system-ui, -apple-system, sans-serif" }}>
      {/* ===== Left: Chat ===== */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, background: "#f8fafc" }}>
        <header style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "12px 24px",
          background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
          color: "#fff",
          boxShadow: "0 1px 3px rgba(0,0,0,0.12)",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <h1 style={{ fontSize: "1rem", margin: 0, fontWeight: 700, letterSpacing: "-0.02em" }}>Agent Commerce</h1>
            <div style={{ display: "flex", gap: 2, background: "rgba(255,255,255,0.1)", borderRadius: 8, padding: 2 }}>
              {(["conversational", "ui"] as const).map((m) => (
                <button key={m} type="button" onClick={() => setMode(m)} style={{
                  padding: "5px 12px", borderRadius: 6, border: "none", fontSize: "0.78rem", fontWeight: 600,
                  cursor: "pointer",
                  background: mode === m ? "rgba(255,255,255,0.2)" : "transparent",
                  color: mode === m ? "#fff" : "rgba(255,255,255,0.5)",
                  transition: "all 0.15s",
                }}>
                  {m === "conversational" ? "Chat" : "UI"}
                </button>
              ))}
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <BalancePill cents={balance} />
            <button type="button" onClick={() => setPolicyOpen(!policyOpen)} style={{
              padding: "5px 12px", borderRadius: 6, border: "1px solid",
              borderColor: policyOpen ? "#a78bfa" : "rgba(255,255,255,0.2)",
              background: policyOpen ? "rgba(167,139,250,0.15)" : "transparent",
              color: policyOpen ? "#c4b5fd" : "rgba(255,255,255,0.6)",
              fontSize: "0.78rem", fontWeight: 600, cursor: "pointer", transition: "all 0.15s",
            }}>Policy</button>
            <button type="button" onClick={resetSession} style={{
              padding: "5px 12px", borderRadius: 6, border: "1px solid rgba(255,255,255,0.2)",
              background: "transparent", color: "rgba(255,255,255,0.6)",
              fontSize: "0.78rem", fontWeight: 600, cursor: "pointer", transition: "all 0.15s",
            }}>Reset</button>
          </div>
          {totalBurned > 0 && (
            <div style={{ background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)", paddingBottom: 8 }}>
              <SessionBurnBar spent={totalBurned} total={INITIAL_BALANCE} />
            </div>
          )}
        </header>

        {policyOpen && (
          <PolicyPanel
            systemPolicy={systemPolicy} onCommitSystem={commitSystemPolicy}
            userPolicy={userPolicy} onCommitUser={commitUserPolicy}
          />
        )}

        <div style={{ flex: 1, overflow: "auto", padding: "20px 24px", display: "flex", flexDirection: "column", gap: 16 }}>
          {messages.length === 0 && (
            <div style={{ textAlign: "center", marginTop: "4rem" }}>
              <div style={{ fontSize: "2rem", marginBottom: 12, opacity: 0.3 }}>$</div>
              <p style={{ color: "#94a3b8", fontSize: "0.95rem", margin: 0, fontWeight: 500 }}>
                Your session starts with {fmtUsd(INITIAL_BALANCE)}
              </p>
              <p style={{ color: "#cbd5e1", fontSize: "0.85rem", margin: "6px 0 0" }}>
                Try: "What can I buy?" or "Show me the catalog"
              </p>
            </div>
          )}
          {messages.map((m, i) => {
            const catalogItems = m.role === "assistant" ? extractCatalogItems(m.trace) : null;
            const balanceData = m.role === "assistant" ? extractBalanceData(m.trace) : null;
            const refundData = m.role === "assistant" ? extractRefundData(m.trace) : null;
            const toolErrors = m.role === "assistant" ? extractToolErrors(m.trace) : [];
            const stripeData = m.role === "assistant" ? extractStripeIntrospection(m.trace) : [];
            const hasWideContent = !!(catalogItems || stripeData.length);

            return (
              <div key={i} style={{ animation: "fadeIn 0.2s ease-out" }}>
                <div style={{ display: "flex", justifyContent: m.role === "user" ? "flex-end" : "flex-start" }}>
                  <div style={{
                    maxWidth: hasWideContent ? "100%" : "75%",
                    padding: "12px 16px", borderRadius: 16,
                    background: m.role === "user"
                      ? "linear-gradient(135deg, #0369a1, #0284c7)"
                      : "#fff",
                    color: m.role === "user" ? "#fff" : "#1e293b",
                    boxShadow: m.role === "user"
                      ? "0 2px 8px rgba(3,105,161,0.25)"
                      : "0 1px 4px rgba(0,0,0,0.06)",
                    whiteSpace: "pre-wrap", fontSize: "0.9rem", lineHeight: 1.6,
                  }}>
                    {m.content}
                    {catalogItems && (
                      <CatalogGrid items={catalogItems} onBuy={(item) => send(`I'd like to buy ${item.name}`)} />
                    )}
                    {balanceData && (
                      <BalanceInline cents={balanceData.cents} />
                    )}
                    {refundData && (
                      <RefundInline data={refundData} />
                    )}
                    {toolErrors.length > 0 && (
                      <ErrorAlerts errors={toolErrors} />
                    )}
                    {stripeData.length > 0 && (
                      <StripeIntrospectionCards items={stripeData} />
                    )}
                  </div>
                </div>
                {m.role === "assistant" && m.costCents != null && (
                  <div style={{
                    display: "flex", gap: 8, fontSize: "0.72rem", color: "#94a3b8",
                    marginTop: 4, paddingLeft: 4, alignItems: "center",
                  }}>
                    <span style={{
                      background: "#f1f5f9", padding: "1px 8px", borderRadius: 10, fontWeight: 600,
                      color: "#64748b",
                    }}>
                      -{fmtUsd(m.costCents)}
                    </span>
                    <span style={{ color: "#cbd5e1" }}>{(m.llmTokensUsed ?? 0).toLocaleString()} LLM tokens</span>
                  </div>
                )}
                {m.checkouts?.map((co) => (
                  <CheckoutCard key={co.id + co.status} checkout={co} mode={mode} loading={loading} onAction={handleCheckoutAction} onRefund={handleRefund} />
                ))}
              </div>
            );
          })}
          {loading && (
            <div style={{ display: "flex", gap: 4, padding: "8px 4px" }}>
              {[0, 1, 2].map((i) => (
                <div key={i} style={{
                  width: 8, height: 8, borderRadius: 4, background: "#94a3b8",
                  animation: `bounce 1.4s ease-in-out ${i * 0.16}s infinite`,
                }} />
              ))}
            </div>
          )}
          <div ref={chatBottomRef} />
        </div>

        <form onSubmit={(e) => { e.preventDefault(); send(); }}
          style={{
            display: "flex", gap: 10, padding: "14px 24px",
            background: "#fff", boxShadow: "0 -2px 8px rgba(0,0,0,0.04)",
          }}>
          <input style={{
            flex: 1, padding: "12px 18px", borderRadius: 24, border: "1px solid #e2e8f0",
            fontSize: "0.9rem", outline: "none", transition: "border-color 0.15s",
            background: "#f8fafc",
          }}
            value={input} onChange={(e) => setInput(e.target.value)}
            onFocus={(e) => { e.target.style.borderColor = "#0369a1"; }}
            onBlur={(e) => { e.target.style.borderColor = "#e2e8f0"; }}
            placeholder={mode === "conversational" ? 'Type "yes" to confirm, or ask anything...' : "Chat with the agent..."}
            disabled={loading} />
          <button type="submit" disabled={loading || !input.trim()} style={{
            padding: "12px 24px", borderRadius: 24, border: "none",
            background: loading || !input.trim() ? "#94a3b8" : "#0369a1",
            color: "#fff", fontWeight: 700, fontSize: "0.85rem", cursor: "pointer",
            transition: "background 0.15s",
          }}>Send</button>
        </form>
      </div>

      {/* ===== Right: Trace Panel ===== */}
      <div style={{
        width: traceOpen ? 380 : 36, transition: "width 0.2s",
        borderLeft: "1px solid #1e293b", background: "#0f172a", color: "#e2e8f0",
        display: "flex", flexDirection: "column", overflow: "hidden",
      }}>
        <button type="button" onClick={() => setTraceOpen(!traceOpen)} style={{
          padding: "12px 12px", background: "#1e293b", border: "none", borderBottom: "1px solid #334155",
          color: "#94a3b8", fontSize: "0.78rem", fontWeight: 600, cursor: "pointer", textAlign: "left",
          whiteSpace: "nowrap",
        }}>
          {traceOpen ? "Trace Panel" : "T"}
        </button>

        {traceOpen && (
          <div style={{ flex: 1, overflow: "auto", padding: 12, fontSize: "0.76rem", lineHeight: 1.5 }}>
            {allTrace.length === 0 && (
              <p style={{ color: "#475569", fontStyle: "italic", fontSize: "0.78rem" }}>
                Tool calls and LLM steps will appear here.
              </p>
            )}
            {allTrace.map((step, i) => (
              <TraceEntry key={i} step={step} />
            ))}
            <div ref={traceBottomRef} />
          </div>
        )}
      </div>
    </div>
  );
}

/* ========== Trace Entry ========== */

function TraceEntry({ step }: { step: TraceStep }) {
  const [expanded, setExpanded] = useState(false);

  if (step.type === "llm") {
    const tokens = step.usage
      ? `${step.usage.prompt_tokens ?? "?"}+${step.usage.completion_tokens ?? "?"}`
      : "";
    return (
      <div style={{ marginBottom: 6, padding: "6px 8px", background: "#1e293b", borderRadius: 6, borderLeft: "2px solid #818cf8" }}>
        <div style={{ color: "#818cf8", fontWeight: 600, fontSize: "0.76rem" }}>
          LLM {step.action === "tool_calls" ? "-> tools" : "-> reply"}{" "}
          <span style={{ color: "#475569", fontWeight: 400 }}>{step.duration_ms}ms / {tokens}</span>
        </div>
        {step.tool_calls && (
          <div style={{ marginTop: 3, color: "#94a3b8" }}>
            {step.tool_calls.map((tc, j) => (
              <div key={j} style={{ fontSize: "0.74rem" }}>
                <span style={{ color: "#fbbf24" }}>{tc.name}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  const isError = typeof step.result === "object" && step.result !== null && "error" in (step.result as Record<string, unknown>);
  return (
    <div style={{ marginBottom: 6, padding: "6px 8px", background: "#1e293b", borderRadius: 6, borderLeft: `2px solid ${isError ? "#f87171" : "#34d399"}` }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>
          <span style={{ color: isError ? "#f87171" : "#34d399", fontWeight: 600, fontSize: "0.76rem" }}>{step.name}</span>
          <span style={{ color: "#475569", fontSize: "0.74rem" }}> {step.duration_ms}ms</span>
        </span>
        <button type="button" onClick={() => setExpanded(!expanded)} style={{
          background: "none", border: "none", color: "#64748b", cursor: "pointer", fontSize: "0.72rem",
        }}>
          {expanded ? "[-]" : "[+]"}
        </button>
      </div>
      {!expanded && step.arguments && Object.keys(step.arguments).length > 0 && (
        <div style={{ color: "#64748b", marginTop: 2, fontSize: "0.72rem" }}>
          ({Object.entries(step.arguments).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})
        </div>
      )}
      {expanded && (
        <div style={{ marginTop: 6, fontSize: "0.72rem" }}>
          <div style={{ color: "#64748b", marginBottom: 2 }}>args:</div>
          <pre style={{ margin: 0, color: "#cbd5e1", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
            {JSON.stringify(step.arguments, null, 2)}
          </pre>
          <div style={{ color: "#64748b", marginTop: 6, marginBottom: 2 }}>result:</div>
          <pre style={{ margin: 0, color: "#cbd5e1", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 200, overflow: "auto" }}>
            {JSON.stringify(step.result, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

/* ========== Policy Panel ========== */

type PolicyFieldDef = {
  key: string;
  label: string;
  hint: string;
  type: "number" | "boolean";
  zeroLabel?: string;
  negLabel?: string;
};

const SYSTEM_POLICY_FIELDS: PolicyFieldDef[] = [
  { key: "refund_window_minutes", label: "Refund window", hint: "minutes", type: "number", zeroLabel: "No refunds", negLabel: "Unlimited" },
  { key: "min_amount_cents_per_session", label: "Min order value", hint: "cents", type: "number", zeroLabel: "No minimum" },
  { key: "max_items_per_session", label: "Max items / session", hint: "items", type: "number", zeroLabel: "Unlimited" },
  { key: "require_cancel_reason", label: "Require cancel reason", hint: "", type: "boolean" },
];

const USER_POLICY_FIELDS: PolicyFieldDef[] = [
  { key: "max_tokens_per_session", label: "Max tokens / session", hint: "tokens", type: "number", zeroLabel: "Unlimited" },
  { key: "max_amount_cents_per_session", label: "Max spend / session", hint: "cents", type: "number", zeroLabel: "Unlimited" },
];

function NumberPolicyInput({ value, field, onCommit }: {
  value: number;
  field: PolicyFieldDef;
  onCommit: (v: number) => void;
}) {
  const [local, setLocal] = useState(String(value));
  useEffect(() => { setLocal(String(value)); }, [value]);

  function commit() {
    const n = parseInt(local) || 0;
    if (n !== value) onCommit(n);
  }

  const displayVal = parseInt(local) || 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <input
        type="number"
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") commit(); }}
        style={{
          width: 72, padding: "4px 8px", borderRadius: 6, border: "1px solid #c4b5fd",
          fontSize: "0.82rem", textAlign: "right",
        }}
      />
      <span style={{ color: "#8b5cf6", fontSize: "0.75rem", minWidth: 50 }}>
        {displayVal === 0 && field.zeroLabel
          ? field.zeroLabel
          : displayVal < 0 && field.negLabel
            ? field.negLabel
            : field.hint}
      </span>
    </div>
  );
}

function PolicyFieldRow({ field, value, onChange }: {
  field: PolicyFieldDef;
  value: number | boolean;
  onChange: (v: number | boolean) => void;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <label style={{ flex: 1, color: "#475569", fontWeight: 500, fontSize: "0.82rem" }}>{field.label}</label>
      {field.type === "boolean" ? (
        <button
          type="button"
          onClick={() => onChange(!(value as boolean))}
          style={{
            width: 44, height: 24, borderRadius: 12, border: "none", cursor: "pointer",
            background: value ? "#7c3aed" : "#cbd5e1",
            position: "relative", transition: "background 0.2s",
          }}
        >
          <span style={{
            position: "absolute", top: 2, left: value ? 22 : 2,
            width: 20, height: 20, borderRadius: 10, background: "#fff",
            transition: "left 0.2s", boxShadow: "0 1px 2px rgba(0,0,0,0.15)",
          }} />
        </button>
      ) : (
        <NumberPolicyInput
          value={value as number}
          field={field}
          onCommit={(v) => onChange(v)}
        />
      )}
    </div>
  );
}

function PolicyPanel({ systemPolicy, onCommitSystem, userPolicy, onCommitUser }: {
  systemPolicy: SystemPolicy;
  onCommitSystem: (p: Partial<SystemPolicy>) => void;
  userPolicy: UserPolicy;
  onCommitUser: (p: Partial<UserPolicy>) => void;
}) {
  return (
    <div style={{
      padding: "14px 24px", background: "#f5f3ff", borderBottom: "1px solid #ddd6fe",
      fontSize: "0.82rem",
    }}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 20px", marginBottom: 12 }}>
        <div style={{ gridColumn: "1/-1", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 2 }}>
          <span style={{ fontWeight: 700, color: "#5b21b6", fontSize: "0.82rem" }}>Merchant Rules</span>
          <span style={{ fontSize: "0.7rem", color: "#dc2626", fontWeight: 500 }}>Resets session</span>
        </div>
        {SYSTEM_POLICY_FIELDS.map((f) => (
          <PolicyFieldRow
            key={f.key}
            field={f}
            value={(systemPolicy as Record<string, number | boolean>)[f.key]}
            onChange={(v) => onCommitSystem({ [f.key]: v } as Partial<SystemPolicy>)}
          />
        ))}
      </div>

      <div style={{ borderTop: "1px solid #ddd6fe", margin: "0 -24px", padding: "0 24px" }} />

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 20px", marginTop: 12 }}>
        <div style={{ gridColumn: "1/-1", display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 2 }}>
          <span style={{ fontWeight: 700, color: "#0369a1", fontSize: "0.82rem" }}>Buyer Preferences</span>
          <span style={{ fontSize: "0.7rem", color: "#059669", fontWeight: 500 }}>Live update</span>
        </div>
        {USER_POLICY_FIELDS.map((f) => (
          <PolicyFieldRow
            key={f.key}
            field={f}
            value={(userPolicy as Record<string, number | boolean>)[f.key]}
            onChange={(v) => onCommitUser({ [f.key]: v } as Partial<UserPolicy>)}
          />
        ))}
      </div>
    </div>
  );
}

/* ========== Checkout Card ========== */

const CANCEL_REASONS = [
  { code: "price_sensitivity", label: "Too expensive" },
  { code: "product_fit", label: "Not what I need" },
  { code: "comparison", label: "Found a better option" },
  { code: "timing_deferred", label: "Not ready to buy yet" },
  { code: "payment_options", label: "Payment issue" },
  { code: "other", label: "Other reason" },
] as const;

function CheckoutCard({
  checkout: co, mode, loading, onAction, onRefund,
}: {
  checkout: ACPCheckout;
  mode: "conversational" | "ui";
  loading: boolean;
  onAction: (id: string, action: "complete" | "cancel", intentTrace?: { reason_code: string; trace_summary: string }) => void;
  onRefund: (id: string, reason: string) => void;
}) {
  const [showCancelPicker, setShowCancelPicker] = useState(false);
  const [showRefundPicker, setShowRefundPicker] = useState(false);
  const [cancelReason, setCancelReason] = useState("other");
  const [cancelNote, setCancelNote] = useState("");
  const [refundReason, setRefundReason] = useState("");
  const total = co.totals?.find((t) => t.type === "total");
  const isActionable = co.status === "ready_for_payment";
  const isCompleted = co.status === "completed";
  const isCanceled = co.status === "canceled" || co.status === "refunded";
  const isRefunded = co.status === "refunded";
  const multiItem = (co.line_items?.length ?? 0) > 1;
  const totalTokens = co._poc?.tokens;

  return (
    <div style={{
      margin: "8px 0 4px 0", padding: 16, borderRadius: 14,
      background: isCompleted ? "#f0fdf4" : isCanceled ? "#fef2f2" : "#fff",
      boxShadow: isCompleted
        ? "0 2px 8px rgba(22,163,74,0.1)"
        : isCanceled
          ? "0 2px 8px rgba(220,38,38,0.08)"
          : "0 2px 8px rgba(0,0,0,0.06)",
      maxWidth: 440, border: "none",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontWeight: 700, fontSize: "0.95rem", color: "#0f172a" }}>
          {multiItem ? `${co.line_items.length} items` : co.line_items?.[0]?.name || co._poc?.pack_label || "Checkout"}
        </span>
        <span style={{
          fontSize: "0.72rem", fontWeight: 600, padding: "3px 10px", borderRadius: 10,
          background: isCompleted ? "#dcfce7" : isActionable ? "#fef3c7" : isCanceled ? "#fecaca" : "#f1f5f9",
          color: isCompleted ? "#166534" : isActionable ? "#92400e" : isCanceled ? "#991b1b" : "#64748b",
        }}>
          {co.status}
        </span>
      </div>

      <div style={{ margin: "4px 0 10px", fontSize: "0.85rem", color: "#475569" }}>
        {co.line_items?.map((li) => {
          const qty = li.quantity ?? 1;
          const name = li.name || li.item?.name || li.id;
          const liTotal = li.totals?.find((t) => t.type === "subtotal");
          return (
            <div key={li.id} style={{ display: "flex", justifyContent: "space-between", padding: "3px 0" }}>
              <span>{qty > 1 ? `${qty}x ` : ""}{name}</span>
              {liTotal && <span style={{ color: "#64748b", fontWeight: 500 }}>{fmt(liTotal.amount, co.currency)}</span>}
            </div>
          );
        })}
      </div>

      {total && (
        <p style={{ margin: "6px 0 2px", fontSize: "1.3rem", fontWeight: 800, color: "#0f172a", letterSpacing: "-0.02em" }}>
          {fmt(total.amount, co.currency)}
        </p>
      )}
      {co.protocol && <p style={{ margin: "2px 0", fontSize: "0.7rem", color: "#94a3b8" }}>ACP {co.protocol.version}</p>}

      {isCompleted && co.order && (
        <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "#166534", fontWeight: 600 }}>
          Order {co.order.id}
        </p>
      )}

      {isCompleted && totalTokens != null && totalTokens > 0 && (
        <div style={{
          margin: "8px 0 0", padding: "8px 12px", borderRadius: 8,
          background: "#dcfce7", display: "flex", alignItems: "center", gap: 8,
        }}>
          <div style={{
            width: 28, height: 28, borderRadius: 7,
            background: "#16a34a", display: "flex", alignItems: "center", justifyContent: "center",
            color: "#fff", fontWeight: 800, fontSize: "0.78rem",
          }}>+</div>
          <div>
            <span style={{ fontSize: "0.85rem", fontWeight: 700, color: "#166534" }}>
              {totalTokens} {totalTokens === 1 ? "credit" : "credits"} added
            </span>
            {co._poc?.balance_tokens != null && (
              <span style={{ fontSize: "0.78rem", color: "#15803d", marginLeft: 8 }}>
                Balance: {fmtUsd(co._poc.balance_tokens)}
              </span>
            )}
          </div>
        </div>
      )}

      {isRefunded && (
        <p style={{ margin: "6px 0 0", fontSize: "0.82rem", color: "#991b1b", fontWeight: 600 }}>
          Refunded
        </p>
      )}

      {isCanceled && co.intent_trace && (
        <div style={{ margin: "8px 0 0", padding: "8px 10px", borderRadius: 8, background: "#fef2f2", fontSize: "0.82rem" }}>
          <span style={{ fontWeight: 600, color: "#991b1b" }}>
            {CANCEL_REASONS.find((r) => r.code === co.intent_trace?.reason_code)?.label || co.intent_trace.reason_code}
          </span>
          {co.intent_trace.trace_summary && (
            <span style={{ color: "#7f1d1d" }}> — {co.intent_trace.trace_summary}</span>
          )}
        </div>
      )}

      {mode === "ui" && isActionable && !showCancelPicker && (
        <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
          <button type="button" disabled={loading} onClick={() => onAction(co.id, "complete")} style={{
            flex: 1, padding: "11px 0", borderRadius: 10, border: "none",
            background: "linear-gradient(135deg, #15803d, #16a34a)",
            color: "#fff", fontWeight: 700, fontSize: "0.88rem", cursor: "pointer",
            boxShadow: "0 2px 6px rgba(22,163,74,0.3)",
            transition: "transform 0.1s",
          }}>Confirm & Pay</button>
          <button type="button" disabled={loading} onClick={() => setShowCancelPicker(true)} style={{
            padding: "11px 18px", borderRadius: 10, border: "1px solid #e2e8f0",
            background: "#fff", color: "#64748b", fontWeight: 600, fontSize: "0.88rem", cursor: "pointer",
          }}>Cancel</button>
        </div>
      )}

      {mode === "ui" && isCompleted && !isRefunded && !showRefundPicker && (
        <div style={{ marginTop: 12 }}>
          <button type="button" disabled={loading} onClick={() => setShowRefundPicker(true)} style={{
            padding: "9px 18px", borderRadius: 8, border: "1px solid #fca5a5",
            background: "#fff", color: "#dc2626", fontWeight: 600, fontSize: "0.82rem", cursor: "pointer",
            transition: "all 0.15s",
          }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "#fef2f2"; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "#fff"; }}
          >Request Refund</button>
        </div>
      )}

      {mode === "ui" && showRefundPicker && (
        <div style={{ marginTop: 12, padding: 14, borderRadius: 10, background: "#fef2f2", border: "1px solid #fca5a5" }}>
          <p style={{ margin: "0 0 10px", fontWeight: 600, fontSize: "0.85rem", color: "#991b1b" }}>Why do you want a refund?</p>
          <input
            style={{ width: "100%", padding: "8px 12px", borderRadius: 8, border: "1px solid #e2e8f0", fontSize: "0.82rem", boxSizing: "border-box" }}
            placeholder="Reason for refund..."
            value={refundReason} onChange={(e) => setRefundReason(e.target.value)}
            autoFocus
          />
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button type="button" disabled={loading || !refundReason.trim()} onClick={() => {
              onRefund(co.id, refundReason.trim());
              setShowRefundPicker(false);
              setRefundReason("");
            }} style={{
              flex: 1, padding: "9px 0", borderRadius: 8, border: "none",
              background: (!refundReason.trim() || loading) ? "#94a3b8" : "#dc2626",
              color: "#fff", fontWeight: 700, fontSize: "0.85rem", cursor: "pointer",
            }}>Confirm Refund</button>
            <button type="button" onClick={() => { setShowRefundPicker(false); setRefundReason(""); }} style={{
              padding: "9px 16px", borderRadius: 8, border: "1px solid #e2e8f0",
              background: "#fff", color: "#64748b", fontWeight: 600, fontSize: "0.85rem", cursor: "pointer",
            }}>Back</button>
          </div>
        </div>
      )}

      {mode === "ui" && isActionable && showCancelPicker && (
        <div style={{ marginTop: 14, padding: 14, borderRadius: 10, background: "#fff7ed", border: "1px solid #fed7aa" }}>
          <p style={{ margin: "0 0 10px", fontWeight: 600, fontSize: "0.85rem", color: "#9a3412" }}>Why are you canceling?</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 10 }}>
            {CANCEL_REASONS.map((r) => (
              <button key={r.code} type="button" onClick={() => setCancelReason(r.code)} style={{
                padding: "4px 10px", borderRadius: 6, border: "1px solid",
                borderColor: cancelReason === r.code ? "#ea580c" : "#e2e8f0",
                background: cancelReason === r.code ? "#fff7ed" : "#fff",
                color: cancelReason === r.code ? "#ea580c" : "#64748b",
                fontSize: "0.78rem", fontWeight: 600, cursor: "pointer",
              }}>{r.label}</button>
            ))}
          </div>
          <input
            style={{ width: "100%", padding: "8px 12px", borderRadius: 8, border: "1px solid #e2e8f0", fontSize: "0.82rem", boxSizing: "border-box" }}
            placeholder="Tell us more (optional)..."
            value={cancelNote} onChange={(e) => setCancelNote(e.target.value)}
          />
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <button type="button" disabled={loading} onClick={() => {
              onAction(co.id, "cancel", { reason_code: cancelReason, trace_summary: cancelNote || CANCEL_REASONS.find((r) => r.code === cancelReason)?.label || cancelReason });
              setShowCancelPicker(false);
            }} style={{
              flex: 1, padding: "9px 0", borderRadius: 8, border: "none",
              background: "#dc2626", color: "#fff", fontWeight: 700, fontSize: "0.85rem", cursor: "pointer",
            }}>Confirm Cancel</button>
            <button type="button" onClick={() => setShowCancelPicker(false)} style={{
              padding: "9px 16px", borderRadius: 8, border: "1px solid #e2e8f0",
              background: "#fff", color: "#64748b", fontWeight: 600, fontSize: "0.85rem", cursor: "pointer",
            }}>Back</button>
          </div>
        </div>
      )}

      {mode === "conversational" && isActionable && (
        <p style={{ margin: "8px 0 0", fontSize: "0.8rem", color: "#94a3b8", fontStyle: "italic" }}>
          Reply "yes" or "confirm" in chat to complete.
        </p>
      )}
    </div>
  );
}
