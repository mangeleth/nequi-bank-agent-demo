"""Supervisor tests. The LLM is scripted and the specialist agents are fakes, so each test can
set up a situation (a looping model, a failing agent, an inflated refund) and check that the
graph's deterministic controls decide the outcome.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from services.supervisor.clients import SpecialistUnavailable
from services.supervisor.graph import MAX_CALLS_PER_AGENT, MAX_SUPERVISOR_TURNS, breaker
from services.supervisor.main import create_app
from shared.refund_policy import CustomerRefundHistory, RefundPolicyConfig
from shared.schemas import FraudAssessment, LedgerReconciliation
from shared.tracing import Tracing
from tests.fakes import ScriptedChatModel, ai
from tests.fakes import tool_call as call
from tests.jwt_helpers import SETTINGS, bearer

TX = "TX-20261001000001"
URL = "/v1/disputes/triage"
DISPUTE = {"transaction_id": TX, "reason": "failed_transfer", "claimed_amount": "50000.00",
           "description": "I sent money and it never arrived"}


class FakeSpecialists:
    """Stands in for the Fraud Agent, Ledger Agent, and Core Systems."""

    def __init__(self, *, status="failed", debited="50000.00", credited="0.00", risk=(0.08, "low"),
                 fraud_failures=0, ledger_failures=0, history=(0, "0"), history_down=False, owns=True):
        self.ledger = LedgerReconciliation(transaction_id=TX, settlement_status=status, debited_amount=debited,
                                           credited_amount=credited, summary="From the ledger.")
        self.fraud = FraudAssessment(transaction_id=TX, risk_score=risk[0], risk_level=risk[1], rationale="Signals.")
        self.history = CustomerRefundHistory(auto_refund_count=history[0], auto_refund_total=Decimal(history[1]))
        self.fraud_failures, self.ledger_failures = fraud_failures, ledger_failures
        self.history_down, self.owns = history_down, owns
        self.fraud_calls = self.ledger_calls = 0
        self.tokens_received = []

    async def owns_transaction(self, caller, transaction_id):
        return self.owns

    async def assess_fraud(self, dispute, token):
        self.fraud_calls += 1
        self.tokens_received.append(token)
        if self.fraud_calls <= self.fraud_failures:
            raise SpecialistUnavailable("HTTP 502 from fraud agent")
        return self.fraud

    async def reconcile_ledger(self, dispute, token):
        self.ledger_calls += 1
        self.tokens_received.append(token)
        if self.ledger_calls <= self.ledger_failures:
            raise SpecialistUnavailable("HTTP 502 from ledger agent")
        return self.ledger

    async def refund_history(self, caller, window_days):
        if self.history_down:
            raise SpecialistUnavailable("HTTP 503 from core systems")
        return self.history

    async def ready(self):
        return True


def route(next_: str) -> object:
    return ai(call("Route", "r", next=next_, reason="scripted"))


def verdict(decision="refund_recommended", refund_amount="50000.00") -> object:
    return ai(call("VerdictDraft", "v", decision=decision, refund_amount=refund_amount, explanation="Scripted."))


HAPPY = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict()]


def triage(script, specialists=None, headers=None, **app_options):
    """Run one triage request. Returns (response, scripted model, fake specialists)."""
    model = ScriptedChatModel(script=script)
    specialists = specialists or FakeSpecialists()
    app = create_app(auth=SETTINGS, model=model, specialists=specialists, tracing=Tracing(),
                     policy=RefundPolicyConfig(), **app_options)
    with TestClient(app) as client:
        response = client.post(URL, json=DISPUTE, headers=bearer("user-1001") if headers is None else headers)
    return response, model, specialists


def failed_checks(body: dict) -> set[str]:
    return {check["name"] for check in body["approval"]["checks"] if not check["passed"]}


# --- Outcomes -----------------------------------------------------------------------------------


def test_small_clear_refund_is_auto_approved():
    response, _, _ = triage(HAPPY)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "resolved"
    assert body["verdict"]["decision"] == "refund_recommended"
    assert (body["approval"]["route"], body["approval"]["approved_amount"]) == ("auto_approved", "50000.00")
    assert [step.split(":")[0].split(" ->")[0] for step in body["steps"]] == [
        "supervisor", "ledger_agent", "supervisor", "fraud_agent", "supervisor", "verdict", "policy"]


def test_settled_transfer_needs_no_fraud_assessment():
    script = [route("ledger_agent"), route("finish"), verdict("no_action", None)]
    response, _, specialists = triage(script, FakeSpecialists(status="settled", credited="50000.00"))
    body = response.json()

    assert (body["status"], body["verdict"]["decision"], body["approval"]) == ("resolved", "no_action", None)
    assert specialists.fraud_calls == 0  # the supervisor skipped an agent it did not need


def test_refund_over_the_limit_goes_to_a_human():
    script = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict(refund_amount="450000.00")]
    response, _, _ = triage(script, FakeSpecialists(debited="450000.00"))
    body = response.json()

    assert (body["status"], body["approval"]["route"]) == ("pending_human_approval", "human_required")
    assert failed_checks(body) == {"under_amount_limit", "under_refund_total_limit"}


def test_high_fraud_risk_goes_to_fraud_operations():
    script = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict("escalate_fraud", None)]
    response, _, _ = triage(script, FakeSpecialists(risk=(0.86, "high")))
    body = response.json()
    assert (body["status"], body["verdict"]["decision"], body["approval"]) == (
        "pending_human_approval", "escalate_fraud", None)


def test_model_inflating_the_refund_is_caught_by_the_policy():
    script = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict(refund_amount="90000.00")]
    response, _, _ = triage(script)  # the ledger says 50.000
    body = response.json()

    assert body["approval"]["route"] == "human_required"
    assert "amount_matches_ledger" in failed_checks(body)
    assert body["approval"]["approved_amount"] is None


# --- Circuit breakers ----------------------------------------------------------------------------


def test_model_that_keeps_asking_for_the_same_agent_is_stopped():
    response, model, specialists = triage([route("fraud_agent")])  # the same answer, forever
    body = response.json()

    assert body["status"] == "pending_human_approval"
    assert "fraud agent was already called" in body["escalation_reason"]
    assert specialists.fraud_calls == MAX_CALLS_PER_AGENT
    assert len(model.seen) == MAX_CALLS_PER_AGENT + 1  # no model call after the breaker tripped


def test_finishing_without_ledger_evidence_is_escalated():
    response, _, _ = triage([route("finish")])
    assert "without ledger evidence" in response.json()["escalation_reason"]


def test_invalid_routing_output_is_escalated():
    response, _, _ = triage([ai(call("Route", "r", next="transfer_money", reason="x"))])
    assert "valid routing decision" in response.json()["escalation_reason"]


def test_code_requires_a_fraud_assessment_when_money_is_missing():
    # The model tries to finish straight after the ledger lookup; code sends it to the Fraud Agent.
    script = [route("ledger_agent"), route("finish"), route("finish"), verdict()]
    response, _, specialists = triage(script)
    body = response.json()

    assert specialists.fraud_calls == 1
    assert body["approval"]["route"] == "auto_approved"
    assert any("fraud_agent (required by code" in step for step in body["steps"])


def test_refund_without_a_fraud_assessment_is_escalated_when_the_agent_is_down():
    script = [route("ledger_agent"), route("finish"), route("finish"), route("finish"), verdict()]
    response, _, specialists = triage(script, FakeSpecialists(fraud_failures=99))
    body = response.json()

    assert specialists.fraud_calls == MAX_CALLS_PER_AGENT
    assert "without a fraud assessment" in body["escalation_reason"]
    assert body["approval"] is None


def test_turn_counter_trips_the_breaker():
    assert breaker({"route": "ledger_agent", "turns": MAX_SUPERVISOR_TURNS}) is None
    assert "turns" in breaker({"route": "ledger_agent", "turns": MAX_SUPERVISOR_TURNS + 1})


def test_recursion_limit_is_the_backstop():
    # Even if the counters allowed it, LangGraph stops the run after `recursion_limit` steps.
    response, _, _ = triage(HAPPY, recursion_limit=3)
    body = response.json()
    assert response.status_code == 200
    assert (body["status"], body["verdict"]) == ("pending_human_approval", None)
    assert "hard limit of 3 steps" in body["escalation_reason"]


# --- Failures -------------------------------------------------------------------------------------


def test_agent_failure_is_retried_once():
    script = [route("ledger_agent"), route("fraud_agent"), route("fraud_agent"), route("finish"), verdict()]
    response, _, specialists = triage(script, FakeSpecialists(fraud_failures=1))
    assert response.json()["approval"]["route"] == "auto_approved"
    assert specialists.fraud_calls == 2


def test_refund_history_unavailable_fails_closed():
    response, _, _ = triage(HAPPY, FakeSpecialists(history_down=True))
    body = response.json()
    assert body["status"] == "pending_human_approval"
    assert "refund history was unavailable" in body["escalation_reason"]
    assert body["approval"] is None  # never auto-approved on missing history


def test_unexpected_error_ends_in_human_review_not_a_crash():
    class BrokenSpecialists(FakeSpecialists):
        async def reconcile_ledger(self, dispute, token):
            raise RuntimeError("bug in our own code")

    response, _, _ = triage(HAPPY, BrokenSpecialists())
    body = response.json()
    assert response.status_code == 200
    assert (body["status"], body["escalation_reason"]) == (
        "pending_human_approval", "an unexpected error stopped the triage")


# --- Identity ---------------------------------------------------------------------------------------


def test_token_is_forwarded_to_agents_but_never_shown_to_the_model():
    headers = bearer("user-1001")
    token = headers["Authorization"].removeprefix("Bearer ")
    response, model, specialists = triage(HAPPY, headers=headers)

    assert set(specialists.tokens_received) == {token}
    shown = model.everything_shown_to_model()
    assert token not in shown and "user-1001" not in shown
    assert token not in response.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not.a.token"}])
def test_unauthenticated_request_never_reaches_the_model(headers):
    response, model, _ = triage(HAPPY, headers=headers)
    assert response.status_code == 401
    assert model.seen == []


def test_disputing_someone_elses_transaction_is_404_without_calling_the_model():
    response, model, specialists = triage(HAPPY, FakeSpecialists(owns=False))
    assert response.status_code == 404
    assert model.seen == [] and specialists.ledger_calls == 0
