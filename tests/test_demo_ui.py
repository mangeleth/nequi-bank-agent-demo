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


def test_each_customers_transfers_display_differently():
    """A selectbox tracks its choice by what it displays: equal labels once swapped TX-...0001 for
    TX-...0008 without the viewer noticing."""
    for user_id, transactions in logic.TRANSACTIONS.items():
        shown = [t.display for t in transactions]
        assert len(set(shown)) == len(shown), user_id
        assert len({t.label for t in transactions}) == len(transactions), user_id


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
    assert progress.lines() == ["📥 Recibida · 0.2 s", "🔎 En revisión · 1.0 s", "💸 Reembolso pagado · 9.9 s"]


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
    assert outcome.path == "incident" and outcome.headline.startswith("⚡ Decidida sin un modelo")
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
    assert [tab.label for tab in page.tabs] == ["📱 App del cliente", "👤 Revisión (supervisor)",
                                                "📊 Tablero de evaluación"]  # no demo script (#18)
    assert any("La revisión no está disponible" in w.value for w in page.warning)  # no API here: no crash
    metrics = {m.label: m.value for m in page.metric}
    assert {"Solicitudes evaluadas", "Solicitudes exitosas", "Tasa de éxito", "Costo total", "Costo por éxito"} <= set(metrics)
    assert {"Acuerdo · held-out", "Aprobaciones inseguras · held-out"} <= set(metrics)  # the judge's health
    assert metrics["Tasa de éxito"].endswith("%") and metrics["Costo total"].startswith("$")


# --- The trace inside the demo (ADR-0025) ---------------------------------------------------------


def _obs(id_, name, kind, start, end, parent=None, **extra):
    return {"id": id_, "name": name, "type": kind, "parentObservationId": parent,
            "startTime": f"2026-10-02T16:52:{start}Z", "endTime": f"2026-10-02T16:52:{end}Z", **extra}


TRACE = [  # the shape of a real trace: graph nodes, an agent in another pod, LangChain internals
    _obs("root", "dispute-triage", "CHAIN", "17.000", "22.000"),
    _obs("sup", "supervisor", "CHAIN", "17.002", "18.000", "root"),
    _obs("seq", "RunnableSequence", "CHAIN", "17.004", "17.999", "sup"),
    _obs("g1", "AzureChatOpenAI", "GENERATION", "17.008", "17.900", "seq", model="gpt-4o-2024-11-20",
         usageDetails={"total": 595}, totalCost=0.0021),
    _obs("node", "ledger_agent", "CHAIN", "18.090", "20.000", "root"),
    _obs("svc", "ledger-agent", "AGENT", "18.092", "19.990", "node"),
    _obs("mw", "ModelCallLimitMiddleware.before_model", "CHAIN", "18.094", "18.095", "svc"),
    _obs("g2", "AzureChatOpenAI", "GENERATION", "18.096", "19.000", "svc", model="gpt-4o-2024-11-20",
         usageDetails={"total": 700}, totalCost=0.0025),
    _obs("t1", "get_transaction", "TOOL", "19.099", "19.110", "svc", input='{"transaction_id": "TX-20261001000003"}',
         output='{"settlement_status": "settled"}'),
]


def test_trace_view_shows_the_story_and_hides_langchain_internals():
    view = logic.trace_view(TRACE)

    assert [(r["depth"], r["label"]) for r in view.rows] == [
        (0, "📨 Dispute triage (the whole run)"),
        (1, "🧭 Supervisor decides the next step"),
        (2, "🧠 Model call (gpt-4o-2024-11-20)"),  # its RunnableSequence parent is hidden, not counted
        (1, "📒 Ask the Ledger Agent"),
        (2, "📒 Ledger Agent service"),
        (3, "🧠 Model call (gpt-4o-2024-11-20)"),
        (3, "🔧 get_transaction(TX-20261001000003)"),
    ]
    assert view.hidden == 2
    assert (view.model_calls, view.tool_calls, view.tokens) == (2, 1, 1295)
    assert view.cost_usd == 0.0046 and view.duration_s == 5.0
    assert view.rows[6]["start_ms"] == 2099 and view.rows[6]["output"] == '{"settlement_status": "settled"}'


def test_trace_view_can_include_every_step_and_totals_do_not_change():
    every = logic.trace_view(TRACE, include_internal=True)
    assert len(every.rows) == len(TRACE) and every.hidden == 0
    assert (every.model_calls, every.cost_usd) == (2, 0.0046)


def test_trace_id_comes_from_the_trace_link():
    assert logic.trace_id_from_url("https://us.cloud.langfuse.com/project/p1/traces/0787695") == "0787695"
    assert logic.trace_id_from_url(None) is None


def test_langfuse_keys_are_read_from_mounted_files(tmp_path, monkeypatch):
    (tmp_path / "pk").write_text("pk-lf-demo\n")
    (tmp_path / "sk").write_text("sk-lf-demo\n")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY_FILE", str(tmp_path / "pk"))
    monkeypatch.setenv("LANGFUSE_SECRET_KEY_FILE", str(tmp_path / "sk"))
    assert logic.LangfuseReader.from_env() is not None

    monkeypatch.setenv("LANGFUSE_SECRET_KEY_FILE", str(tmp_path / "missing"))
    assert logic.LangfuseReader.from_env() is None  # no keys: the UI only links to the trace


def test_the_chosen_transfer_stays_chosen_across_reruns(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("EVAL_RESULTS_DIR", "evals/results")
    page = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "services/demo_ui/app.py"),
                             default_timeout=30).run()
    for tx_id, story in (("TX-20261001000001", "Escenario 1"), ("TX-20261001000008", "Escenario 5"),
                         ("TX-20261001000001", "Escenario 1")):
        page.selectbox(key="tx-A-user-1001").set_value(tx_id).run()
        page.run()  # another rerun, as any click causes
        assert page.selectbox(key="tx-A-user-1001").value == tx_id
        assert any(c.value.startswith(story) for c in page.caption)


def test_a_trace_still_arriving_is_detected_from_the_disputes_steps():
    steps = ["supervisor -> ledger_agent: evidence first", "ledger_agent: ok",
             "supervisor -> fraud_agent: then risk", "fraud_agent: ok", "verdict: refund_recommended"]
    only_ledger = TRACE  # has the ledger-agent service, not the fraud-agent one
    assert logic.missing_from_trace(only_ledger, steps) == ["fraud-agent"]
    complete = TRACE + [_obs("svc2", "fraud-agent", "AGENT", "20.100", "21.000", "root")]
    assert logic.missing_from_trace(complete, steps) == []
    assert logic.missing_from_trace([], ["incident: covered", "verdict: no_action"]) == []


def test_the_scenario_table_says_what_decided_each_one(tmp_path):
    _report(tmp_path, "run", [
        {**_result(True, 0.02), "id": "agents"},
        {**_result(True, 0.0, "INC-20261001-01"), "id": "incident"},
        {"id": "duplicate", "task_success": True, "cost_usd": 0.0, "latency_ms": 300,
         "actual": {"model_calls": 0, "status": "refund_paid"}},
        {"id": "refused", "task_success": True, "cost_usd": 0.0, "latency_ms": 300,
         "actual": {"model_calls": 0, "status": None}},
    ])
    paths = {row["escenario"]: (row["camino"], row["resultado"]) for row in logic.latest_scenarios(tmp_path)}
    assert paths == {"agents": ("🤖 agentes", "refund_paid"), "incident": ("⚡ incidente", "refund_paid"),
                     "duplicate": ("— sin revisión", "refund_paid"), "refused": ("— sin revisión", "—")}


# --- The reviewer (ADR-0027) and the judge's health (ADR-0026) ------------------------------------


def test_a_reviewer_token_from_the_ui_is_accepted_only_as_a_reviewer():
    from shared.auth import verify_reviewer_token

    token = logic.login_reviewer("ops-ana", LOGIN)
    assert verify_reviewer_token(token, SETTINGS).reviewer_id == "ops-ana"
    with pytest.raises(ValueError):
        logic.login_reviewer("user-1001", LOGIN)


def test_the_reviewer_works_the_queue_through_the_real_intake_api():
    from tests.test_review import TO_A_PERSON
    from tests.test_supervisor import FakeSpecialists

    specialists = FakeSpecialists(debited="450000.00")
    with TestClient(_supervisor(specialists, script=TO_A_PERSON)) as http:
        customer = logic.IntakeClient("http://supervisor", http=http)
        token = logic.login("user-1001", LOGIN)
        submitted = customer.submit(token, logic.find_transaction("user-1001", "TX-20261001000002"), "no llegó")
        _follow(customer, token, submitted.json()["dispute_id"])

        reviewer = logic.ReviewClient("http://supervisor", http=http)
        rtoken = logic.login_reviewer("ops-ana", LOGIN)
        queue = reviewer.queue(rtoken)
        rows = logic.queue_rows(queue)
        decided = reviewer.decide(rtoken, queue[0]["dispute"]["dispute_id"], "approve", "verified in the ledger")
        view, _ = _follow(customer, token, submitted.json()["dispute_id"])

    assert rows[0]["cliente"] == "user-1001" and "under_amount_limit" in rows[0]["por qué llegó a una persona"]
    assert rows[0]["juez"] == "— sin evaluar"
    assert decided.status_code == 200 and view["status"] == "refund_paid"
    assert view["result"]["approval"]["approved_by"] == "ops-ana"


def test_judgement_rows_and_badges_in_spanish():
    judgement = {"passed": False, "prompt_version": "v3", "result": {
        "groundedness": {"passed": False, "reason": "invented cause"},
        "completeness": {"passed": True, "reason": "ok"}, "clarity": {"passed": True, "reason": "ok"}}}
    assert logic.judge_badge(judgement) == "❌ con problemas" and logic.judge_badge(None) == "— sin evaluar"
    assert logic.judgement_rows(judgement)[0] == {"criterio": "Basada en los registros", "resultado": "❌ no cumple",
                                                  "razón del juez": "invented cause"}


def test_the_judge_health_reads_every_calibration_run():
    rows = logic.load_calibrations(Path("evals/judge/results"))
    assert {r["prompt"] for r in rows} >= {"v1", "v2", "v3"}
    assert {r["split"] for r in rows} == {"tuning", "held-out"}
    assert all(0 <= r["agreement"] <= 1 and r["unsafe_passes"] >= 0 for r in rows)


def test_the_summary_says_who_decided_and_when_in_colombia_time():
    reviewed = {"review": {"decision": "approve", "reviewer_id": "ops-ana", "note": "verificado en el libro",
                           "decided_at": "2026-10-02T18:32:53Z", "amount": "450000.00"},
                "approval": {"route": "human_approved", "approved_amount": "450000.00"},
                "payment": {"amount": "450000.00", "currency": "COP", "refund_id": "RF-7012",
                            "executed_at": "2026-10-02T18:32:54Z"}}
    approved, paid = logic.decision_summary(reviewed)
    assert "Aprobada por una persona" in approved and "**ops-ana**" in approved
    assert "el 02/10/2026 a las 13:32 (hora de Colombia)" in approved  # 18:32 UTC is 13:32 in Bogotá
    assert "450000.00 COP" in approved and "verificado en el libro" in approved
    assert "RF-7012" in paid and "13:32" in paid

    rejected = logic.decision_summary({"review": {"decision": "reject", "reviewer_id": "ops-luis", "note": "ya llegó",
                                                  "decided_at": "2026-10-02T15:00:00Z"}})
    assert rejected == ["👤 **Rechazada por una persona:** **ops-luis** el 02/10/2026 a las 10:00 (hora de Colombia). "
                        "Motivo: “ya llegó”."]


def test_the_summary_for_the_policy_and_for_an_incident():
    auto = logic.decision_summary({"approval": {"route": "auto_approved", "approved_amount": "50000.00",
                                                "evaluated_at": "2026-10-02T17:00:00Z"}})
    assert auto[0].startswith("⚙️ **Aprobada automáticamente**") and "12:00 (hora de Colombia)" in auto[0]
    covered = logic.decision_summary({"incident": {"incident_id": "INC-20261001-01", "confirmed_by": "operations-lead"},
                                      "verdict": {"decided_at": "2026-10-02T17:00:00Z"}})
    assert "INC-20261001-01" in covered[0] and "sin un modelo" in covered[0]
    assert logic.decision_summary(None) == []


def test_judge_vs_people_counts_confirmations_false_alarms_and_unsafe_passes():
    def label(kind, judge, human):
        names = ("groundedness", "completeness", "clarity")
        return {"kind": kind, "human_verdict": dict(zip(names, human, strict=True)),
                "judgement": {"result": {n: {"passed": j, "reason": "r"} for n, j in zip(names, judge, strict=True)}}}

    stats = logic.judge_vs_people([
        label("judge_flag", (False, True, True), (False, True, True)),  # the judge was right
        label("judge_flag", (False, True, True), (True, True, True)),  # a false alarm
        label("control_sample", (True, True, True), (False, True, True)),  # an unsafe pass, found by sampling
    ])
    g = stats["criteria"]["groundedness"]
    assert (g["reviewed"], g["agree"], g["confirmed"], g["false_alarms"], g["unsafe_passes"]) == (3, 1, 1, 1, 1)
    assert stats["kinds"] == {"judge_flag": 2, "control_sample": 1}
    assert stats["criteria"]["clarity"]["agree"] == 3
