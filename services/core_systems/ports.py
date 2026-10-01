"""Ports: what the API needs from the systems of record, independent of how they are implemented.

Adapters implement these protocols. Today: `adapters.in_memory` (synthetic data). Later: e.g. a
PostgreSQL ledger or an HTTP client to the real core banking platform, with no change to the API.

Every read is scoped by `customer_id` *in the query itself*, so an adapter cannot accidentally
return another customer's data (defence against IDOR at the data-access layer).
Methods are async because real adapters do network I/O.
"""

from typing import Protocol

from services.core_systems.models import RefundHistory, RiskSignals, Transaction


class LedgerRepository(Protocol):
    async def get_transaction(self, customer_id: str, transaction_id: str) -> Transaction | None:
        """The customer's transaction, or None if it does not exist or belongs to someone else."""

    async def get_refund_history(self, customer_id: str, window_days: int) -> RefundHistory:
        """Automatic refunds granted to the customer within the last `window_days`."""

    async def ping(self) -> bool:
        """True if the backing system is reachable (used by the readiness probe)."""


class RiskRepository(Protocol):
    async def get_signals(self, customer_id: str, transaction_id: str) -> RiskSignals | None:
        """Risk signals for the customer's transaction, or None if not found / not theirs."""

    async def ping(self) -> bool: ...
