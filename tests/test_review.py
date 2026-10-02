"""Review by a person (ADR-0027): only a reviewer can decide, the amount always comes from the
ledger, an approval is paid by the same payer exactly once, and a dispute is decided only once."""

import time

import pytest
from fastapi.testclient import TestClient

from services.supervisor.dedup import InMemoryGate
from services.supervisor.main import create_app
from services.supervisor.store import InMemoryDisputeStore
from shared.refund_policy import RefundPolicyConfig
from shared.tracing import Tracing
from tests.fakes import ScriptedChatModel
from tests.jwt_helpers import DELEGATION, SETTINGS, SIGNER, bearer, claims, sign
from tests.test_supervisor import DISPUTE, URL, FakeSpecialists, route, verdict

# 450.000 COP is over the automatic limit: the policy sends it to a person.
HUMAN_OK = {"groundedness": True, "completeness": True, "clarity": True}
TO_A_PERSON = [route("ledger_agent"), route("fraud_agent"), route("finish"), verdict(refund_amount="450000.00")]


def reviewer_headers(sub="ops-ana", roles=("dispute-reviewer",)) -> dict:
    return {"Authorization": f"Bearer {sign(claims(sub=sub, roles=list(roles)))}"}


class Bank:
    """The intake API with its in-process worker and payer, a customer, and a reviewer."""

    def __init__(self, specialists=None):
        self.specialists = specialists or FakeSpecialists(debited="450000.00")
        self.app = create_app(auth=SETTINGS, model=ScriptedChatModel(script=TO_A_PERSON), specialists=self.specialists,
                              tracing=Tracing(), policy=RefundPolicyConfig(), gate=InMemoryGate(),
                              store=InMemoryDisputeStore(), signer=SIGNER, delegation=DELEGATION, retry_delay_seconds=0)

    def __enter__(self):
        self.http = TestClient(self.app).__enter__()
        return self

    def __exit__(self, *exc):
        self.http.__exit__(*exc)

    def wait(self, dispute_id, until):
        for _ in range(500):
            view = self.http.get(f"{URL}/{dispute_id}", headers=bearer("user-1001")).json()
            if until(view):
                return view
            time.sleep(0.01)
        raise AssertionError(f"never reached: {view}")

    def dispute_waiting_for_a_person(self) -> str:
        dispute_id = self.http.post(URL, json=DISPUTE, headers=bearer("user-1001")).json()["dispute_id"]
        self.wait(dispute_id, lambda v: v["status"] == "pending_human_approval")
        return dispute_id

    def decide(self, dispute_id, decision, note="checked the ledger and the risk signals", headers=None):
        return self.http.post(f"/v1/reviews/disputes/{dispute_id}/decision", json={"decision": decision, "note": note},
                              headers=headers or reviewer_headers())


def test_the_review_queue_shows_what_waits_for_a_person():
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        queue = bank.http.get("/v1/reviews/queue", headers=reviewer_headers()).json()

    assert [item["dispute"]["dispute_id"] for item in queue] == [dispute_id]
    item = queue[0]
    assert item["customer_id"] == "user-1001" and item["request"]["transaction_id"] == DISPUTE["transaction_id"]
    assert "under_amount_limit" in {c["name"] for c in item["dispute"]["result"]["approval"]["checks"] if not c["passed"]}


def test_an_approval_pays_the_ledgers_amount_through_the_same_payer_exactly_once():
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        decided = bank.decide(dispute_id, "approve")
        paid = bank.wait(dispute_id, lambda v: v["status"] == "refund_paid")
        twice = bank.decide(dispute_id, "approve")

    assert decided.status_code == 200 and decided.json()["status"] in ("refund_approved", "refund_paid")
    result = paid["result"]
    assert (result["approval"]["route"], result["approval"]["approved_by"]) == ("human_approved", "ops-ana")
    assert result["approval"]["approved_amount"] == result["payment"]["amount"] == "450000.00"  # the ledger's
    assert result["review"]["note"] == "checked the ledger and the risk signals"
    assert len(bank.specialists.paid) == 1  # one payment
    assert twice.status_code == 409  # a dispute is decided once


def test_a_rejection_closes_the_dispute_and_tells_the_customer():
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        decided = bank.decide(dispute_id, "reject", note="the recipient confirmed receipt by phone")
        view = bank.http.get(f"{URL}/{dispute_id}", headers=bearer("user-1001")).json()

    assert decided.status_code == 200 and view["status"] == "rejected"
    assert view["customer_message"].endswith("A person reviewed your dispute and decided that no refund is due.")
    assert view["result"]["review"]["reviewer_id"] == "ops-ana"
    assert bank.specialists.pay_calls == []


@pytest.mark.parametrize("headers", [
    bearer("user-1001"),  # a customer, even the dispute's own
    reviewer_headers(roles=()),  # an employee without the reviewer role
    reviewer_headers(roles=("dispute-viewer",)),
    {},
])
def test_only_a_reviewer_can_see_the_queue_or_decide(headers):
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        assert bank.http.get("/v1/reviews/queue", headers=headers).status_code == 401
        assert bank.decide(dispute_id, "approve", headers=headers or {"X": "y"}).status_code == 401
        assert bank.specialists.pay_calls == []


def test_a_reviewer_cannot_choose_the_amount_or_skip_the_note():
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        url = f"/v1/reviews/disputes/{dispute_id}/decision"
        with_amount = bank.http.post(url, json={"decision": "approve", "note": "pay them", "amount": "9000000.00"},
                                     headers=reviewer_headers())
        no_note = bank.http.post(url, json={"decision": "approve", "note": ""}, headers=reviewer_headers())
    assert with_amount.status_code == no_note.status_code == 422
    assert bank.specialists.pay_calls == []


def test_approving_when_the_ledger_shows_nothing_owed_is_refused():
    specialists = FakeSpecialists(debited="450000.00")
    with Bank(specialists) as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        # Meanwhile the money was returned by another route: the ledger now shows nothing owed.
        specialists.ledger = type(specialists.ledger).model_validate(
            specialists.ledger.model_dump() | {"settlement_status": "reversed", "credited_amount": "450000.00"})
        refused = bank.decide(dispute_id, "approve")
    assert refused.status_code == 409 and "nothing owed" in refused.json()["detail"]
    assert bank.specialists.pay_calls == []


def test_a_customer_still_cannot_read_another_customers_dispute():
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        assert bank.http.get(f"{URL}/{dispute_id}", headers=bearer("user-1002")).status_code == 404


def test_customer_service_sees_and_resolves_what_the_judge_flagged():
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        store = bank.app.state.store
        import asyncio as _asyncio
        from uuid import UUID as _UUID

        _asyncio.run(store.flag_follow_up(_UUID(dispute_id), "groundedness: invented cause"))
        open_ = bank.http.get("/v1/reviews/follow-ups", headers=reviewer_headers()).json()
        customer = bank.http.get("/v1/reviews/follow-ups", headers=bearer("user-1001"))
        done = bank.http.post(f"/v1/reviews/follow-ups/{dispute_id}/resolve",
                              json={"note": "called the customer and corrected it", "human_verdict": HUMAN_OK},
                              headers=reviewer_headers())
        again = bank.http.post(f"/v1/reviews/follow-ups/{dispute_id}/resolve",
                               json={"note": "a second time", "human_verdict": HUMAN_OK}, headers=reviewer_headers())
        after = bank.http.get("/v1/reviews/follow-ups", headers=reviewer_headers()).json()

    assert [(f["dispute"]["dispute_id"], f["reason"]) for f in open_] == [(dispute_id, "groundedness: invented cause")]
    assert customer.status_code == 401  # customer service is reviewers only
    assert done.status_code == 200 and done.json()["resolved_by"] == "ops-ana"
    assert again.status_code == 409 and after == []


def test_a_customer_service_check_is_recorded_internally_and_invisible_to_the_customer():
    """The judge's follow-up is validation only: the system records it (follow-up queue, audit
    trail), but the customer still sees their outcome as it was, and never the agent who checked."""
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        assert bank.decide(dispute_id, "approve").status_code == 200  # ops-ana approves: shown to the customer
        before = bank.wait(dispute_id, lambda v: v["status"] == "refund_paid")

        import asyncio as _asyncio
        from uuid import UUID as _UUID

        _asyncio.run(bank.app.state.store.flag_follow_up(_UUID(dispute_id), "clarity: code-like text"))
        bank.http.post(f"/v1/reviews/follow-ups/{dispute_id}/resolve", json={"note": "explanation checked, fine", "human_verdict": HUMAN_OK},
                       headers=reviewer_headers(sub="ops-luis"))
        after = bank.http.get(f"{URL}/{dispute_id}", headers=bearer("user-1001")).json()
        internal = bank.http.get(f"/v1/reviews/disputes/{dispute_id}", headers=reviewer_headers()).json()

    assert after == before  # the customer's view is exactly as it was: still paid
    shown = str(after)
    assert "ops-luis" not in shown and "customer service" not in shown and "clarity" not in shown
    assert after["result"]["review"]["reviewer_id"] == "ops-ana"  # the approval is still shown
    notes = [e["note"] for e in internal["events"]]  # the system knows, in the audit trail
    assert "sent to customer service: clarity: code-like text" in notes
    assert "customer service follow-up done by ops-luis: explanation checked, fine" in notes



def test_resolving_requires_the_persons_three_answers_and_they_become_a_label():
    with Bank() as bank:
        dispute_id = bank.dispute_waiting_for_a_person()
        import asyncio as _asyncio
        from uuid import UUID as _UUID

        _asyncio.run(bank.app.state.store.flag_follow_up(_UUID(dispute_id), "groundedness: invented cause"))
        url = f"/v1/reviews/follow-ups/{dispute_id}/resolve"
        missing = bank.http.post(url, json={"note": "looked at it"}, headers=reviewer_headers())
        partial = bank.http.post(url, json={"note": "looked at it", "human_verdict": {"groundedness": True}},
                                 headers=reviewer_headers())
        done = bank.http.post(url, json={"note": "the cause is in the record",
                                         "human_verdict": {"groundedness": True, "completeness": True,
                                                           "clarity": False}}, headers=reviewer_headers())
        labels = bank.http.get("/v1/reviews/human-labels", headers=reviewer_headers()).json()
        customer = bank.http.get("/v1/reviews/human-labels", headers=bearer("user-1001"))

    assert missing.status_code == partial.status_code == 422 and done.status_code == 200
    assert [(l["dispute_id"], l["human_verdict"]["clarity"]) for l in labels] == [(dispute_id, False)]
    assert customer.status_code == 401
