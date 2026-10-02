"""PostgreSQL ledger adapter: one shared ledger for every Core Systems replica (ADR-0019).

The in-memory ledger lives in one process, so with two replicas a refund paid by one is unknown
to the other, and both the idempotency key and the "already refunded" check stop working. Here
the truth is in the database, and the database enforces the rules itself:

  - `idempotency_key` is UNIQUE: the same request can be recorded only once
  - `transaction_id` is UNIQUE in the refunds table: a transaction can be refunded only once,
    whatever key is used
  - the refund row and the change to the transaction are written in ONE database transaction:
    both happen or neither does

The synthetic fixtures are loaded once, the first time the tables are empty.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from services.core_systems.adapters.fixtures import AUTO_REFUNDS, INCIDENTS, TRANSACTIONS
from services.core_systems.models import Incident, Refund, RefundHistory, Transaction
from services.core_systems.ports import IdempotencyConflict, RefundRejected
from shared.schemas import SettlementStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_transactions (
    transaction_id     text PRIMARY KEY,
    customer_id        text NOT NULL,
    recipient_account  text NOT NULL,
    amount             numeric(15,2) NOT NULL,
    currency           text NOT NULL,
    created_at         timestamptz NOT NULL,
    settlement_status  text NOT NULL CHECK (settlement_status IN ('settled', 'pending', 'failed', 'reversed')),
    debited_amount     numeric(15,2) NOT NULL CHECK (debited_amount >= 0),
    credited_amount    numeric(15,2) NOT NULL CHECK (credited_amount >= 0)
);
-- Added in Step 12 (ADR-0022); ADD COLUMN IF NOT EXISTS upgrades a ledger created before it.
ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS recipient_bank text NOT NULL DEFAULT 'NEQUI';
ALTER TABLE ledger_transactions ADD COLUMN IF NOT EXISTS failure_code text;

CREATE TABLE IF NOT EXISTS incidents (   -- confirmed by operations, once per incident
    incident_id     text PRIMARY KEY,
    title           text NOT NULL,
    failure_code    text NOT NULL,
    recipient_bank  text NOT NULL,
    window_start    timestamptz NOT NULL,
    window_end      timestamptz NOT NULL CHECK (window_end > window_start),
    confirmed_by    text NOT NULL,
    confirmed_at    timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_refunds (
    refund_id        text PRIMARY KEY,
    idempotency_key  text NOT NULL UNIQUE,                                  -- one record per request
    transaction_id   text UNIQUE REFERENCES ledger_transactions (transaction_id),  -- one refund per transaction
    customer_id      text NOT NULL,
    amount           numeric(15,2) NOT NULL CHECK (amount > 0),
    currency         text NOT NULL,
    executed_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_refunds_by_customer ON ledger_refunds (customer_id, executed_at);
"""

_TX_COLUMNS = ("transaction_id, customer_id, recipient_account, recipient_bank, amount, currency, created_at, "
               "settlement_status, debited_amount, credited_amount, failure_code")
_INCIDENT_COLUMNS = ("incident_id, title, failure_code, recipient_bank, window_start, window_end, confirmed_by, "
                     "confirmed_at")
# The incident rule (ADR-0022), the same as `covers()` in the in-memory adapter.
_COVERS = ("t.failure_code = i.failure_code AND t.recipient_bank = i.recipient_bank "
           "AND t.created_at >= i.window_start AND t.created_at < i.window_end")
_REFUND_COLUMNS = "refund_id, transaction_id, customer_id, amount, currency, executed_at, idempotency_key"


class PostgresLedger:
    def __init__(self, pool) -> None:
        self._pool = pool  # psycopg_pool.AsyncConnectionPool with dict rows

    async def migrate(self) -> None:
        """Create the tables and load the fixtures if empty. Safe when replicas start together."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(727002)")
            await conn.execute(SCHEMA)
            await self._load_fixtures(conn)  # adds what is missing; never changes existing rows

    async def reset(self) -> None:
        """Demo only: put the ledger back to the synthetic starting data (`make demo-reset`)."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(727002)")
            await conn.execute(SCHEMA)
            await conn.execute("TRUNCATE ledger_refunds, ledger_transactions, incidents")
            await self._load_fixtures(conn)

    @staticmethod
    async def _load_fixtures(conn) -> None:
        """Insert the synthetic data that is missing. An existing transaction keeps its state (a
        refunded one stays refunded); only the two columns added in Step 12 are backfilled."""
        now = datetime.now(UTC)
        for tx in TRANSACTIONS.values():
            await conn.execute(
                f"INSERT INTO ledger_transactions ({_TX_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (transaction_id) DO UPDATE SET recipient_bank = EXCLUDED.recipient_bank, "
                "failure_code = EXCLUDED.failure_code "
                "WHERE ledger_transactions.failure_code IS DISTINCT FROM EXCLUDED.failure_code",  # backfill only
                (tx["transaction_id"], tx["customer_id"], tx["recipient_account"], tx["recipient_bank"], tx["amount"],
                 tx["currency"], tx["created_at"], tx["settlement_status"].value, tx["debited_amount"],
                 tx["credited_amount"], tx["failure_code"]))
        for incident in INCIDENTS.values():
            await conn.execute(
                f"INSERT INTO incidents ({_INCIDENT_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (incident_id) DO NOTHING",
                tuple(incident[column.strip()] for column in _INCIDENT_COLUMNS.split(",")))
        for customer, past in AUTO_REFUNDS.items():  # refunds from before this demo; no transaction attached
            for index, (days_ago, amount) in enumerate(past):
                await conn.execute(
                    "INSERT INTO ledger_refunds (refund_id, idempotency_key, customer_id, amount, currency, executed_at) "
                    "VALUES (%s, %s, %s, %s, 'COP', %s) ON CONFLICT (refund_id) DO NOTHING",
                    (f"RF-seed-{customer}-{index}", f"seed:{customer}:{index}", customer, amount,
                     now - timedelta(days=days_ago)))

    async def get_transaction(self, customer_id: str, transaction_id: str) -> Transaction | None:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {_TX_COLUMNS} FROM ledger_transactions WHERE transaction_id = %s AND customer_id = %s",
                (transaction_id, customer_id))
            row = await cursor.fetchone()
        return Transaction(**row) if row else None

    async def get_refund_history(self, customer_id: str, window_days: int) -> RefundHistory:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) AS count, coalesce(sum(amount), 0) AS total FROM ledger_refunds "
                "WHERE customer_id = %s AND executed_at >= now() - make_interval(days => %s)",
                (customer_id, window_days))
            row = await cursor.fetchone()
        return RefundHistory(customer_id=customer_id, window_days=window_days,
                             auto_refund_count=row["count"], auto_refund_total=Decimal(row["total"]).quantize(Decimal("0.01")))

    async def incident_for(self, customer_id: str, transaction_id: str) -> Incident | None:
        columns = ", ".join(f"i.{c.strip()}" for c in _INCIDENT_COLUMNS.split(","))
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {columns} FROM ledger_transactions t JOIN incidents i ON {_COVERS} "
                "WHERE t.transaction_id = %s AND t.customer_id = %s ORDER BY i.incident_id LIMIT 1",
                (transaction_id, customer_id))
            row = await cursor.fetchone()
        return Incident(**row) if row else None

    async def affected_transactions(self, incident_id: str) -> list[Transaction] | None:
        columns = ", ".join(f"t.{c.strip()}" for c in _TX_COLUMNS.split(","))
        async with self._pool.connection() as conn:
            if await (await conn.execute("SELECT 1 FROM incidents WHERE incident_id = %s", (incident_id,))).fetchone() is None:
                return None
            cursor = await conn.execute(
                f"SELECT {columns} FROM incidents i JOIN ledger_transactions t ON {_COVERS} "
                "WHERE i.incident_id = %s ORDER BY t.transaction_id", (incident_id,))
            return [Transaction(**row) for row in await cursor.fetchall()]

    async def execute_refund(
        self, customer_id: str, transaction_id: str, amount: Decimal, idempotency_key: str
    ) -> tuple[Refund, bool]:
        async with self._pool.connection() as conn, conn.transaction():
            # Requests with the same key go one at a time, on any replica (released at commit).
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (idempotency_key,))

            # 1. Seen this key before? Then nothing moves: return what was done the first time.
            cursor = await conn.execute(
                f"SELECT {_REFUND_COLUMNS} FROM ledger_refunds WHERE idempotency_key = %s", (idempotency_key,))
            previous = await cursor.fetchone()
            if previous is not None:
                same_request = (previous["customer_id"], previous["transaction_id"], previous["amount"]) == (
                    customer_id, transaction_id, amount)
                if not same_request:
                    raise IdempotencyConflict("this idempotency key was used for a different refund")
                return Refund(**previous), True

            # 2. The ledger's own rules, on a locked row: no other request can change this
            #    transaction until we commit.
            cursor = await conn.execute(
                f"SELECT {_TX_COLUMNS} FROM ledger_transactions WHERE transaction_id = %s AND customer_id = %s FOR UPDATE",
                (transaction_id, customer_id))
            tx = await cursor.fetchone()
            if tx is None:
                raise RefundRejected("transaction_not_found", "transaction not found")
            if tx["settlement_status"] == SettlementStatus.REVERSED.value:
                raise RefundRejected("already_refunded", "this transaction was already refunded")
            if tx["settlement_status"] != SettlementStatus.FAILED.value:
                raise RefundRejected("not_refundable", f"a {tx['settlement_status']} transaction cannot be refunded")
            owed = tx["debited_amount"] - tx["credited_amount"]
            if amount != owed:
                raise RefundRejected("amount_mismatch", f"the ledger shows {owed} owed, not {amount}")

            # 3. Move the money and record it, in the same database transaction.
            cursor = await conn.execute(
                "INSERT INTO ledger_refunds (refund_id, idempotency_key, transaction_id, customer_id, amount, currency) "
                f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_REFUND_COLUMNS}",
                (f"RF-{uuid.uuid4().hex[:16]}", idempotency_key, transaction_id, customer_id, amount, tx["currency"]))
            refund = await cursor.fetchone()
            await conn.execute(
                "UPDATE ledger_transactions SET settlement_status = 'reversed', credited_amount = debited_amount "
                "WHERE transaction_id = %s", (transaction_id,))
            return Refund(**refund), False

    async def ping(self) -> bool:
        try:
            async with self._pool.connection(timeout=2) as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False


def ledger_conninfo() -> str:
    """Connection settings: LEDGER_DATABASE_URL, or LEDGER_PG* with a password file from Key Vault."""
    from psycopg.conninfo import make_conninfo

    if url := os.environ.get("LEDGER_DATABASE_URL", "").strip():
        return url
    password_file = os.environ.get("LEDGER_PASSWORD_FILE", "").strip()
    return make_conninfo(
        host=os.environ["LEDGER_PGHOST"], dbname=os.environ["LEDGER_PGDATABASE"], user=os.environ["LEDGER_PGUSER"],
        password=Path(password_file).read_text().strip() if password_file else "", connect_timeout=5,
    )


async def open_postgres_ledger() -> tuple[PostgresLedger, object]:
    """Open the pool, create the tables, load the fixtures if empty. Returns (ledger, pool to close)."""
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(ledger_conninfo(), min_size=1, max_size=5, open=False, kwargs={"row_factory": dict_row})
    await pool.open(wait=True, timeout=30)
    ledger = PostgresLedger(pool)
    await ledger.migrate()
    return ledger, pool


if __name__ == "__main__":  # python -m services.core_systems.adapters.postgres reset
    import asyncio
    import sys

    async def _reset() -> None:
        ledger, pool = await open_postgres_ledger()
        await ledger.reset()
        await pool.close()

    if sys.argv[1:] != ["reset"]:
        sys.exit("usage: python -m services.core_systems.adapters.postgres reset")
    asyncio.run(_reset())
    print("ledger reset to the synthetic starting data")
