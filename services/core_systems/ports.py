"""Ports: what the API needs from the systems of record, independent of how they are implemented.

Adapters implement these protocols. Today: `adapters.in_memory` (synthetic data). Later: e.g. a
PostgreSQL ledger or an HTTP client to the real core banking platform, with no change to the API.

Every read and write is scoped by `customer_id` *in the query itself*, so an adapter cannot accidentally
return another customer's data (defence against IDOR at the data-access layer).
Methods are async because real adapters do network I/O.
"""

from decimal import Decimal
from typing import Protocol

from services.core_systems.models import Incident, Refund, RefundHistory, RiskSignals, Transaction


class RefundRejected(Exception):
    """The ledger's own rules refuse this refund. `code` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different refund."""


# Refunds paid by an incident's batch job (ADR-0022) carry keys with this prefix. They were not
# claimed by the customer, so they do not count towards the customer's automatic-refund limits.
INCIDENT_KEY_PREFIX = "incident:"


class LedgerRepository(Protocol):
    async def get_transaction(self, customer_id: str, transaction_id: str) -> Transaction | None:
        """The customer's transaction, or None if it does not exist or belongs to someone else."""

    async def get_refund_history(self, customer_id: str, window_days: int) -> RefundHistory:
        """Automatic refunds granted to the customer within the last `window_days`. Incident
        refunds (INCIDENT_KEY_PREFIX) are not counted: the customer did not claim them."""

    async def get_refund(self, customer_id: str, transaction_id: str) -> Refund | None:
        """The refund already paid for this customer's transaction, or None."""

    async def execute_refund(
        self, customer_id: str, transaction_id: str, amount: Decimal, idempotency_key: str
    ) -> tuple[Refund, bool]:
        """Pay a refund, exactly once. Returns the refund and whether it was a replay.

        - The same key again returns the refund already made; no money moves (replay = True).
        - The same key with a different transaction or amount raises IdempotencyConflict.
        - The ledger applies its own rules whatever the caller decided (RefundRejected):
          the transaction must be the customer's, must have failed, the amount must equal what
          was debited and never credited, and a transaction can be refunded only once.
        """

    async def ping(self) -> bool:
        """True if the backing system is reachable (used by the readiness probe)."""


class IncidentRegistry(Protocol):
    """Incidents that operations has confirmed (ADR-0022). Matched against the ledger's own
    transactions, so in our adapters the ledger implements it too."""

    async def incident_for(self, customer_id: str, transaction_id: str) -> Incident | None:
        """The confirmed incident covering this customer's transaction, or None."""

    async def affected_transactions(self, incident_id: str) -> list[Transaction] | None:
        """Every transaction the incident covers, for the batch refund job. None if no such incident."""


class RiskRepository(Protocol):
    async def get_signals(self, customer_id: str, transaction_id: str) -> RiskSignals | None:
        """Risk signals for the customer's transaction, or None if not found / not theirs."""

    async def ping(self) -> bool: ...
