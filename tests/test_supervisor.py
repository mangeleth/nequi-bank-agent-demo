"""Supervisor tests. The LLM is scripted and the specialist agents are fakes, so each test can
set up a situation (a looping model, a failing agent, an inflated refund) and check that the
graph's deterministic controls decide the outcome.
"""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from services.supervisor.clients import RefundRefused, SpecialistUnavailable
from services.supervisor.dedup import InMemoryGate
from services.supervisor.graph import MAX_CALLS_PER_AGENT, MAX_SUPERVISOR_TURNS, breaker
from services.supervisor.main import create_app
from services.supervisor.store import InMemoryDisputeStore
from shared.auth import verify_token
from shared.refund_policy import CustomerRefundHistory, RefundPolicyConfig
from shared.schemas import FraudAssessment, LedgerReconciliation, RefundPayment
from shared.tracing import Tracing
from tests.fakes import ScriptedChatModel, ai
from tests.fakes import tool_call as call
from tests.jwt_helpers import DELEGATION, INTERNAL_SETTINGS, SETTINGS, SIGNER, bearer

TX = "TX-20261001000001"
URL = "/v1/disputes"
DISPUTE = {"transaction_id": TX, "reason": "failed_transfer", "claimed_amount": "50000.00",
           "description": "I sent money and it never arrived"}


class FakeSpecialists:
    """Stands in for the Fraud Agent, Ledger Agent, and Core Systems."""

    def __init__(self, *, status="failed", debited="50000.00", credited="0.00", risk=(0.08, "low"),
                 fraud_failures=0, ledger_failures=0, history=(0, "0"), history_down=False, owns=True,
                 pay="paid", pay_failures=0):
        self.ledger = LedgerReconciliation(transaction_id=TX, settlement_status=status, debited_amount=debited,
                                           credited_amount=credited, summary="From the ledger.")
        self.fraud = FraudAssessment(transaction_id=TX, risk_score=risk[0], risk_level=risk[1], rationale="Signals.")
        self.history = CustomerRefundHistory(auto_refund_count=history[0], auto_refund_total=Decimal(history[1]))
        self.fraud_failures, self.ledger_failures = fraud_failures, ledger_failures
        self.history_down, self.owns = history_down, owns
        self.fraud_calls = self.ledger_calls = 0
        self.tokens_received = []
        # The ledger's side of a payment: "paid", "refused" (a definite no), or "down" (no answer).
        # `pay_failures` makes the first N payment calls get no answer, then `pay` applies.
        self.pay, self.pay_failures = pay, pay_failures
        self.pay_calls: list[tuple[str, str, Decimal, str]] = []
        self.paid: dict[str, RefundPayment] = {}  # idempotency key -> refund, as the ledger keeps it

    async def owns_transaction(self, caller, transaction_id):
        return self.owns

    async def assess_fraud(self, dispute, token, traceparent=None):
        self.fraud_calls += 1
        self.tokens_received.append(token)
        if self.fraud_calls <= self.fraud_failures:
            raise SpecialistUnavailable("HTTP 502 from fraud agent")
        return self.fraud

    async def reconcile_ledger(self, dispute, token, traceparent=None):
        self.ledger_calls += 1
        self.tokens_received.append(token)
        if self.ledger_calls <= self.ledger_failures:
            raise SpecialistUnavailable("HTTP 502 from ledger agent")
        return self.ledger

    async def refund_history(self, caller, window_days):
        if self.history_down:
            raise SpecialistUnavailable("HTTP 503 from core systems")
        return self.history

    async def pay_refund(self, user_id, transaction_id, amount, idempotency_key):
        self.pay_calls.append((user_id, transaction_id, amount, idempotency_key))
        if len(self.pay_calls) <= self.pay_failures or self.pay == "down":
            raise SpecialistUnavailable("ConnectTimeout calling the ledger")
        if self.pay == "refused":
            raise RefundRefused("amount_mismatch", "the ledger shows 49000.00 owed, not 50000.00")
        if idempotency_key not in self.paid:  # the same key returns the same refund
            self.paid[idempotency_key] = RefundPayment(
                refund_id=f"RF-{len(self.paid) + 1:016d}", amount=amount, currency="COP",
                executed_at=datetime.now(UTC), idempotency_key=idempotency_key)
        return self.paid[idempotency_key]

    async def ready(self):
        return True


def route(next_: str) -> object:
    return ai(call("Route", "r", next=next_, reason="scripted"))


def verdict(decision="refund_recommended", refund_amount="50000.00") -> object:
    return ai(call("VerdictDraft", "v", decision=decision, refund_amount=refund_amount, explanation="Scripted."))


HAPPY = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict()]


class Outcome:
    """The finished dispute, shaped like a response so the tests read the triage result directly."""

    status_code = 200

    def __init__(self, view: dict) -> None:
        self.view = view  # the whole DisputeView returned by GET /v1/disputes/{id}
        self.text = json.dumps(view)

    def json(self) -> dict:
        return self.view["result"]


def wait_until_done(client, dispute_id: str, headers: dict) -> dict:
    """Poll the status endpoint until the run is over, as a client app would."""
    for _ in range(500):
        view = client.get(f"{URL}/{dispute_id}", headers=headers).json()
        if view["execution_status"] in ("finished", "failed"):
            return view
        time.sleep(0.01)
    raise AssertionError(f"dispute {dispute_id} never finished: {view}")


def triage(script, specialists=None, headers=None, **app_options):
    """Submit one dispute and wait for its result. Returns (outcome, scripted model, fake specialists).

    A request refused at intake (401, 404) is returned as the raw response instead.
    """
    model = ScriptedChatModel(script=script)
    specialists = specialists or FakeSpecialists()
    app = create_app(auth=SETTINGS, model=model, specialists=specialists, tracing=Tracing(),
                     policy=RefundPolicyConfig(), gate=InMemoryGate(), store=InMemoryDisputeStore(),
                     signer=SIGNER, delegation=DELEGATION, retry_delay_seconds=0, **app_options)
    auth_headers = bearer("user-1001") if headers is None else headers
    with TestClient(app) as client:
        submitted = client.post(URL, json=DISPUTE, headers=auth_headers)
        if submitted.status_code != 202:
            return submitted, model, specialists
        view = wait_until_done(client, submitted.json()["dispute_id"], auth_headers)
    return Outcome(view), model, specialists


def failed_checks(body: dict) -> set[str]:
    return {check["name"] for check in body["approval"]["checks"] if not check["passed"]}


# --- Outcomes -----------------------------------------------------------------------------------


def test_small_clear_refund_is_auto_approved():
    response, _, _ = triage(HAPPY)
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "refund_paid"  # approved by the policy, then confirmed by the ledger
    assert body["verdict"]["decision"] == "refund_recommended"
    assert (body["approval"]["route"], body["approval"]["approved_amount"]) == ("auto_approved", "50000.00")
    assert (body["payment"]["amount"], body["payment"]["idempotency_key"]) == (
        "50000.00", f"dispute:{body['dispute_id']}")
    assert [step.split(":")[0].split(" ->")[0] for step in body["steps"]] == [
        "supervisor", "ledger_agent", "supervisor", "fraud_agent", "supervisor", "verdict", "policy", "pay"]


def test_settled_transfer_needs_no_fraud_assessment():
    script = [route("ledger_agent"), route("finish"), verdict("no_action", None)]
    response, _, specialists = triage(script, FakeSpecialists(status="settled", credited="50000.00"))
    body = response.json()

    assert (body["status"], body["verdict"]["decision"], body["approval"]) == ("closed_no_refund", "no_action", None)
    assert specialists.fraud_calls == 0  # the supervisor skipped an agent it did not need


def test_refund_over_the_limit_goes_to_a_human():
    script = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict(refund_amount="450000.00")]
    response, _, _ = triage(script, FakeSpecialists(debited="450000.00"))
    body = response.json()

    assert (body["status"], body["approval"]["route"]) == ("pending_human_approval", "human_required")
    assert failed_checks(body) == {"under_amount_limit", "under_refund_total_limit"}


def test_no_action_cannot_close_a_dispute_where_money_is_missing():
    # The model says "no action" although the ledger shows 50.000 debited and never credited.
    script = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict("no_action", None)]
    response, _, _ = triage(script)
    body = response.json()

    assert body["status"] == "pending_human_approval"
    assert "ledger shows a failed transfer with money missing" in body["escalation_reason"]
    assert "marked for review by a person" in body["customer_message"]


def test_customer_message_is_built_from_ledger_and_policy_facts():
    response, _, _ = triage(HAPPY)
    assert response.json()["customer_message"] == (
        "The transfer is marked as failed: 50000.00 COP was debited from your account and 0.00 COP reached "
        "the recipient. A refund of 50000.00 COP has been paid back to your account.")


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


def test_unexpected_error_is_retried_once_then_goes_to_a_person():
    class BrokenSpecialists(FakeSpecialists):
        async def reconcile_ledger(self, dispute, token, traceparent=None):
            self.ledger_calls += 1
            raise RuntimeError("bug in our own code")

    outcome, _, specialists = triage([route("ledger_agent")], BrokenSpecialists())  # asks for the ledger each time

    assert specialists.ledger_calls == 2  # the first delivery, and one retry
    assert (outcome.view["execution_status"], outcome.view["status"]) == ("failed", "pending_human_approval")
    assert outcome.view["result"] is None
    assert "marked for review by a person" in outcome.view["customer_message"]  # never "failed" to the customer


# --- Identity ---------------------------------------------------------------------------------------


def test_agents_get_a_token_issued_by_the_supervisor_not_the_customers():
    headers = bearer("user-1001")
    customer_token = headers["Authorization"].removeprefix("Bearer ")
    response, model, specialists = triage(HAPPY, headers=headers)

    assert len(set(specialists.tokens_received)) == 1  # one token per run, used for both agents
    issued = specialists.tokens_received[0]
    assert issued != customer_token  # the customer's login token is never forwarded

    identity = verify_token(issued, INTERNAL_SETTINGS)
    assert (identity.user_id, identity.delegated_by, identity.transaction_id) == ("user-1001", "supervisor", TX)
    assert (identity.expires_at - datetime.now(UTC)).total_seconds() <= DELEGATION.lifetime_seconds

    shown = model.everything_shown_to_model()
    for secret in (customer_token, issued, "user-1001"):
        assert secret not in shown and secret not in response.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not.a.token"}])
def test_unauthenticated_request_never_reaches_the_model(headers):
    response, model, _ = triage(HAPPY, headers=headers)
    assert response.status_code == 401
    assert model.seen == []


def test_disputing_someone_elses_transaction_is_404_without_calling_the_model():
    response, model, specialists = triage(HAPPY, FakeSpecialists(owns=False))
    assert response.status_code == 404
    assert model.seen == [] and specialists.ledger_calls == 0


# --- The stored dispute -------------------------------------------------------------------------------


def test_the_two_statuses_are_reported_separately():
    script = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict(refund_amount="450000.00")]
    outcome, _, _ = triage(script, FakeSpecialists(debited="450000.00"))

    # The run finished; the customer's dispute is still waiting for a person.
    assert (outcome.view["execution_status"], outcome.view["status"]) == ("finished", "pending_human_approval")
    assert outcome.view["customer_message"] == outcome.json()["customer_message"]
    assert outcome.view["dispute_id"] == outcome.json()["dispute_id"] == outcome.json()["verdict"]["dispute_id"]


# --- Paying the approved refund (ADR-0020) ---------------------------------------------------------


def test_paid_only_when_the_ledger_confirms_and_the_amount_is_the_policys():
    outcome, _, specialists = triage(HAPPY)
    (user_id, transaction_id, amount, key), = specialists.pay_calls

    assert (user_id, transaction_id, amount) == ("user-1001", TX, Decimal("50000.00"))  # the policy's amount
    assert key == f"dispute:{outcome.view['dispute_id']}"
    assert outcome.view["status"] == "refund_paid"
    assert outcome.json()["payment"]["refund_id"] == specialists.paid[key].refund_id


def test_a_refund_the_ledger_refuses_goes_to_a_person_and_is_not_retried():
    outcome, _, specialists = triage(HAPPY, FakeSpecialists(pay="refused"))

    assert len(specialists.pay_calls) == 1  # a definite no: asking again would get the same answer
    assert (outcome.view["execution_status"], outcome.view["status"]) == ("finished", "pending_human_approval")
    assert "amount_mismatch" in outcome.json()["escalation_reason"]
    assert outcome.json()["payment"] is None
    assert "could not be paid automatically" in outcome.view["customer_message"]
    assert "has been paid" not in outcome.view["customer_message"]


def test_no_answer_from_the_ledger_is_retried_without_asking_the_model_again():
    outcome, model, specialists = triage(HAPPY, FakeSpecialists(pay_failures=1))

    assert outcome.view["status"] == "refund_paid"
    assert len(specialists.pay_calls) == 2 and len({call[3] for call in specialists.pay_calls}) == 1  # same key
    assert len(specialists.paid) == 1  # paid once
    # The retry resumed from the saved approval: the agents and the model were not asked again.
    assert specialists.ledger_calls == specialists.fraud_calls == 1
    assert len(model.seen) == len(HAPPY)


def test_a_payment_still_unknown_after_the_last_delivery_goes_to_a_person():
    outcome, _, specialists = triage(HAPPY, FakeSpecialists(pay="down"))

    assert len(specialists.pay_calls) == 2  # one attempt and one retry, then stop
    assert outcome.view["status"] == "pending_human_approval"
    reason = outcome.json()["escalation_reason"]
    assert "did not answer" in reason and f"dispute:{outcome.view['dispute_id']}" in reason  # where to look
    assert "has been paid" not in outcome.view["customer_message"]
