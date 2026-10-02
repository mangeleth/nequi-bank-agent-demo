"""Langfuse Cloud tracing (ADR-0013). Tracing is an observer: if it is not configured, or
Langfuse is unreachable, the services keep working without it.

Keys are read from files when `LANGFUSE_*_FILE` is set (Key Vault secrets mounted by the CSI
driver, ADR-0005), otherwise from environment variables (local development).
"""

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("tracing")

# W3C Trace Context header: 00-<32 hex trace id>-<16 hex parent span id>-<flags>.
# The supervisor sends it to each agent so the agent's steps join the supervisor's trace.
TRACEPARENT_HEADER = "traceparent"
_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_NO_PARENT = "0" * 16


def parse_traceparent(header: str | None) -> dict | None:
    """Trace context from an incoming `traceparent` header, or None if absent or malformed."""
    match = _TRACEPARENT.match(header or "")
    if match is None:
        return None
    trace_id, parent_span_id = match.groups()
    context = {"trace_id": trace_id}
    if parent_span_id != _NO_PARENT:
        context["parent_span_id"] = parent_span_id
    return context


def outgoing_traceparent(config: dict | None, trace_id: str | None) -> str | None:
    """`traceparent` for a call made from inside a graph node: same trace, with the node's own
    step as the parent, so the callee's steps appear nested under it.

    Finding the node's step relies on the Langfuse handler's internal `_runs` table (the public
    API has no accessor for it). If that changes, the callee's steps still join the same trace,
    just at the top level instead of nested.
    """
    if trace_id is None:
        return None
    parent_span_id = _NO_PARENT
    try:
        manager = (config or {}).get("callbacks")
        for handler in getattr(manager, "handlers", []):
            span = getattr(handler, "_runs", {}).get(manager.parent_run_id)
            if span is not None and re.fullmatch(r"[0-9a-f]{16}", str(span.id)):
                parent_span_id = span.id
                break
    except Exception:  # tracing must never break the request
        log.debug("could not determine the current span; joining the trace at the top level")
    return f"00-{trace_id}-{parent_span_id}-01"


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

    def start(self, traceparent: str | None = None) -> tuple[list, str | None]:
        """Callbacks for one run, and the ID of the trace they will write to.

        With a valid `traceparent` the run joins the caller's trace; otherwise it starts its own.
        """
        if self._client is None:
            return [], None
        from langfuse.langchain import CallbackHandler

        context = parse_traceparent(traceparent) or {"trace_id": self._client.create_trace_id()}
        return [CallbackHandler(public_key=self._public_key, trace_context=context)], context["trace_id"]

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
