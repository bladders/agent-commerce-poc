"""Temporal client singleton for the API service.

Provides a lazy-initialized async Temporal client. When TEMPORAL_ADDRESS
is not set, all operations gracefully return None so the API can fall
back to its inline (non-Temporal) payment path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from temporalio.client import Client, WorkflowHandle

log = logging.getLogger(__name__)

_client: Client | None = None
_address: str = ""

TASK_QUEUE = "checkout-workflows"


@dataclass
class CheckoutInput:
    """Mirrors temporal/shared.py — kept in sync manually.
    Duplicated here to avoid cross-container import dependency.
    """
    checkout_session_id: str
    user_id: str
    total_cents: int
    total_tokens: int
    currency: str = "usd"
    metadata: dict[str, str] = field(default_factory=dict)
    db_path: str = ""


@dataclass
class CheckoutResult:
    status: str
    payment_intent_id: str | None = None
    order_id: str | None = None
    new_balance: int | None = None
    refund_id: str | None = None


def configure(address: str) -> None:
    global _address
    _address = address


async def get_client() -> Client | None:
    """Return the Temporal client, connecting lazily. Returns None if not configured."""
    global _client
    if not _address:
        return None
    if _client is None:
        try:
            _client = await Client.connect(_address)
            log.info("Connected to Temporal at %s", _address)
        except Exception as e:
            log.warning("Failed to connect to Temporal at %s: %s", _address, e)
            return None
    return _client


async def start_checkout_workflow(
    workflow_id: str, input: Any
) -> WorkflowHandle | None:
    """Start a CheckoutWorkflow. Returns None if Temporal is unavailable."""
    client = await get_client()
    if not client:
        return None
    try:
        from temporalio.client import WorkflowHandle as _WH
        handle = await client.start_workflow(
            "CheckoutWorkflow",
            input,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        log.info("Started CheckoutWorkflow %s", workflow_id)
        return handle
    except Exception as e:
        log.warning("Failed to start CheckoutWorkflow %s: %s", workflow_id, e)
        return None


async def get_workflow_handle(workflow_id: str) -> WorkflowHandle | None:
    """Get a handle to an existing workflow. Returns None if Temporal is unavailable."""
    client = await get_client()
    if not client:
        return None
    try:
        return client.get_workflow_handle(workflow_id)
    except Exception as e:
        log.warning("Failed to get workflow handle %s: %s", workflow_id, e)
        return None


async def signal_refund(workflow_id: str, reason: str) -> bool:
    """Signal a running CheckoutWorkflow to process a refund. Returns True on success."""
    handle = await get_workflow_handle(workflow_id)
    if not handle:
        return False
    try:
        await handle.signal("request_refund", reason)
        log.info("Sent refund signal to workflow %s", workflow_id)
        return True
    except Exception as e:
        log.warning("Failed to signal refund for %s: %s", workflow_id, e)
        return False


async def query_workflow_status(workflow_id: str) -> str | None:
    """Query the current status of a CheckoutWorkflow."""
    handle = await get_workflow_handle(workflow_id)
    if not handle:
        return None
    try:
        return await handle.query("get_status")
    except Exception as e:
        log.warning("Failed to query workflow %s: %s", workflow_id, e)
        return None


async def poll_until_fulfilled(
    workflow_id: str,
    timeout: float = 30.0,
    poll_interval: float = 0.5,
) -> CheckoutResult | None:
    """Poll workflow queries until status reaches 'fulfilled' (or terminal).

    The workflow itself stays open for a 24h refund window, so we can't just
    await .result(). Instead we poll the lightweight query endpoints.
    """
    handle = await get_workflow_handle(workflow_id)
    if not handle:
        return None

    terminal = {"fulfilled", "refunded", "payment_failed"}
    elapsed = 0.0
    while elapsed < timeout:
        try:
            status = await handle.query("get_status")
        except Exception as e:
            log.warning("Query failed for %s: %s", workflow_id, e)
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            continue

        if status in terminal:
            pi_id = await handle.query("get_payment_intent_id")
            order_id = await handle.query("get_order_id")
            balance = await handle.query("get_balance")
            return CheckoutResult(
                status=status,
                payment_intent_id=pi_id,
                order_id=order_id,
                new_balance=balance,
            )

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    log.info("Workflow %s not fulfilled after %.1fs (status=%s)", workflow_id, timeout, "unknown")
    return None


async def wait_for_workflow(workflow_id: str, timeout: float = 30.0) -> CheckoutResult | None:
    """Wait for a workflow to complete, with a timeout. Returns CheckoutResult."""
    handle = await get_workflow_handle(workflow_id)
    if not handle:
        return None
    try:
        raw = await asyncio.wait_for(handle.result(), timeout=timeout)
        if isinstance(raw, CheckoutResult):
            return raw
        if isinstance(raw, dict):
            return CheckoutResult(**{
                k: v for k, v in raw.items()
                if k in CheckoutResult.__dataclass_fields__
            })
        return None
    except asyncio.TimeoutError:
        log.info("Workflow %s still running after %.1fs", workflow_id, timeout)
        return None
    except Exception as e:
        log.warning("Error waiting for workflow %s: %s", workflow_id, e)
        return None
