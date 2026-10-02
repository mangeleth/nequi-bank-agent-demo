from types import SimpleNamespace
from uuid import uuid4

import pytest

from shared.tracing import Tracing, build_tracing, outgoing_traceparent, parse_traceparent

TRACE = "a" * 32
SPAN = "b" * 16


def test_parse_valid_traceparent():
    assert parse_traceparent(f"00-{TRACE}-{SPAN}-01") == {"trace_id": TRACE, "parent_span_id": SPAN}


def test_zero_parent_means_join_the_trace_at_the_top_level():
    assert parse_traceparent(f"00-{TRACE}-{'0' * 16}-01") == {"trace_id": TRACE}


@pytest.mark.parametrize("header", [None, "", "garbage", f"00-{TRACE}-{SPAN}", f"00-{'A' * 32}-{SPAN}-01",
                                    f"00-{TRACE[:-1]}-{SPAN}-01", f"00-{TRACE}-{SPAN}-01\r\nX-Injected: 1"])
def test_malformed_traceparent_is_ignored(header):
    assert parse_traceparent(header) is None


def test_outgoing_traceparent_uses_the_current_node_as_parent():
    run_id = uuid4()
    handler = SimpleNamespace(_runs={run_id: SimpleNamespace(id=SPAN, trace_id=TRACE)})
    config = {"callbacks": SimpleNamespace(parent_run_id=run_id, handlers=[handler])}
    assert outgoing_traceparent(config, TRACE) == f"00-{TRACE}-{SPAN}-01"


@pytest.mark.parametrize("config", [None, {}, {"callbacks": None}, {"callbacks": object()},
                                    {"callbacks": SimpleNamespace(parent_run_id=uuid4(), handlers=[object()])}])
def test_outgoing_traceparent_falls_back_to_the_top_level(config):
    assert outgoing_traceparent(config, TRACE) == f"00-{TRACE}-{'0' * 16}-01"


def test_no_trace_no_header():
    assert outgoing_traceparent({}, None) is None


def test_tracing_is_optional(monkeypatch):
    for name in ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY_FILE",
                 "LANGFUSE_SECRET_KEY_FILE", "LANGFUSE_BASE_URL"]:
        monkeypatch.delenv(name, raising=False)
    tracing = build_tracing()
    assert not tracing.enabled
    assert tracing.start(f"00-{TRACE}-{SPAN}-01") == ([], None)
    assert tracing.url(TRACE) is None
    tracing.shutdown()


def test_keys_are_read_from_mounted_files(monkeypatch, tmp_path):
    (tmp_path / "pk").write_text("pk-lf-test\n")
    (tmp_path / "sk").write_text("sk-lf-test\n")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY_FILE", str(tmp_path / "pk"))
    monkeypatch.setenv("LANGFUSE_SECRET_KEY_FILE", str(tmp_path / "sk"))
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    tracing = build_tracing()
    try:
        assert tracing.enabled
        callbacks, trace_id = tracing.start(f"00-{TRACE}-{SPAN}-01")
        assert len(callbacks) == 1 and trace_id == TRACE  # joined the caller's trace
    finally:
        tracing.shutdown()
