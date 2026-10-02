"""Langfuse Cloud tracing (ADR-0013). Tracing is an observer: if it is not configured, or
Langfuse is unreachable, the services keep working without it.

Keys are read from files when `LANGFUSE_*_FILE` is set (Key Vault secrets mounted by the CSI
driver, ADR-0005), otherwise from environment variables (local development).
"""

import logging
import os
from pathlib import Path

log = logging.getLogger("tracing")


def _setting(name: str) -> str:
    """Value of NAME, or the contents of the file named by NAME_FILE."""
    if path := os.environ.get(f"{name}_FILE", "").strip():
        return Path(path).read_text().strip()
    return os.environ.get(name, "").strip()


class Tracing:
    def __init__(self, client=None, public_key: str | None = None) -> None:
        self._client = client
        self._public_key = public_key

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def start(self) -> tuple[list, str | None]:
        """Callbacks for one graph run, and the ID of the trace they will write to."""
        if self._client is None:
            return [], None
        from langfuse.langchain import CallbackHandler

        trace_id = self._client.create_trace_id()
        return [CallbackHandler(public_key=self._public_key, trace_context={"trace_id": trace_id})], trace_id

    def url(self, trace_id: str | None) -> str | None:
        if self._client is None or trace_id is None:
            return None
        return self._client.get_trace_url(trace_id=trace_id)

    def shutdown(self) -> None:
        """Send any buffered traces before the process exits."""
        if self._client is not None:
            self._client.shutdown()


def build_tracing() -> Tracing:
    public_key, secret_key = _setting("LANGFUSE_PUBLIC_KEY"), _setting("LANGFUSE_SECRET_KEY")
    base_url = os.environ.get("LANGFUSE_BASE_URL", "").strip()
    if not (public_key and secret_key and base_url):
        log.warning("Langfuse is not configured; tracing is disabled")
        return Tracing()

    from langfuse import Langfuse

    return Tracing(Langfuse(public_key=public_key, secret_key=secret_key, base_url=base_url), public_key)
