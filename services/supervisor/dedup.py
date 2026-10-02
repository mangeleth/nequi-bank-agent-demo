"""Idempotency and deduplication at the gate (ADR-0015).

A frustrated customer taps "Dispute" several times. Every tap produces the same key,
sha256(user_id, transaction_id). The first request claims the key and runs; a duplicate never
reaches a model: it gets the stored result if the first has finished, or "already being
processed" if it has not.

The store must be shared by every supervisor replica, so the cluster uses Redis:
    SET key in_progress NX EX <lock seconds>     one atomic step: claim it only if nobody has
The in-memory store is for tests and single-process local runs.
"""

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Protocol

IN_PROGRESS = "in_progress"


class GateUnavailable(Exception):
    """The store cannot be reached. The caller fails closed: no claim, no triage."""


@dataclass(frozen=True)
class Claim:
    """Outcome of trying to claim a dispute key."""

    claimed: bool  # True: this request is the first and must run the triage
    result: str | None = None  # the stored result (JSON) when an earlier request already finished

    @property
    def in_progress(self) -> bool:
        return not self.claimed and self.result is None


def dispute_key(user_id: str, transaction_id: str) -> str:
    """Deterministic key for one customer's dispute of one transaction.

    The same customer and transaction always give the same key, whatever the reason or
    description, and two customers can never collide on a transaction ID.
    """
    digest = hashlib.sha256(f"{user_id}\x1f{transaction_id}".encode()).hexdigest()
    return f"dispute:{digest}"


@dataclass(frozen=True)
class GateSettings:
    lock_seconds: int = 120  # how long a running triage holds the key; a crashed run frees it after this
    result_seconds: int = 86_400  # how long a finished result answers duplicates (24 hours)

    @classmethod
    def from_env(cls) -> "GateSettings":
        default = cls()
        return cls(
            lock_seconds=int(os.environ.get("DEDUP_LOCK_SECONDS", default.lock_seconds)),
            result_seconds=int(os.environ.get("DEDUP_RESULT_SECONDS", default.result_seconds)),
        )


class DisputeGate(Protocol):
    async def claim(self, key: str) -> Claim: ...

    async def complete(self, key: str, result: str) -> None:
        """Store the finished result so later duplicates receive it."""

    async def release(self, key: str) -> None:
        """Give the key back without a result (the request was refused or could not start)."""

    async def ping(self) -> bool: ...


class InMemoryGate:
    """Single-process store. Not shared between replicas: tests and local runs only."""

    def __init__(self, settings: GateSettings | None = None) -> None:
        self._settings = settings or GateSettings()
        self._entries: dict[str, tuple[str, float]] = {}  # key -> (value, expires at)
        self._lock = asyncio.Lock()

    def _get(self, key: str) -> str | None:
        value, expires_at = self._entries.get(key, (None, 0.0))
        return value if time.monotonic() < expires_at else None

    async def claim(self, key: str) -> Claim:
        async with self._lock:
            current = self._get(key)
            if current is None:
                self._entries[key] = (IN_PROGRESS, time.monotonic() + self._settings.lock_seconds)
                return Claim(claimed=True)
            return Claim(claimed=False, result=None if current == IN_PROGRESS else current)

    async def complete(self, key: str, result: str) -> None:
        async with self._lock:
            self._entries[key] = (result, time.monotonic() + self._settings.result_seconds)

    async def release(self, key: str) -> None:
        async with self._lock:
            if self._get(key) == IN_PROGRESS:
                del self._entries[key]

    async def ping(self) -> bool:
        return True


class RedisGate:
    """Shared store for all replicas. `client` is a redis.asyncio client with decode_responses=True."""

    def __init__(self, client, settings: GateSettings | None = None) -> None:
        self._redis = client
        self._settings = settings or GateSettings()

    async def claim(self, key: str) -> Claim:
        from redis.exceptions import RedisError

        try:
            # NX: set only if the key does not exist. One atomic step, so of ten simultaneous
            # requests on any replicas exactly one gets True.
            if await self._redis.set(key, IN_PROGRESS, nx=True, ex=self._settings.lock_seconds):
                return Claim(claimed=True)
            current = await self._redis.get(key)
        except RedisError as exc:
            raise GateUnavailable(type(exc).__name__) from exc
        return Claim(claimed=False, result=None if current in (None, IN_PROGRESS) else current)

    async def complete(self, key: str, result: str) -> None:
        from redis.exceptions import RedisError

        try:
            await self._redis.set(key, result, ex=self._settings.result_seconds)
        except RedisError as exc:
            raise GateUnavailable(type(exc).__name__) from exc

    async def release(self, key: str) -> None:
        from redis.exceptions import RedisError, WatchError

        try:
            async with self._redis.pipeline() as pipe:
                await pipe.watch(key)  # delete only if it is still our in-progress marker
                if await pipe.get(key) == IN_PROGRESS:
                    pipe.multi()
                    pipe.delete(key)
                    await pipe.execute()
        except WatchError:
            pass  # the key changed under us: someone else owns it now, leave it
        except RedisError as exc:
            raise GateUnavailable(type(exc).__name__) from exc

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False


def build_gate() -> DisputeGate:
    """Choose the store from DEDUP_BACKEND: `redis` (needs REDIS_URL) or `memory`."""
    backend = os.environ.get("DEDUP_BACKEND", "memory").strip()
    settings = GateSettings.from_env()
    if backend == "memory":
        return InMemoryGate(settings)
    if backend == "redis":
        import redis.asyncio as redis

        client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True,
                                socket_timeout=2, socket_connect_timeout=2)
        return RedisGate(client, settings)
    raise ValueError(f"unknown DEDUP_BACKEND={backend!r} (supported: redis, memory)")
