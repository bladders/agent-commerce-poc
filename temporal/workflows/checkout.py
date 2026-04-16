"""CheckoutWorkflow — durable checkout-to-fulfillment lifecycle.

State machine:
  payment_pending → payment_succeeded → fulfilled → [refunded]

Each transition is a Temporal activity with its own retry policy.
If the worker crashes mid-flow, Temporal replays completed activities
from event history and re-runs only the remaining steps.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporal.shared import (
        CheckoutInput,
        CheckoutResult,
        FulfillInput,
        FulfillResult,
        PaymentInput,
        PaymentResult,
        RefundInput,
        RefundResult,
        ReverseFulfillInput,
        ReverseFulfillResult,
    )


@workflow.defn
class CheckoutWorkflow:
    """Manages the full payment lifecycle for a single checkout session."""

    def __init__(self) -> None:
        self.status = "created"
        self.payment_intent_id: str | None = None
        self.order_id: str | None = None
        self.new_balance: int | None = None
        self.refund_requested = False
        self.refund_reason: str | None = None
        self.refund_result: RefundResult | None = None

    @workflow.run
    async def run(self, input: CheckoutInput) -> CheckoutResult:
        # Step 1: Create & confirm PaymentIntent
        payment: PaymentResult = await workflow.execute_activity(
            "create_payment_intent",
            PaymentInput(
                stripe_secret_key=input.stripe_secret_key,
                amount_cents=input.total_cents,
                currency=input.currency,
                metadata=input.metadata,
                idempotency_key=f"pi_{input.checkout_session_id}",
            ),
            result_type=PaymentResult,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_attempts=3,
                non_retryable_error_types=["stripe.InvalidRequestError"],
            ),
        )
        self.payment_intent_id = payment.payment_intent_id
        self.status = "payment_pending"

        # Step 2: If not already succeeded, wait for confirmation
        if payment.status not in ("succeeded", "processing"):
            payment = await workflow.execute_activity(
                "confirm_payment",
                payment.payment_intent_id,
                result_type=PaymentResult,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        if payment.status == "succeeded":
            self.status = "payment_succeeded"

            # Step 3: Fulfill — credit ledger + Stripe Credit Grant
            fulfillment: FulfillResult = await workflow.execute_activity(
                "fulfill_payment",
                FulfillInput(
                    db_path=input.db_path,
                    user_id=input.user_id,
                    payment_intent_id=payment.payment_intent_id,
                    amount_cents=input.total_cents,
                    total_tokens=input.total_tokens,
                    currency=input.currency,
                    checkout_session_id=input.checkout_session_id,
                ),
                result_type=FulfillResult,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self.new_balance = fulfillment.new_balance
            self.order_id = f"ord_{workflow.uuid4().hex[:16]}"
            self.status = "fulfilled"

        # Step 4: Wait for optional refund signal (24h workflow timeout)
        try:
            await workflow.wait_condition(
                lambda: self.refund_requested,
                timeout=timedelta(hours=24),
            )
        except TimeoutError:
            pass

        if self.refund_requested:
            refund: RefundResult = await workflow.execute_activity(
                "process_refund",
                RefundInput(
                    stripe_secret_key=input.stripe_secret_key,
                    payment_intent_id=self.payment_intent_id,
                    checkout_session_id=input.checkout_session_id,
                    reason=self.refund_reason or "",
                ),
                result_type=RefundResult,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self.refund_result = refund

            # Saga compensation: reverse ledger + void credit grant
            reverse: ReverseFulfillResult = await workflow.execute_activity(
                "reverse_fulfillment",
                ReverseFulfillInput(
                    db_path=input.db_path,
                    user_id=input.user_id,
                    payment_intent_id=self.payment_intent_id,
                    amount_cents=input.total_cents,
                ),
                result_type=ReverseFulfillResult,
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self.new_balance = reverse.new_balance
            self.status = "refunded"

        return CheckoutResult(
            status=self.status,
            payment_intent_id=self.payment_intent_id,
            order_id=self.order_id,
            new_balance=self.new_balance,
            refund_id=self.refund_result.refund_id if self.refund_result else None,
        )

    @workflow.signal
    async def request_refund(self, reason: str) -> None:
        self.refund_reason = reason
        self.refund_requested = True

    @workflow.query
    def get_status(self) -> str:
        return self.status

    @workflow.query
    def get_payment_intent_id(self) -> str | None:
        return self.payment_intent_id

    @workflow.query
    def get_order_id(self) -> str | None:
        return self.order_id

    @workflow.query
    def get_balance(self) -> int | None:
        return self.new_balance
