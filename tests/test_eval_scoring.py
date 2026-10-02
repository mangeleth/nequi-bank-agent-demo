"""The evaluator is itself code that can be wrong, so its scoring rules are tested."""

from decimal import Decimal

import pytest

from evals.scoring import agent_retries, evaluate, numbers_in, numeric_groundedness, summarize, tool_calls_correct

TX = "TX-20261001000001"
LEDGER_OUTPUT = '{"amount": "50000.00", "debited_amount": "50000.00", "credited_amount": "0.00"}'
RISK_OUTPUT = '{"engine_score": 0.08, "recipient_account_age_days": 900, "amount_vs_customer_avg": 0.9}'

SCENARIO = {
    "id": "small-failed-transfer",
    "request": {"transaction_id": TX, "reason": "failed_transfer", "claimed_amount": "50000.00", "description": ""},
    "expected": {
        "http_status": 200, "status": "refund_approved", "decision": "refund_recommended", "policy_route": "auto_approved",
        "required_tool_calls": [{"name": "get_transaction", "args": {"transaction_id": TX}},
                                {"name": "get_risk_signals", "args": {"transaction_id": TX}}],
    },
}


def call(name: str, tx: str = TX, output: str = "") -> dict:
    return {"name": name, "args": {"transaction_id": tx}, "output": output}


def run(**overrides) -> dict:
    base = {
        "http_status": 200,
        "execution_status": "finished",
        "dispute_status": "refund_approved",
        "accept_ms": 300,
        "elapsed_ms": 9000,
        "total_cost_usd": 0.019,
        "total_tokens": 5700,
        "tool_calls": [call("get_transaction", output=LEDGER_OUTPUT), call("get_risk_signals", output=RISK_OUTPUT)],
        "body": {
            "status": "refund_approved",
            "verdict": {"decision": "refund_recommended", "explanation": "50,000.00 COP was debited and 0.00 credited."},
            "approval": {"route": "auto_approved"},
            "fraud": {"signals": ["Engine score 0.08", "Recipient account is 900 days old"], "rationale": "Low risk."},
            "ledger": {"summary": "The transfer of 50000.00 COP failed."},
        },
    }
    return base | overrides


def test_a_correct_run_scores_fully():
    result = evaluate(run(), SCENARIO)
    assert (result["task_success"], result["tool_call_correct"], result["groundedness"]) == (True, True, 1.0)
    assert (result["cost_usd"], result["tokens"], result["latency_ms"]) == (0.019, 5700, 9000)


@pytest.mark.parametrize("body_change", [
    {"status": "pending_human_approval"},
    {"verdict": {"decision": "no_action", "explanation": "x"}},
    {"approval": {"route": "human_required"}},
    {"approval": None},
])
def test_any_wrong_outcome_field_fails_the_task(body_change):
    assert evaluate(run(body=run()["body"] | body_change), SCENARIO)["task_success"] is False


def test_a_wrong_customer_message_fails_the_task():
    scenario = SCENARIO | {"expected": SCENARIO["expected"] | {"customer_message_contains": "has been approved"}}
    said_paid = run()["body"] | {"customer_message": "Your refund has been paid."}
    said_approved = run()["body"] | {"customer_message": "A refund of 50000.00 COP has been approved."}
    assert evaluate(run(body=said_paid), scenario)["task_success"] is False
    assert evaluate(run(body=said_approved), scenario)["task_success"] is True


def test_http_200_is_not_success_when_the_outcome_is_wrong():
    escalated = run()["body"] | {"status": "pending_human_approval", "approval": None, "escalation_reason": "x"}
    assert evaluate(run(body=escalated), SCENARIO)["task_success"] is False


def test_missing_required_tool_call_is_incorrect():
    assert tool_calls_correct([call("get_transaction")], SCENARIO["expected"], TX) is False


def test_looking_up_another_transaction_is_incorrect_even_if_required_calls_were_made():
    calls = [call("get_transaction"), call("get_risk_signals"), call("get_transaction", tx="TX-20261001000004")]
    assert tool_calls_correct(calls, SCENARIO["expected"], TX) is False


def test_an_unknown_tool_is_incorrect():
    calls = [call("get_transaction"), call("get_risk_signals"), call("transfer_money")]
    assert tool_calls_correct(calls, SCENARIO["expected"], TX) is False


def test_extra_allowed_lookups_of_the_same_transaction_are_fine():
    calls = [call("get_transaction"), call("get_risk_signals"), call("get_transaction"),
             {"name": "get_refund_history", "args": {"window_days": 30}, "output": ""}]
    assert tool_calls_correct(calls, SCENARIO["expected"], TX) is True


@pytest.mark.parametrize(("text", "expected"), [
    ("50,000.00 COP", {Decimal("50000")}),
    ("50000.00", {Decimal("50000")}),
    ("50.000 pesos", {Decimal("50"), Decimal("50000")}),  # Colombian thousands separator
    ("score 0.08.", {Decimal("0.08")}),
    ("no numbers here", set()),
])
def test_numbers_are_normalised(text, expected):
    assert numbers_in(text) == expected


def test_an_invented_number_lowers_groundedness():
    body = run()["body"] | {"ledger": {"summary": "The customer lost 75000.00 COP."}}
    result = evaluate(run(body=body), SCENARIO)
    assert result["groundedness"] < 1.0


def test_a_percentage_matches_the_fraction_in_the_source():
    assert numeric_groundedness("Risk is 8%.", RISK_OUTPUT) == 1.0


def test_groundedness_is_undefined_without_numbers():
    assert numeric_groundedness("The transfer failed.", LEDGER_OUTPUT) is None


def test_expected_refusal_scores_on_http_status_alone():
    scenario = SCENARIO | {"expected": {"http_status": 404}}
    refused = {"http_status": 404, "body": {"detail": "transaction not found"}, "elapsed_ms": 12,
               "tool_calls": [], "total_cost_usd": 0, "total_tokens": 0}
    result = evaluate(refused, scenario)
    assert (result["task_success"], result["tool_call_correct"], result["cost_usd"]) == (True, True, 0)
    assert evaluate(run(), scenario)["task_success"] is False  # a 200 where a refusal was expected


def test_cost_per_success_charges_failures_to_the_successes():
    results = [evaluate(run(), SCENARIO) for _ in range(6)]
    results.append(evaluate(run(body=run()["body"] | {"status": "pending_human_approval"}), SCENARIO))
    summary = summarize(results)

    assert summary["task_success_rate"] == pytest.approx(6 / 7)
    assert summary["cost_per_request_usd"] == pytest.approx(0.019)
    assert summary["cost_per_success_usd"] == pytest.approx(7 * 0.019 / 6)


def test_cost_per_success_is_undefined_when_nothing_succeeds():
    failed = evaluate(run(body={}), SCENARIO)
    assert summarize([failed])["cost_per_success_usd"] is None


def test_retries_are_counted_from_the_reported_path():
    steps = ["supervisor -> ledger_agent: x", "ledger_agent: failed, debited 1, credited 0",
             "supervisor -> fraud_agent: x", "fraud_agent: unavailable",
             "supervisor -> fraud_agent: x", "fraud_agent: risk low (0.08)", "verdict: refund_recommended"]
    assert agent_retries({"steps": steps}) == 1
    assert agent_retries({"steps": steps[:2]}) == 0
    assert agent_retries({}) == 0


def test_summary_reports_spending_and_cost_per_success_together():
    summary = summarize([evaluate(run(), SCENARIO)])
    assert {"successes", "agent_retries", "total_cost_usd", "cost_per_success_usd"} <= set(summary)


def test_a_duplicate_must_be_a_replay_and_a_first_submission_must_not():
    duplicate = SCENARIO | {"expected": SCENARIO["expected"] | {"replay": True, "required_tool_calls": []}}
    replayed = run(replayed=True, tool_calls=[], total_cost_usd=0, total_tokens=0)

    assert evaluate(replayed, duplicate)["task_success"] is True
    assert evaluate(run(), duplicate)["task_success"] is False  # the models ran again for a duplicate
    assert evaluate(replayed, SCENARIO)["task_success"] is False  # a first submission answered from a stale store
    assert evaluate(replayed, duplicate)["cost_usd"] == 0


def test_a_run_that_failed_is_not_a_success_even_with_a_polite_answer():
    assert evaluate(run(execution_status="failed"), SCENARIO)["task_success"] is False


def test_the_stored_dispute_must_agree_with_its_result():
    assert evaluate(run(dispute_status="pending_human_approval"), SCENARIO)["task_success"] is False


def test_accepted_with_202_is_scored_like_any_accepted_dispute():
    scenario = SCENARIO | {"expected": SCENARIO["expected"] | {"http_status": 202}}
    result = evaluate(run(http_status=202), scenario)
    assert (result["task_success"], result["groundedness"], result["accept_ms"]) == (True, 1.0, 300)


def _incident_run(model_calls: int, incident_id: str | None = "INC-20261001-01") -> dict:
    body = {"status": "refund_paid", "verdict": {"decision": "refund_recommended"},
            "approval": {"route": "auto_approved"},
            "customer_message": "This transfer was affected by a confirmed problem on our side: x.",
            "incident": {"incident_id": incident_id} if incident_id else None, "steps": []}
    return {"http_status": 202, "body": body, "execution_status": "finished", "dispute_status": "refund_paid",
            "tool_calls": [], "elapsed_ms": 900, "accept_ms": 300, "total_cost_usd": 0.0, "total_tokens": 0,
            "model_calls": model_calls}


INCIDENT_SCENARIO = {
    "id": "known-incident-fast-path",
    "request": {"transaction_id": "TX-20261001000009", "reason": "failed_transfer", "claimed_amount": "35000.00"},
    "expected": {"http_status": 202, "status": "refund_paid", "decision": "refund_recommended",
                 "policy_route": "auto_approved", "required_tool_calls": [],
                 "customer_message_contains": "confirmed problem on our side",
                 "incident_id": "INC-20261001-01", "max_model_calls": 0},
}


def test_the_fast_path_scenario_passes_only_with_zero_model_calls():
    assert evaluate(_incident_run(model_calls=0), INCIDENT_SCENARIO)["task_success"]
    assert not evaluate(_incident_run(model_calls=3), INCIDENT_SCENARIO)["task_success"]  # the agents ran


def test_the_fast_path_scenario_requires_the_incident_to_have_decided():
    assert not evaluate(_incident_run(model_calls=0, incident_id=None), INCIDENT_SCENARIO)["task_success"]
    assert evaluate(_incident_run(model_calls=0), INCIDENT_SCENARIO)["groundedness"] is None  # no model text
