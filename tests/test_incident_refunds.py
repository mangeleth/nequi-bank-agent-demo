"""The incident batch refund (ADR-0022). Runs against the in-memory ledger and, under
`make test-db`, the PostgreSQL ledger: the same rules, the same results."""

import asyncio
import os
from decimal import Decimal

import pytest

from services.core_systems.incident_refunds import incident_key, refund_incident

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
INCIDENT = "INC-20261001-01"
DISPUTED = ("user-1002", "TX-20261001000009", Decimal("35000.00"))  # also disputed by its customer
SILENT = ("user-1003", "TX-20261001000010", Decimal("60000.00"))  # never disputed


@pytest.fixture(params=["in_memory", pytest.param("postgres", marks=pytest.mark.skipif(
    not DATABASE_URL, reason="needs a PostgreSQL: run `make test-db`"))])
async def ledger(request):
    if request.param == "in_memory":
        from services.core_systems.adapters.in_memory import InMemoryLedger

        yield InMemoryLedger()
        return
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from services.core_systems.adapters.postgres import PostgresLedger

    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute("DROP TABLE IF EXISTS ledger_refunds, ledger_transactions, incidents")
    pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row})
    await pool.open()
    store = PostgresLedger(pool)
    await store.migrate()
    yield store
    await pool.close()


async def test_a_dry_run_shows_the_plan_and_pays_nothing(ledger):
    report = await refund_incident(ledger, INCIDENT, execute=False)

    assert [(tx, customer) for tx, customer, _ in report.to_pay] == [
        ("TX-20261001000009", "user-1002"), ("TX-20261001000010", "user-1003")]
    assert report.total == Decimal("95000.00") and report.paid == []
    assert await ledger.get_refund("user-1003", SILENT[1]) is None  # nothing moved


async def test_execute_pays_every_covered_transaction_including_the_undisputed_one(ledger):
    report = await refund_incident(ledger, INCIDENT, execute=True, per_second=1000)

    assert len(report.paid) == 2 and report.refused == []
    silent = await ledger.get_refund("user-1003", SILENT[1])
    assert (silent.amount, silent.idempotency_key) == (SILENT[2], incident_key(INCIDENT, SILENT[1]))
    assert (await ledger.get_transaction("user-1003", SILENT[1])).settlement_status == "reversed"


async def test_running_it_again_pays_nothing_more(ledger):
    await refund_incident(ledger, INCIDENT, execute=True, per_second=1000)
    again = await refund_incident(ledger, INCIDENT, execute=True, per_second=1000)

    assert again.to_pay == [] and again.paid == []
    assert again.already_refunded == ["TX-20261001000009", "TX-20261001000010"]


async def test_a_transaction_its_customer_already_got_refunded_is_skipped(ledger):
    customer, tx, amount = DISPUTED
    await ledger.execute_refund(customer, tx, amount, "dispute:00000000-0000-0000-0000-000000000009")

    report = await refund_incident(ledger, INCIDENT, execute=True, per_second=1000)

    assert report.already_refunded == [tx] and [t for t, _, _ in report.to_pay] == [SILENT[1]]
    assert (await ledger.get_refund(customer, tx)).idempotency_key.startswith("dispute:")  # paid once, by the dispute


async def test_a_dispute_and_the_batch_at_the_same_moment_pay_once(ledger):
    """Your question: both try the same transaction at once. Different keys, so the key cannot
    help; the ledger's own state does (a locked row and one refund per transaction)."""
    customer, tx, amount = DISPUTED
    dispute_pays = ledger.execute_refund(customer, tx, amount, "dispute:00000000-0000-0000-0000-000000000009")
    batch_pays = refund_incident(ledger, INCIDENT, execute=True, per_second=1000)

    dispute_result, report = await asyncio.gather(dispute_pays, batch_pays, return_exceptions=True)

    refund = await ledger.get_refund(customer, tx)  # the one refund for this transaction
    assert refund is not None and refund.amount == amount
    if refund.idempotency_key.startswith("dispute:"):  # the dispute won the race
        assert tx not in [t for t, _, _ in report.to_pay] or tx in [t for t, _ in report.refused]
    else:  # the batch won: the dispute's request was refused as already refunded
        assert refund.idempotency_key == incident_key(INCIDENT, tx)
        assert getattr(dispute_result, "code", None) == "already_refunded"


async def test_incident_refunds_do_not_count_towards_the_customers_limits(ledger):
    before = await ledger.get_refund_history("user-1003", 30)
    await refund_incident(ledger, INCIDENT, execute=True, per_second=1000)
    after = await ledger.get_refund_history("user-1003", 30)

    assert after == before  # the customer did not claim it: their next dispute is judged as before


async def test_a_plan_over_the_cap_pays_nothing(ledger):
    with pytest.raises(ValueError, match="over --max-total"):
        await refund_incident(ledger, INCIDENT, execute=True, max_total=Decimal("50000.00"))
    assert await ledger.get_refund("user-1003", SILENT[1]) is None


async def test_an_unknown_incident_is_refused(ledger):
    with pytest.raises(LookupError):
        await refund_incident(ledger, "INC-20991231-99", execute=True)
