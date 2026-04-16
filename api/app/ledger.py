"""SQLite ledger: demo user balances (cents) + idempotent payment records."""

import sqlite3
import threading
from pathlib import Path

_lock = threading.Lock()


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str) -> None:
    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS balances (
                    user_id TEXT PRIMARY KEY,
                    tokens INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_payments (
                    payment_intent_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    tokens INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def get_balance(path: str, user_id: str) -> int:
    with _lock:
        conn = _connect(path)
        try:
            row = conn.execute(
                "SELECT tokens FROM balances WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO balances (user_id, tokens) VALUES (?, 0)", (user_id,)
                )
                conn.commit()
                return 0
            return int(row["tokens"])
        finally:
            conn.close()


def add_tokens_idempotent(
    path: str, *, payment_intent_id: str, user_id: str, tokens: int
) -> tuple[int, bool]:
    """Returns (new_balance, was_applied)."""
    with _lock:
        conn = _connect(path)
        try:
            existing = conn.execute(
                "SELECT 1 FROM processed_payments WHERE payment_intent_id = ?",
                (payment_intent_id,),
            ).fetchone()
            if existing:
                row = conn.execute(
                    "SELECT tokens FROM balances WHERE user_id = ?", (user_id,)
                ).fetchone()
                bal = int(row["tokens"]) if row else 0
                return bal, False

            conn.execute(
                "INSERT INTO processed_payments (payment_intent_id, user_id, tokens) VALUES (?,?,?)",
                (payment_intent_id, user_id, tokens),
            )
            conn.execute(
                """
                INSERT INTO balances (user_id, tokens) VALUES (?,?)
                ON CONFLICT(user_id) DO UPDATE SET tokens = tokens + excluded.tokens
                """,
                (user_id, tokens),
            )
            row = conn.execute(
                "SELECT tokens FROM balances WHERE user_id = ?", (user_id,)
            ).fetchone()
            conn.commit()
            return int(row["tokens"]), True
        finally:
            conn.close()


def set_balance(path: str, user_id: str, amount: int) -> int:
    """Set the balance to a specific amount. Returns the new balance."""
    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                """
                INSERT INTO balances (user_id, tokens) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET tokens = excluded.tokens
                """,
                (user_id, amount),
            )
            conn.commit()
            return amount
        finally:
            conn.close()


def deduct_tokens(
    path: str, *, payment_intent_id: str, user_id: str, tokens: int
) -> tuple[int, bool]:
    """Reverse a token credit on refund. Returns (new_balance, was_applied).

    Idempotent: checks processed_payments to find the original credit,
    removes it, and subtracts the tokens. Won't go below zero.
    """
    with _lock:
        conn = _connect(path)
        try:
            existing = conn.execute(
                "SELECT tokens FROM processed_payments WHERE payment_intent_id = ?",
                (payment_intent_id,),
            ).fetchone()
            if not existing:
                row = conn.execute(
                    "SELECT tokens FROM balances WHERE user_id = ?", (user_id,)
                ).fetchone()
                bal = int(row["tokens"]) if row else 0
                return bal, False

            credited = int(existing["tokens"])
            conn.execute(
                "DELETE FROM processed_payments WHERE payment_intent_id = ?",
                (payment_intent_id,),
            )
            conn.execute(
                """
                UPDATE balances SET tokens = MAX(0, tokens - ?)
                WHERE user_id = ?
                """,
                (credited, user_id),
            )
            row = conn.execute(
                "SELECT tokens FROM balances WHERE user_id = ?", (user_id,)
            ).fetchone()
            conn.commit()
            return int(row["tokens"]), True
        finally:
            conn.close()
