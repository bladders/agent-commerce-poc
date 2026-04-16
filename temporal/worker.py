"""Temporal worker — registers checkout workflow and activities."""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from temporal.activities.fulfillment import fulfill_payment
from temporal.activities.payment import confirm_payment, create_payment_intent
from temporal.activities.refund import process_refund, reverse_fulfillment
from temporal.shared import TASK_QUEUE
from temporal.workflows.checkout import CheckoutWorkflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


async def main() -> None:
    temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    log.info("Connecting to Temporal at %s", temporal_address)

    client = None
    for attempt in range(1, 21):
        try:
            client = await Client.connect(temporal_address)
            break
        except Exception as e:
            log.warning("Attempt %d — Temporal not ready: %s", attempt, e)
            await asyncio.sleep(3)
    if client is None:
        raise RuntimeError(f"Could not connect to Temporal at {temporal_address} after 20 attempts")

    log.info("Connected. Starting worker on queue %r", TASK_QUEUE)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CheckoutWorkflow],
        activities=[
            create_payment_intent,
            confirm_payment,
            fulfill_payment,
            process_refund,
            reverse_fulfillment,
        ],
    )

    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
