"""The durable record of every dispute (ADR-0016).

A dispute outlives any single run of the graph, so it is a stored record with two statuses:
  - execution status: what happened to the run (queued, running, finished, failed)
  - business status: where the customer's dispute stands (received, investigating, ...)

`DisputeStore` is the port. PostgreSQL is the adapter used in the cluster; the in-memory adapter
is for tests and single-process local runs. Both must behave the same (tests/test_dispute_store.py).

Rules the store enforces, whatever the caller does:
  - one dispute per key (unique constraint), so a duplicate that slips past Redis still cannot
    create a second dispute
  - every read is scoped by customer
  - a status changes only from the status it is expected to be in
  - every change is appended to `dispute_events`, the audit trail
"""

import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

from shared.schemas import DisputeRequest, DisputeStatus, DisputeView, ExecutionStatus, TriageResult

_ACTIVE = (ExecutionStatus.QUEUED.value, ExecutionStatus.RUNNING.value)


@dataclass(frozen=True)
class DisputeRecord:
    dispute_id: UUID
    dispute_key: str
    user_id: str
    transaction_id: str
    request: dict
    execution_status: ExecutionStatus
    business_status: DisputeStatus
    customer_message: str
    result: dict | None
    attempts: int
    created_at: datetime
    updated_at: datetime

    def view(self) -> DisputeView:
        return DisputeView(
            dispute_id=self.dispute_id,
            transaction_id=self.transaction_id,
            status=self.business_status,
            execution_status=self.execution_status,
            customer_message=self.customer_message,
            result=TriageResult.model_validate(self.result) if self.result else None,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class DisputeStore(Protocol):
    async def create(self, *, dispute_id: UUID, dispute_key: str, user_id: str, request: DisputeRequest,
                     customer_message: str) -> tuple[DisputeRecord, bool]:
        """Insert a new dispute, or return the existing one for this key. Second value: created?"""

    async def get(self, dispute_id: UUID, user_id: str) -> DisputeRecord | None:
        """The customer's dispute, or None if it does not exist or belongs to someone else."""

    async def load(self, dispute_id: UUID) -> DisputeRecord | None:
        """The dispute by ID, for the worker. Not scoped to a customer: never call from the API."""

    async def start(self, dispute_id: UUID, customer_message: str, *, takeover: bool = False,
                    max_attempts: int = 1_000) -> bool:
        """queued -> running, received -> investigating. With `takeover`, a dispute still marked
        running (its worker died) may be started again. False once `max_attempts` is reached."""

    async def finish(self, dispute_id: UUID, result: TriageResult) -> bool:
        """running -> finished, with the business status and message from the result."""

    async def settle(self, dispute_id: UUID, *, expected: DisputeStatus, result: TriageResult, note: str) -> bool:
        """After a finished run: move the business status on from `expected` (e.g. refund_approved
        -> refund_paid), with the new result. False if the dispute is not finished or no longer
        in `expected`, so two workers cannot both settle it."""

    async def fail(self, dispute_id: UUID, customer_message: str, note: str) -> bool:
        """queued/running -> failed; the dispute itself goes to a person."""

    async def fail_stuck(self, older_than_seconds: int, customer_message: str) -> int:
        """Fail every run that has been queued or running for too long (its process died)."""

    async def events(self, dispute_id: UUID) -> list[dict]: ...

    async def ping(self) -> bool: ...


# --- In-memory adapter ---------------------------------------------------------------------------


class InMemoryDisputeStore:
    """Single-process store for tests and local runs. Not durable, not shared."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, DisputeRecord] = {}
        self._events: dict[UUID, list[dict]] = {}

    def _log(self, record: DisputeRecord, note: str) -> None:
        self._events.setdefault(record.dispute_id, []).append({
            "at": record.updated_at, "execution_status": record.execution_status.value,
            "business_status": record.business_status.value, "note": note,
        })

    def _change(self, dispute_id: UUID, expected: tuple[str, ...], note: str, **changes) -> bool:
        record = self._by_id.get(dispute_id)
        if record is None or record.execution_status.value not in expected:
            return False
        record = replace(record, updated_at=datetime.now(UTC), **changes)
        self._by_id[dispute_id] = record
        self._log(record, note)
        return True

    async def create(self, *, dispute_id, dispute_key, user_id, request, customer_message):
        for existing in self._by_id.values():
            if existing.dispute_key == dispute_key:
                return existing, False
        now = datetime.now(UTC)
        record = DisputeRecord(
            dispute_id=dispute_id, dispute_key=dispute_key, user_id=user_id,
            transaction_id=request.transaction_id, request=request.model_dump(mode="json"),
            execution_status=ExecutionStatus.QUEUED, business_status=DisputeStatus.RECEIVED,
            customer_message=customer_message, result=None, attempts=0, created_at=now, updated_at=now,
        )
        self._by_id[dispute_id] = record
        self._log(record, "dispute received")
        return record, True

    async def get(self, dispute_id, user_id):
        record = self._by_id.get(dispute_id)
        return record if record is not None and record.user_id == user_id else None

    async def load(self, dispute_id):
        return self._by_id.get(dispute_id)

    async def start(self, dispute_id, customer_message, *, takeover=False, max_attempts=1_000):
        record = self._by_id.get(dispute_id)
        if record is None or record.attempts >= max_attempts:
            return False
        restarting = record.execution_status == ExecutionStatus.RUNNING
        return self._change(
            dispute_id, _ACTIVE if takeover else (ExecutionStatus.QUEUED.value,),
            "run restarted after a failed delivery" if restarting else "run started",
            execution_status=ExecutionStatus.RUNNING, business_status=DisputeStatus.INVESTIGATING,
            customer_message=customer_message, attempts=record.attempts + 1,
        )

    async def finish(self, dispute_id, result):
        return self._change(
            dispute_id, (ExecutionStatus.RUNNING.value,), "run finished",
            execution_status=ExecutionStatus.FINISHED, business_status=result.status,
            customer_message=result.customer_message, result=result.model_dump(mode="json"),
        )

    async def settle(self, dispute_id, *, expected, result, note):
        record = self._by_id.get(dispute_id)
        if record is None or record.business_status != expected:
            return False
        return self._change(
            dispute_id, (ExecutionStatus.FINISHED.value,), note, business_status=result.status,
            customer_message=result.customer_message, result=result.model_dump(mode="json"),
        )

    async def fail(self, dispute_id, customer_message, note):
        return self._change(
            dispute_id, _ACTIVE, note, execution_status=ExecutionStatus.FAILED,
            business_status=DisputeStatus.PENDING_HUMAN_APPROVAL, customer_message=customer_message,
        )

    async def fail_stuck(self, older_than_seconds, customer_message):
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        stuck = [r.dispute_id for r in self._by_id.values()
                 if r.execution_status.value in _ACTIVE and r.updated_at < cutoff]
        for dispute_id in stuck:
            await self.fail(dispute_id, customer_message, "run did not finish in time")
        return len(stuck)

    async def events(self, dispute_id):
        return list(self._events.get(dispute_id, []))

    async def ping(self):
        return True


# --- PostgreSQL adapter --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id        uuid PRIMARY KEY,
    dispute_key       text NOT NULL UNIQUE,   -- sha256(user_id, transaction_id): one dispute per key
    user_id           text NOT NULL,
    transaction_id    text NOT NULL,
    request           jsonb NOT NULL,
    execution_status  text NOT NULL CHECK (execution_status IN ('queued', 'running', 'finished', 'failed')),
    business_status   text NOT NULL,
    customer_message  text NOT NULL,
    result            jsonb,
    attempts          integer NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- The human review queue is one query: WHERE business_status = 'pending_human_approval' ORDER BY created_at
CREATE INDEX IF NOT EXISTS disputes_by_status ON disputes (business_status, created_at);
CREATE INDEX IF NOT EXISTS disputes_active ON disputes (updated_at) WHERE execution_status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS dispute_events (   -- append-only audit trail of every status change
    event_id          bigserial PRIMARY KEY,
    dispute_id        uuid NOT NULL REFERENCES disputes (dispute_id),
    at                timestamptz NOT NULL DEFAULT now(),
    execution_status  text NOT NULL,
    business_status   text NOT NULL,
    note              text NOT NULL
);
"""

_COLUMNS = ("dispute_id, dispute_key, user_id, transaction_id, request, execution_status, business_status, "
            "customer_message, result, attempts, created_at, updated_at")
_LOG_EVENT = ("INSERT INTO dispute_events (dispute_id, execution_status, business_status, note) "
              "SELECT dispute_id, execution_status, business_status, %(note)s FROM changed")


def _record(row: dict) -> DisputeRecord:
    return DisputeRecord(**(row | {"execution_status": ExecutionStatus(row["execution_status"]),
                                   "business_status": DisputeStatus(row["business_status"])}))


class PostgresDisputeStore:
    def __init__(self, pool) -> None:
        self._pool = pool  # psycopg_pool.AsyncConnectionPool with dict rows

    async def migrate(self) -> None:
        """Create the tables if they do not exist. Safe when several replicas start together."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(727001)")
            await conn.execute(SCHEMA)

    async def _change(self, dispute_id: UUID, expected: tuple[str, ...], note: str, assignments: str,
                      also: str = "", **params) -> bool:
        """Update one dispute only if it is in an expected status, and log the change, atomically."""
        sql = (f"WITH changed AS (UPDATE disputes SET {assignments}, updated_at = now() "
               f"WHERE dispute_id = %(dispute_id)s AND execution_status = ANY(%(expected)s) {also} RETURNING *) "
               f"{_LOG_EVENT} RETURNING 1")
        async with self._pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(sql, {"dispute_id": dispute_id, "expected": list(expected), "note": note} | params)
            return cursor.rowcount == 1

    async def create(self, *, dispute_id, dispute_key, user_id, request, customer_message):
        from psycopg.types.json import Jsonb

        params = {"dispute_id": dispute_id, "dispute_key": dispute_key, "user_id": user_id,
                  "transaction_id": request.transaction_id, "request": Jsonb(request.model_dump(mode="json")),
                  "customer_message": customer_message, "note": "dispute received"}
        insert = (
            "WITH changed AS (INSERT INTO disputes (dispute_id, dispute_key, user_id, transaction_id, request, "
            "execution_status, business_status, customer_message) VALUES (%(dispute_id)s, %(dispute_key)s, "
            "%(user_id)s, %(transaction_id)s, %(request)s, 'queued', 'received', %(customer_message)s) "
            f"ON CONFLICT (dispute_key) DO NOTHING RETURNING *), logged AS ({_LOG_EVENT}) "
            f"SELECT {_COLUMNS} FROM changed"
        )
        async with self._pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(insert, params)).fetchone()
            if row is not None:
                return _record(row), True
            # The unique constraint held: this key already has a dispute.
            existing = await conn.execute(f"SELECT {_COLUMNS} FROM disputes WHERE dispute_key = %s", (dispute_key,))
            return _record(await existing.fetchone()), False

    async def get(self, dispute_id, user_id):
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT {_COLUMNS} FROM disputes WHERE dispute_id = %s AND user_id = %s", (dispute_id, user_id))
            row = await cursor.fetchone()
        return _record(row) if row else None

    async def load(self, dispute_id):
        async with self._pool.connection() as conn:
            cursor = await conn.execute(f"SELECT {_COLUMNS} FROM disputes WHERE dispute_id = %s", (dispute_id,))
            row = await cursor.fetchone()
        return _record(row) if row else None

    async def start(self, dispute_id, customer_message, *, takeover=False, max_attempts=1_000):
        return await self._change(
            dispute_id, _ACTIVE if takeover else (ExecutionStatus.QUEUED.value,),
            "run restarted after a failed delivery" if takeover else "run started",
            "execution_status = 'running', business_status = 'investigating', "
            "customer_message = %(customer_message)s, attempts = attempts + 1",
            also="AND attempts < %(max_attempts)s",
            customer_message=customer_message, max_attempts=max_attempts)

    async def finish(self, dispute_id, result):
        from psycopg.types.json import Jsonb

        return await self._change(
            dispute_id, (ExecutionStatus.RUNNING.value,), "run finished",
            "execution_status = 'finished', business_status = %(business_status)s, "
            "customer_message = %(customer_message)s, result = %(result)s",
            business_status=result.status.value, customer_message=result.customer_message,
            result=Jsonb(result.model_dump(mode="json")))

    async def settle(self, dispute_id, *, expected, result, note):
        from psycopg.types.json import Jsonb

        return await self._change(
            dispute_id, (ExecutionStatus.FINISHED.value,), note,
            "business_status = %(business_status)s, customer_message = %(customer_message)s, result = %(result)s",
            also="AND business_status = %(expected_business)s",
            business_status=result.status.value, customer_message=result.customer_message,
            result=Jsonb(result.model_dump(mode="json")), expected_business=expected.value)

    async def fail(self, dispute_id, customer_message, note):
        return await self._change(
            dispute_id, _ACTIVE, note,
            "execution_status = 'failed', business_status = 'pending_human_approval', "
            "customer_message = %(customer_message)s", customer_message=customer_message)

    async def fail_stuck(self, older_than_seconds, customer_message):
        sql = ("WITH changed AS (UPDATE disputes SET execution_status = 'failed', "
               "business_status = 'pending_human_approval', customer_message = %(customer_message)s, updated_at = now() "
               "WHERE execution_status IN ('queued', 'running') "
               "AND updated_at < now() - make_interval(secs => %(seconds)s) RETURNING *) "
               f"{_LOG_EVENT} RETURNING 1")
        async with self._pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(sql, {"customer_message": customer_message, "seconds": older_than_seconds,
                                              "note": "run did not finish in time"})
            return cursor.rowcount

    async def events(self, dispute_id):
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT at, execution_status, business_status, note FROM dispute_events "
                "WHERE dispute_id = %s ORDER BY event_id", (dispute_id,))
            return await cursor.fetchall()

    async def ping(self):
        try:
            async with self._pool.connection(timeout=2) as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False


def postgres_conninfo() -> str:
    """Connection settings from PGHOST, PGDATABASE, PGUSER, and a password file mounted from Key Vault."""
    from psycopg.conninfo import make_conninfo

    if url := os.environ.get("DATABASE_URL", "").strip():
        return url
    password_file = os.environ.get("POSTGRES_PASSWORD_FILE", "").strip()
    return make_conninfo(
        host=os.environ["PGHOST"], dbname=os.environ["PGDATABASE"], user=os.environ["PGUSER"],
        password=Path(password_file).read_text().strip() if password_file else os.environ.get("PGPASSWORD", ""),
        connect_timeout=5,
    )


async def open_store() -> tuple[DisputeStore, object | None]:
    """Choose the store from DISPUTE_STORE: `postgres` or `memory`. Returns (store, pool to close)."""
    backend = os.environ.get("DISPUTE_STORE", "memory").strip()
    if backend == "memory":
        return InMemoryDisputeStore(), None
    if backend == "postgres":
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(postgres_conninfo(), min_size=1, max_size=5, open=False,
                                   kwargs={"row_factory": dict_row})
        await pool.open(wait=True, timeout=30)
        store = PostgresDisputeStore(pool)
        await store.migrate()
        return store, pool
    raise ValueError(f"unknown DISPUTE_STORE={backend!r} (supported: postgres, memory)")


def as_json(record: DisputeRecord) -> str:
    return json.dumps(record.view().model_dump(mode="json"))
