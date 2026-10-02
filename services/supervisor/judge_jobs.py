"""Jobs for the LLM judge (ADR-0026): which finished disputes still need their explanation graded.

    triage worker   a run finishes with a model-written explanation -> enqueue(dispute_id)
    judge worker    receive() -> grade -> save the verdict -> ack(job)

A Redis Stream with a consumer group. A job a worker has received stays PENDING until it is
acknowledged; if the worker dies, another one reclaims it after `RECLAIM_IDLE_MS` (the same idea
as the dispute queue's lock, ADR-0018). After `MAX_ATTEMPTS` deliveries the job is given up, and the
worker records that the dispute could not be judged.

Enqueueing never affects the dispute: if Redis is down the triage still finishes, and the
dispute simply has no judgement.
"""

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

STREAM, GROUP = "judge-jobs", "judges"
MAX_ATTEMPTS = 3
RECLAIM_IDLE_MS = 60_000
MAX_STREAM_LENGTH = 10_000  # old, acknowledged entries are trimmed


@dataclass(frozen=True)
class JudgeJob:
    dispute_id: UUID
    job_id: str
    attempt: int  # 1 the first time

    @property
    def is_last(self) -> bool:
        return self.attempt >= MAX_ATTEMPTS


class JudgeJobs(Protocol):
    async def enqueue(self, dispute_id: UUID) -> None: ...

    def receive(self) -> AsyncIterator[JudgeJob]: ...

    async def ack(self, job: JudgeJob) -> None: ...

    async def ping(self) -> bool: ...


class InMemoryJudgeJobs:
    """Single-process jobs for tests and local runs, with the same retry rule."""

    def __init__(self) -> None:
        self._ready: asyncio.Queue[tuple[UUID, int]] = asyncio.Queue()
        self._pending: dict[str, JudgeJob] = {}
        self._next = 0

    async def enqueue(self, dispute_id: UUID) -> None:
        await self._ready.put((dispute_id, 1))

    async def receive(self) -> AsyncIterator[JudgeJob]:
        while True:
            dispute_id, attempt = await self._ready.get()
            self._next += 1
            job = JudgeJob(dispute_id=dispute_id, job_id=str(self._next), attempt=attempt)
            self._pending[job.job_id] = job
            yield job

    async def ack(self, job: JudgeJob) -> None:
        self._pending.pop(job.job_id, None)

    async def reclaim(self) -> None:
        """What Redis does for a worker that died: offer its unacknowledged jobs again."""
        for job in list(self._pending.values()):
            self._pending.pop(job.job_id)
            await self._ready.put((job.dispute_id, job.attempt + 1))

    def pending(self) -> int:
        return len(self._pending) + self._ready.qsize()

    async def ping(self) -> bool:
        return True


class RedisJudgeJobs:
    def __init__(self, client, consumer: str | None = None, block_ms: int = 5_000) -> None:
        self._redis = client  # redis.asyncio.Redis with decode_responses=True
        self._consumer = consumer or socket.gethostname()  # the pod name
        self._block_ms = block_ms

    async def _ensure_group(self) -> None:
        from redis.exceptions import ResponseError

        try:
            await self._redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):  # the group already exists: fine
                raise

    async def enqueue(self, dispute_id: UUID) -> None:
        await self._redis.xadd(STREAM, {"dispute_id": str(dispute_id)}, maxlen=MAX_STREAM_LENGTH, approximate=True)

    async def _attempt(self, entry_id: str) -> int:
        info = await self._redis.xpending_range(STREAM, GROUP, min=entry_id, max=entry_id, count=1)
        return int(info[0]["times_delivered"]) if info else 1

    async def _job(self, entry_id: str, fields: dict) -> JudgeJob | None:
        try:
            dispute_id = UUID(fields["dispute_id"])
        except (KeyError, ValueError):
            await self._redis.xack(STREAM, GROUP, entry_id)  # malformed: drop it, it can never succeed
            return None
        return JudgeJob(dispute_id=dispute_id, job_id=entry_id, attempt=await self._attempt(entry_id))

    async def receive(self) -> AsyncIterator[JudgeJob]:
        from redis.exceptions import ResponseError

        await self._ensure_group()
        while True:
            await asyncio.sleep(0)  # let other tasks run between reads, whatever the client does
            try:
                # First, jobs another worker received and never acknowledged (it died, or failed).
                _, claimed, _ = await self._redis.xautoclaim(STREAM, GROUP, self._consumer,
                                                             min_idle_time=RECLAIM_IDLE_MS, count=1)
                entries = claimed
                if not entries:
                    read = await self._redis.xreadgroup(GROUP, self._consumer, {STREAM: ">"}, count=1,
                                                        block=self._block_ms)
                    entries = read[0][1] if read else []
            except ResponseError as exc:
                if "NOGROUP" not in str(exc):
                    raise
                # The stream or the group was deleted (for example `make demo-reset` flushes Redis):
                # create it again and keep consuming, instead of stopping the worker.
                await self._ensure_group()
                continue
            for entry_id, fields in entries:
                if fields and (job := await self._job(entry_id, fields)) is not None:
                    yield job

    async def ack(self, job: JudgeJob) -> None:
        await self._redis.xack(STREAM, GROUP, job.job_id)

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False


def build_judge_jobs() -> JudgeJobs | None:
    """From JUDGE_JOBS: `redis` (needs REDIS_URL), `memory`, or `off` (the default: no judging)."""
    backend = os.environ.get("JUDGE_JOBS", "off").strip()
    if backend == "off":
        return None
    if backend == "memory":
        return InMemoryJudgeJobs()
    if backend == "redis":
        import redis.asyncio as redis

        return RedisJudgeJobs(redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                             socket_timeout=10, socket_connect_timeout=2))
    raise ValueError(f"unknown JUDGE_JOBS={backend!r} (supported: redis, memory, off)")
