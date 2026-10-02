"""Batch refund for a confirmed incident (ADR-0022): pay every covered transaction, including
those of customers who never disputed.

    python -m services.core_systems.incident_refunds INC-20261001-01            # dry run: the plan
    python -m services.core_systems.incident_refunds INC-20261001-01 --execute  # pay

Safe by construction:
  - A DRY RUN by default: it prints what it would pay. Operations reviews it, then runs again with
    --execute.
  - Each refund goes through the ledger's own `execute_refund()`, with the same rules as any
    refund: the transaction must have failed, the amount must equal exactly what is owed, and a
    transaction is refunded only once, whoever asks.
  - The idempotency key is `incident:<incident>:<transaction>`, so running the job again pays
    nothing more (each refund comes back as a replay).
  - A dispute for a transaction the job already paid is recorded as paid with this refund (the
    payer looks it up), not sent to a person.
  - `--max-total` refuses to pay if the plan exceeds it: a wrong incident window cannot quietly
    pay out a fortune.
  - A pace, like the refund payer's, so the ledger is not flooded.
"""

import argparse
import asyncio
import os
from dataclasses import dataclass, field
from decimal import Decimal

from services.core_systems.ports import INCIDENT_KEY_PREFIX, IdempotencyConflict, RefundRejected
from shared.schemas import SettlementStatus


@dataclass
class BatchReport:
    incident_id: str
    executed: bool
    to_pay: list[tuple[str, str, Decimal]] = field(default_factory=list)  # (transaction, customer, amount)
    paid: list[str] = field(default_factory=list)  # refund IDs paid in this run
    already_refunded: list[str] = field(default_factory=list)  # transactions refunded before (any route)
    nothing_owed: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (transaction, reason)

    @property
    def total(self) -> Decimal:
        return sum((amount for _, _, amount in self.to_pay), Decimal("0.00"))

    def lines(self) -> list[str]:
        verb = "paid" if self.executed else "would pay (dry run)"
        out = [f"incident {self.incident_id}: {len(self.to_pay)} refund(s), {self.total} COP, {verb}"]
        out += [f"  {tx}  {customer}  {amount} COP" for tx, customer, amount in self.to_pay]
        if self.already_refunded:
            out.append(f"  already refunded, skipped: {', '.join(self.already_refunded)}")
        if self.nothing_owed:
            out.append(f"  nothing owed, skipped: {', '.join(self.nothing_owed)}")
        out += [f"  REFUSED by the ledger: {tx}: {reason}" for tx, reason in self.refused]
        return out


def incident_key(incident_id: str, transaction_id: str) -> str:
    return f"{INCIDENT_KEY_PREFIX}{incident_id}:{transaction_id}"


async def refund_incident(ledger, incident_id: str, *, execute: bool, max_total: Decimal | None = None,
                          per_second: float = 5.0) -> BatchReport:
    """Plan, and with `execute` pay, the refunds for one incident. Raises LookupError if there is
    no such incident and ValueError if the plan exceeds `max_total` (nothing is paid then)."""
    covered = await ledger.affected_transactions(incident_id)
    if covered is None:
        raise LookupError(f"no incident {incident_id}")

    report = BatchReport(incident_id=incident_id, executed=execute)
    for tx in covered:
        owed = tx.debited_amount - tx.credited_amount
        if tx.settlement_status == SettlementStatus.REVERSED:
            report.already_refunded.append(tx.transaction_id)
        elif tx.settlement_status != SettlementStatus.FAILED or owed <= 0:
            report.nothing_owed.append(tx.transaction_id)
        else:
            report.to_pay.append((tx.transaction_id, tx.customer_id, owed))

    if max_total is not None and report.total > max_total:
        raise ValueError(f"the plan pays {report.total} COP, over --max-total {max_total}: nothing was paid")
    if not execute:
        return report

    interval = 1.0 / per_second
    for transaction_id, customer_id, amount in report.to_pay:
        try:
            refund, _ = await ledger.execute_refund(customer_id, transaction_id, amount,
                                                    incident_key(incident_id, transaction_id))
            report.paid.append(refund.refund_id)
        except (RefundRejected, IdempotencyConflict) as exc:
            # For example a dispute paid it a moment ago: already_refunded. Reported, not retried.
            report.refused.append((transaction_id, str(exc)))
        await asyncio.sleep(interval)
    return report


async def _main(incident_id: str, execute: bool, max_total: Decimal | None) -> int:
    backend = os.environ.get("CORE_SYSTEMS_BACKEND", "in_memory")
    if backend != "postgres":
        print("CORE_SYSTEMS_BACKEND is not postgres: running against a throwaway in-memory ledger")
        from services.core_systems.adapters.in_memory import InMemoryLedger

        ledger, pool = InMemoryLedger(), None
    else:
        from services.core_systems.adapters.postgres import open_postgres_ledger

        ledger, pool = await open_postgres_ledger()
    try:
        report = await refund_incident(ledger, incident_id, execute=execute, max_total=max_total)
    except (LookupError, ValueError) as exc:
        print(f"STOPPED: {exc}")
        return 1
    finally:
        if pool is not None:
            await pool.close()
    print("\n".join(report.lines()))
    if not execute:
        print("Review the plan, then run again with --execute to pay.")
    return 1 if report.refused else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refund every transaction a confirmed incident covers.")
    parser.add_argument("incident_id")
    parser.add_argument("--execute", action="store_true", help="pay; without it, only print the plan")
    parser.add_argument("--max-total", type=Decimal, default=None, help="refuse to pay more than this, in COP")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.incident_id, args.execute, args.max_total)))
