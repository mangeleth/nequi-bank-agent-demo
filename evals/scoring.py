"""Scoring for evaluation runs against the real model (ADR-0014). Pure functions, no network.

A *run* is what one triage actually did; *expected* is what the scenario file says it should do.

    run = {
        "http_status": 200,
        "body": {...},                 # the TriageResult, or the error body
        "elapsed_ms": 9100,
        "tool_calls": [{"name": "get_transaction", "args": {...}, "output": "..."}],  # from the trace
        "total_cost_usd": 0.019,       # every model call in the trace, so retries are included
        "total_tokens": 5700,
    }
"""

import json
import re
from decimal import Decimal, InvalidOperation

ALLOWED_TOOLS = {"get_transaction", "get_risk_signals", "get_refund_history"}

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_DOT_THOUSANDS = re.compile(r"^\d{1,3}(?:\.\d{3})+$")  # 50.000 written the Colombian way


def numbers_in(text: str) -> set[Decimal]:
    """Every number written in `text`, normalised (50,000.00 -> 50000)."""
    found = set()
    for token in _NUMBER.findall(text):
        try:
            found.add(Decimal(token.replace(",", "")).normalize())
        except InvalidOperation:
            continue
        if _DOT_THOUSANDS.match(token):
            found.add(Decimal(token.replace(".", "")).normalize())
    return found


def numeric_groundedness(model_text: str, source_text: str) -> float | None:
    """Share of the numbers the model wrote that appear in the data it was given.

    A deterministic proxy for groundedness: it catches an invented amount, score, or count, and
    says nothing about claims that contain no number. A percentage counts as grounded when the
    source holds the same value as a fraction (86% and 0.86). None when the model wrote no numbers.
    """
    sources = numbers_in(source_text)
    stated = numbers_in(model_text)
    if not stated:
        return None
    grounded = sum(1 for n in stated if n in sources or (n / 100).normalize() in sources)
    return grounded / len(stated)


def _model_text(body: dict) -> str:
    """Everything the models wrote that reaches a customer or a reviewer."""
    fraud, ledger, verdict = body.get("fraud") or {}, body.get("ledger") or {}, body.get("verdict") or {}
    parts = [*fraud.get("signals", []), fraud.get("rationale", ""), ledger.get("summary", ""),
             verdict.get("explanation", "")]
    return "\n".join(part for part in parts if part)


def tool_calls_correct(calls: list[dict], expected: dict, transaction_id: str) -> bool:
    """Every required call was made, and nothing outside the dispute was looked up."""
    required = expected.get("required_tool_calls", [])
    made = [(call["name"], call["args"]) for call in calls]
    all_required_made = all((req["name"], req["args"]) in made for req in required)
    nothing_unexpected = all(
        call["name"] in ALLOWED_TOOLS and call["args"].get("transaction_id", transaction_id) == transaction_id
        for call in calls
    )
    return all_required_made and nothing_unexpected


def agent_retries(body: dict) -> int:
    """How many agent calls were repeats, read from the path the triage reported."""
    steps = body.get("steps", [])
    return sum(max(0, sum(1 for step in steps if step.startswith(agent)) - 1)
               for agent in ("ledger_agent", "fraud_agent"))


def evaluate(run: dict, scenario: dict) -> dict:
    """Score one run against its scenario."""
    expected, request = scenario["expected"], scenario["request"]
    body = run.get("body") or {}

    # A duplicate must be answered from the gate, and a first submission must not be.
    accepted = expected["http_status"] in (200, 202)
    task_success = (
        run["http_status"] == expected["http_status"]
        and run.get("replayed", False) == expected.get("replay", False)
    )
    if task_success and accepted:
        task_success = (
            # The run itself must have finished, and the stored dispute must agree with its result.
            run.get("execution_status") == "finished"
            and run.get("dispute_status") == body.get("status")
            and body.get("status") == expected["status"]
            and (body.get("verdict") or {}).get("decision") == expected["decision"]
            and (body.get("approval") or {}).get("route") == expected["policy_route"]
            and expected.get("customer_message_contains", "") in body.get("customer_message", "")
            # Which confirmed incident decided it, if any (ADR-0022). Absent = must be none.
            and ((body.get("incident") or {}).get("incident_id")) == expected.get("incident_id")
        )
    if task_success and "max_model_calls" in expected:
        # The fast path's whole point: a covered dispute is decided with zero model calls.
        task_success = run.get("model_calls", 0) <= expected["max_model_calls"]

    # What the model was given: the request and every tool result recorded in the trace.
    source_text = json.dumps(request) + "\n" + "\n".join(call.get("output", "") for call in run["tool_calls"])
    return {
        "id": scenario["id"],
        "task_success": task_success,
        "tool_call_correct": tool_calls_correct(run["tool_calls"], expected, request["transaction_id"]),
        "accept_ms": run.get("accept_ms", run["elapsed_ms"]),
        "latency_ms": run["elapsed_ms"],
        "cost_usd": run["total_cost_usd"],  # includes retries: every model call in the trace
        "tokens": run["total_tokens"],
        "agent_retries": agent_retries(body),
        "trace_complete": run.get("trace_complete", True),
        "replayed": run.get("replayed", False),
        "groundedness": (
            numeric_groundedness(_model_text(body), source_text)
            # n/a when no model wrote anything: a replay, or a dispute decided by an incident
            if accepted and not run.get("replayed", False) and not body.get("incident") else None
        ),
        "actual": {
            "http_status": run["http_status"],
            "execution_status": run.get("execution_status"),
            "status": body.get("status"),
            "decision": (body.get("verdict") or {}).get("decision"),
            "policy_route": (body.get("approval") or {}).get("route"),
            "customer_message": body.get("customer_message"),
            "incident_id": (body.get("incident") or {}).get("incident_id"),
            "model_calls": run.get("model_calls", 0),
            "escalation_reason": body.get("escalation_reason"),
        },
    }


def summarize(results: list[dict]) -> dict:
    """Totals for a whole evaluation run."""
    count = len(results)
    successes = sum(r["task_success"] for r in results)
    total_cost = sum(r["cost_usd"] for r in results)
    grounded = [r["groundedness"] for r in results if r["groundedness"] is not None]
    latencies = sorted(r["latency_ms"] for r in results)
    accepts = sorted(r["accept_ms"] for r in results)
    return {
        "scenarios": count,
        "task_success_rate": successes / count if count else None,
        "tool_call_correct_rate": sum(r["tool_call_correct"] for r in results) / count if count else None,
        "mean_groundedness": sum(grounded) / len(grounded) if grounded else None,
        "successes": successes,
        "agent_retries": sum(r["agent_retries"] for r in results),
        # Report spending and cost per success together: a retry policy raises the first and
        # can lower the second, and neither number alone shows whether it was worth it.
        "total_cost_usd": total_cost,
        "total_tokens": sum(r["tokens"] for r in results),
        "cost_per_request_usd": total_cost / count if count else None,
        # The cost of every attempt, failed ones included, charged to the successes.
        "cost_per_success_usd": total_cost / successes if successes else None,
        "median_accept_ms": accepts[count // 2] if count else None,
        "median_latency_ms": latencies[count // 2] if count else None,
        "max_latency_ms": latencies[-1] if count else None,
    }
