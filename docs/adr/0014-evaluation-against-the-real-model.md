# ADR-0014: Evaluate the deployed system against the real model, scored from its traces

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M8 (built early, during M5)

## Context
The unit tests replace the model with a scripted stand-in. They prove that our code behaves
correctly whatever the model does; they say nothing about how well the model performs. The one
serious model mistake so far (LEARNINGS, entry 1) was found by running scenarios by hand and
reading the results. That is not repeatable, and it does not give numbers to compare before
and after a change to a prompt, a model, or a limit.

## Decision
- **A scenario file** (`evals/scenarios.json`): nine disputes with the expected HTTP status,
  dispute status, decision, policy route, and required tool calls. Seven are the fixture
  scenarios; two are adversarial (a prompt injection, and another customer's transaction).
- **A runner** (`evals/run.py`, `make eval` / `make eval-cluster`) sends each dispute to a
  running supervisor as the synthetic customer, then reads what the run actually did from its
  Langfuse trace: tool calls with arguments and results, tokens, and cost.
- **Scoring** (`evals/scoring.py`, pure functions with their own unit tests), per scenario:

  | Metric | Definition |
  |---|---|
  | `task_success` | HTTP status, dispute status, decision, and policy route all equal the expected values. HTTP 200 with the wrong outcome is a failure. |
  | `tool_call_correct` | Every required call was made with the expected arguments, **and** no call used an unknown tool or looked up a different transaction. |
  | `latency_ms` | Wall-clock time of the request. |
  | `cost_usd`, `tokens` | Summed over every model call in the trace, so retries and both agents are included. |
  | `groundedness` | *Numeric* groundedness: the share of numbers the models wrote (signals, rationale, summary, explanation) that appear in the request or in the tool results of that run. |

- **Summary metrics** include **cost per success**: total cost of all attempts divided by the
  number of successful scenarios (LEARNINGS, Part 2, entry B).
- **A partial trace is not scored silently.** Each service sends its part of a trace separately,
  so the runner waits until every agent run it expects (from the result's `steps`) has arrived
  and the count has stopped changing; otherwise the run is flagged incomplete.
- **Exit status 1** unless task success and tool correctness are 100% and every trace was
  complete, so the run can gate a deploy.
- **Baseline** (commit `13f34b6`, deployed on AKS, gpt-4o 2024-11-20): 9 of 9 scenarios, tool
  calls 100%, numeric groundedness 100%, $0.1284 total, $0.0143 per success, median latency
  8.5 s. The report is committed under `evals/results/`.

## Consequences
- + A change to a prompt, model, or limit can be judged by numbers, on the deployed system.
- + The evaluation reads the same traces operations would read, so it also tests the tracing.
- - Nine scenarios with one run each is a smoke-level evaluation, not a measure of error rate:
  it cannot distinguish a 1% failure rate from 0%.
- - Numeric groundedness is a proxy. It catches an invented amount, score, or count; it says
  nothing about a false claim that contains no number, and it would flag a correct figure the
  model derived by arithmetic.
- - A run costs about $0.13 and takes about three minutes, most of it waiting for traces.

## Production delta
Hundreds of scenarios sampled from real (anonymised) disputes, including ambiguous and
conflicting evidence; several runs per scenario to measure decision agreement; groundedness of
non-numeric claims judged by a second model with human spot checks; datasets and experiments
tracked in Langfuse over time; the evaluation as a required check before a deploy and on a
nightly schedule; online metrics (escalation rate, reversed automatic decisions) alongside it.
