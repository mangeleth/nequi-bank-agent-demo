"""The demo UI (ADR-0023): its logic, driven against the real intake API in-process, and the
Streamlit page itself rendered headless."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.core_systems.adapters.fixtures import TRANSACTIONS as LEDGER
from services.demo_ui import logic
from shared.auth import verify_token
from tests.jwt_helpers import AUDIENCE, ISSUER, PRIVATE_KEY, SETTINGS

LOGIN = logic.LoginSettings(private_key_pem=PRIVATE_KEY, issuer=ISSUER, audience=AUDIENCE)


def test_every_demo_transaction_is_the_customers_and_claims_what_was_debited():
    for user_id, transactions in logic.TRANSACTIONS.items():
        for tx in transactions:
            row = LEDGER[tx.transaction_id]
            assert row["customer_id"] == user_id, tx.transaction_id
            assert tx.amount == str(row["debited_amount"]), tx.transaction_id


def test_login_issues_a_token_the_intake_api_accepts_for_that_customer_only():
    identity = verify_token(logic.login("user-1002", LOGIN), SETTINGS)
    assert identity.user_id == "user-1002"
    with pytest.raises(ValueError):
        logic.login("user-9999", LOGIN)  # not a demo customer


@pytest.mark.parametrize(("view", "settled"), [
    ({"execution_status": "queued", "status": "received"}, False),
    ({"execution_status": "running", "status": "investigating"}, False),
    ({"execution_status": "finished", "status": "refund_approved"}, False),  # being paid
    ({"execution_status": "finished", "status": "refund_paid"}, True),
    ({"execution_status": "finished", "status": "pending_human_approval"}, True),
    ({"execution_status": "failed", "status": "pending_human_approval"}, True),
])
def test_is_settled(view, settled):
    assert logic.is_settled(view) is settled


def test_progress_records_each_status_once_in_order():
    progress = logic.Progress(started=100.0)
    for status, at in [("received", 100.2), ("received", 100.5), ("investigating", 101.0), ("refund_paid", 109.9)]:
        progress.observe({"status": status}, at)
    assert progress.seen == [("received", 0.2), ("investigating", 1.0), ("refund_paid", 9.9)]


def _supervisor(specialists, script):
    from services.supervisor.dedup import InMemoryGate
    from services.supervisor.main import create_app
    from services.supervisor.store import InMemoryDisputeStore
    from shared.refund_policy import RefundPolicyConfig
    from shared.tracing import Tracing
    from tests.fakes import ScriptedChatModel
    from tests.jwt_helpers import DELEGATION, SIGNER

    return create_app(auth=SETTINGS, model=ScriptedChatModel(script=script), specialists=specialists,
                      tracing=Tracing(), policy=RefundPolicyConfig(), gate=InMemoryGate(),
                      store=InMemoryDisputeStore(), signer=SIGNER, delegation=DELEGATION, retry_delay_seconds=0)


def _follow(client: logic.IntakeClient, token: str, dispute_id: str) -> tuple[dict, logic.Progress]:
    progress = logic.Progress(started=time.monotonic())
    for _ in range(500):
        view = client.get(token, dispute_id)
        progress.observe(view, time.monotonic())
        if logic.is_settled(view):
            return view, progress
        time.sleep(0.01)
    raise AssertionError(f"never settled: {view}")


def test_the_app_follows_a_covered_dispute_to_paid_and_shows_the_fast_path():
    from tests.test_supervisor import covered

    specialists = covered()
    with TestClient(_supervisor(specialists, script=[])) as http:
        client = logic.IntakeClient("http://supervisor", http=http)
        token = logic.login("user-1001", LOGIN)
        submitted = client.submit(token, logic.find_transaction("user-1001", "TX-20261001000001"), "no llegó")
        view, progress = _follow(client, token, submitted.json()["dispute_id"])

    outcome = logic.read_outcome(view)
    assert submitted.status_code == 202
    assert view["status"] == "refund_paid" and progress.seen[-1][0] == "refund_paid"
    assert outcome.path == "incident" and outcome.headline.startswith("⚡ Decided without a model")
    assert outcome.trace_url is None and outcome.payment["refund_id"].startswith("RF-")
    assert {check["name"] for check in outcome.checks} >= {"auto_refund_enabled", "under_amount_limit"}


def test_the_app_shows_an_agent_investigation_as_such():
    from tests.test_supervisor import HAPPY, FakeSpecialists

    with TestClient(_supervisor(FakeSpecialists(), script=HAPPY)) as http:
        client = logic.IntakeClient("http://supervisor", http=http)
        token = logic.login("user-1001", LOGIN)
        submitted = client.submit(token, logic.find_transaction("user-1001", "TX-20261001000001"), "no llegó")
        view, _ = _follow(client, token, submitted.json()["dispute_id"])

    assert logic.read_outcome(view).path == "agents"


def _report(tmp: Path, name: str, results: list[dict], commit: str = "abc1234") -> None:
    (tmp / f"{name}.json").write_text(json.dumps(
        {"meta": {"date": name, "commit": commit, "target": "aks", "model": "gpt-4o"}, "summary": {}, "results": results}))


def _result(success: bool, cost: float, incident: str | None = None) -> dict:
    return {"id": "s", "task_success": success, "cost_usd": cost, "accept_ms": 300, "latency_ms": 10_000,
            "actual": {"incident_id": incident, "model_calls": 0 if incident else 8, "status": "refund_paid"}}


def test_dashboard_numbers_are_built_from_counts_and_total_cost(tmp_path):
    _report(tmp_path, "2026-10-02 14:22", [_result(True, 0.02)] * 10)
    _report(tmp_path, "2026-10-02 14:39", [_result(True, 0.02)] * 7 + [_result(False, 0.02)] * 3)  # the payment bug
    _report(tmp_path, "2026-10-02 16:08", [_result(True, 0.02)] * 11 + [_result(True, 0.0, "INC-20261001-01")])

    before, bug, after = logic.load_runs(tmp_path)

    assert (bug["evaluated"], bug["successful"], bug["success_rate"]) == (10, 7, 0.7)
    assert bug["total_cost_usd"] == before["total_cost_usd"] == 0.2  # total cost alone hides the bug...
    assert bug["cost_per_success_usd"] == 0.0286 > before["cost_per_success_usd"] == 0.02  # ...this shows it
    assert (after["fast_path"], after["fast_path_cost_usd"]) == (1, 0.0)


def test_the_streamlit_page_renders_with_the_real_reports(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("EVAL_RESULTS_DIR", "evals/results")
    page = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "services/demo_ui/app.py"), default_timeout=30).run()

    assert not page.exception
    assert [tab.label for tab in page.tabs] == ["📱 Customer app", "📊 Evaluation dashboard", "🗺️ Demo script"]
    metrics = {m.label: m.value for m in page.metric}
    assert set(metrics) == {"Evaluated requests", "Successful requests", "Success rate", "Total cost", "Cost per success"}
    assert metrics["Success rate"].endswith("%") and metrics["Total cost"].startswith("$")
