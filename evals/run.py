"""Run the evaluation scenarios against a running supervisor and the real model.

For each scenario: log in as the synthetic customer, send the dispute, then read what the run
actually did (tool calls, tokens, cost) from its Langfuse trace, and score it.

Usage:  make eval            # services running locally (make run-*)
        make eval-cluster    # the deployed system, through a port-forward
Exits with status 1 if any scenario fails, so it can gate a deploy.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from evals.scoring import evaluate, summarize

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from demo_token import issue  # noqa: E402  (the demo identity provider)

ROOT = Path(__file__).resolve().parent


def expected_runs(body: dict) -> dict[str, int]:
    """How many top-level runs the trace must contain, judged from the path the triage reported."""
    steps = body.get("steps", [])

    def answered(agent: str) -> int:
        return sum(1 for step in steps if step.startswith(agent) and not step.endswith("unavailable"))

    return {"dispute-triage": 1, "ledger-agent": answered("ledger_agent"), "fraud-agent": answered("fraud_agent")}


def fetch_trace(http: httpx.Client, trace_id: str, expected: dict[str, int]) -> tuple[list[dict], bool]:
    """All observations of a trace, and whether the trace is complete.

    Each service sends its part of the trace separately and Langfuse ingests asynchronously, so
    an early read is silently partial (fewer tool calls, lower cost). Wait until every expected
    run has arrived and the count has stopped changing. If that never happens, say so instead of
    scoring a partial trace.
    """
    previous, observations = -1, []
    for _ in range(40):
        time.sleep(3)
        response = http.get("/api/public/v2/observations",
                            params={"traceId": trace_id, "limit": 500, "fields": "core,basic,usage,model,io"})
        response.raise_for_status()
        observations = response.json().get("data", [])
        names = [o.get("name") for o in observations]
        arrived = all(names.count(name) >= count for name, count in expected.items())
        if arrived and len(observations) == previous:
            return observations, True
        previous = len(observations)
    return observations, False


def _tool_call(observation: dict) -> dict:
    try:
        args = json.loads(observation.get("input") or "{}")
    except (TypeError, ValueError):
        args = {}
    return {"name": observation.get("name"), "args": args if isinstance(args, dict) else {},
            "output": str(observation.get("output") or "")}


def run_scenario(supervisor: httpx.Client, langfuse: httpx.Client, scenario: dict) -> dict:
    token = issue(scenario["user_id"])
    started = time.monotonic()
    response = supervisor.post("/v1/disputes/triage", json=scenario["request"],
                               headers={"Authorization": f"Bearer {token}"})
    elapsed_ms = round((time.monotonic() - started) * 1000)
    try:
        body = response.json()
    except ValueError:
        body = {}

    observations, complete = [], True
    trace_url = body.get("trace_url") if isinstance(body, dict) else None
    if trace_url:
        observations, complete = fetch_trace(langfuse, trace_url.rsplit("/", 1)[1], expected_runs(body))
    generations = [o for o in observations if o.get("type") == "GENERATION"]
    return {
        "http_status": response.status_code,
        "body": body,
        "elapsed_ms": elapsed_ms,
        "tool_calls": [_tool_call(o) for o in observations if o.get("type") == "TOOL"],
        "model_calls": len(generations),
        "total_cost_usd": sum(g.get("totalCost") or 0 for g in generations),
        "total_tokens": sum((g.get("usageDetails") or {}).get("total", 0) or 0 for g in generations),
        "trace_url": trace_url,
        "trace_complete": complete,
    }


def _fmt(value, pattern="{:.0%}") -> str:
    return "n/a" if value is None else pattern.format(value)


def markdown_report(results: list[dict], summary: dict, meta: dict) -> str:
    lines = [
        f"# Evaluation run {meta['date']}",
        "",
        f"- Commit: `{meta['commit']}`  |  Target: `{meta['target']}`  |  Model: `{meta['model']}`",
        f"- Scenarios: {summary['scenarios']}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Task success | {_fmt(summary['task_success_rate'])} |",
        f"| Tool calls correct | {_fmt(summary['tool_call_correct_rate'])} |",
        f"| Numeric groundedness (mean) | {_fmt(summary['mean_groundedness'])} |",
        f"| Successful scenarios | {summary['successes']} of {summary['scenarios']} |",
        f"| Agent calls that were retries | {summary['agent_retries']} |",
        f"| Total spending (all attempts, retries included) | ${summary['total_cost_usd']:.4f} |",
        f"| Cost per request | {_fmt(summary['cost_per_request_usd'], '${:.4f}')} |",
        f"| **Cost per success** | {_fmt(summary['cost_per_success_usd'], '${:.4f}')} |",
        f"| Total tokens | {summary['total_tokens']:,} |",
        f"| Latency, median / max | {summary['median_latency_ms'] / 1000:.1f} s / {summary['max_latency_ms'] / 1000:.1f} s |",
        "",
        "| Scenario | Success | Tools | Grounded | Latency | Cost | Outcome |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        a = r["actual"]
        outcome = (f"{a['status']}, {a['decision']}, policy {a['policy_route']}" if a["http_status"] == 200
                   else f"HTTP {a['http_status']}")
        lines.append(
            f"| {r['id']} | {'yes' if r['task_success'] else '**NO**'} | {'yes' if r['tool_call_correct'] else '**NO**'} "
            f"| {_fmt(r['groundedness'])} | {r['latency_ms'] / 1000:.1f} s | ${r['cost_usd']:.4f} | {outcome} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8004", help="supervisor base URL")
    parser.add_argument("--scenarios", default=str(ROOT / "scenarios.json"))
    parser.add_argument("--label", default="local", help="what is being evaluated, for the report")
    args = parser.parse_args()

    scenarios = json.loads(Path(args.scenarios).read_text())
    langfuse = httpx.Client(base_url=os.environ["LANGFUSE_BASE_URL"], timeout=30,
                            auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]))
    supervisor = httpx.Client(base_url=args.url, timeout=120)

    results = []
    for scenario in scenarios:
        run = run_scenario(supervisor, langfuse, scenario)
        result = evaluate(run, scenario)
        results.append(result)
        print(f"{'PASS' if result['task_success'] else 'FAIL'}  {scenario['id']:32} "
              f"tools={'ok' if result['tool_call_correct'] else 'WRONG'}  "
              f"grounded={_fmt(result['groundedness'])}  {result['latency_ms'] / 1000:5.1f}s  "
              f"${result['cost_usd']:.4f}  {result['tokens']:>6} tokens")
        if not result["task_success"]:
            print(f"      expected {scenario['expected']}\n      actual   {result['actual']}")
        if not result["trace_complete"]:
            print("      WARNING: the trace did not finish arriving; tool calls and cost may be understated")

    summary = summarize(results)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    meta = {"date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"), "commit": commit, "target": args.label,
            "model": f"{os.environ.get('AOAI_DEPLOYMENT', '?')} {os.environ.get('AOAI_MODEL_VERSION', '')}".strip()}

    stem = ROOT / "results" / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M')}-{commit}-{args.label}"
    stem.with_suffix(".json").write_text(json.dumps({"meta": meta, "summary": summary, "results": results}, indent=2))
    stem.with_suffix(".md").write_text(markdown_report(results, summary, meta))

    print(f"\ntask success {_fmt(summary['task_success_rate'])}  |  tools correct {_fmt(summary['tool_call_correct_rate'])}"
          f"  |  groundedness {_fmt(summary['mean_groundedness'])}")
    print(f"successes {summary['successes']}/{summary['scenarios']}  |  agent retries {summary['agent_retries']}")
    print(f"total ${summary['total_cost_usd']:.4f}  |  per request {_fmt(summary['cost_per_request_usd'], '${:.4f}')}"
          f"  |  per success {_fmt(summary['cost_per_success_usd'], '${:.4f}')}")
    print(f"report: {stem.with_suffix('.md').relative_to(ROOT.parent)}")
    complete = all(r["trace_complete"] for r in results)
    return 0 if summary["task_success_rate"] == 1 and summary["tool_call_correct_rate"] == 1 and complete else 1


if __name__ == "__main__":
    sys.exit(main())
