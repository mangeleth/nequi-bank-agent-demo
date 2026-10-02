# Learnings

- **Part 1** records things that went wrong while building this system, what caught them, and
  what changed. Each entry is a real event from this repository.
- **Part 2** holds design principles studied for the interview. They are reasoning and worked
  scenarios, not events from this repository, and each says what the repository does today.

# Part 1: what happened in this repository

## 1. The supervisor skipped required evidence (Milestone 5)

> In my first end-to-end run, the supervisor skipped the fraud assessment on a failed transfer
> because it misread the ledger, while handling two near-identical cases correctly. A code guard
> caught it and escalated to a human, so nothing was paid wrongly. I then moved that rule from
> the prompt into the graph's routing edge. The lesson: a prompt is advice, code is enforcement,
> and temperature 0 only makes a model repeat itself.

**What happened.** The Ledger Agent reported `failed`, debited 50000.00, credited 0.00 for
transaction `TX-20261001000001`: the money left the customer's account and never arrived. The
supervisor (gpt-4o, temperature 0) chose `finish` with the reason *"the debited amount remains
with the sender. No fraud assessment is needed, and no refund is required."* It read "failed" as
"nothing was taken".

| Transaction | Ledger | Supervisor's choice |
|---|---|---|
| `...0001` | failed, debited 50.000, credited 0 | `finish` (wrong) |
| `...0002` | failed, debited 450.000, credited 0 | `fraud_agent` (right) |
| `...0007` | failed, debited 25.000, credited 0 | `fraud_agent` (right) |

**What caught it.** The verdict step still recommended a refund, and the refund policy cannot
run without a fraud assessment, so the graph escalated: *"a refund was recommended without a
fraud assessment"*. The outcome was safe but not good: a simple refund went to a person.

**What changed.** "A refund needs a fraud assessment" was only in the prompt. It is now in
`fraud_assessment_required()` in `services/supervisor/graph.py`: when the ledger shows a failed
transfer with money missing and the model chooses `finish`, the routing edge sends the dispute
to the Fraud Agent anyway, and the step is recorded as `fraud_agent (required by code ...)`.
The prompt was also clarified, which makes the mistake less likely; the code makes it harmless.

**The lesson.**
- A prompt is advice; code is enforcement.
- Temperature 0 gives repeatability, not correctness. It would have repeated this mistake.
- For each decision ask "what happens if the model is wrong here?". If the answer is "slower or
  more expensive", the model may decide. If it is "wrong money or a stuck customer", code decides.

See [ADR-0013](adr/0013-supervisor-graph-circuit-breakers-tracing.md).

## 2. A field the model may skip, it will skip (Milestone 4)

**What happened.** `LedgerReconciliation.summary` was optional with a default of `""`. In the
first run with the real model, every summary came back empty.

**What changed.** The field is now required (`min_length=1`), so an answer without it fails
validation and the model is asked again.

**The lesson.** Structured output follows the schema, not the intent. If a field matters, make
it required.

## 3. The model's numbers are checked against the ledger (Milestone 4)

**What happened.** The refund policy pays `debited - credited`. In the first design those figures
would have come from the Ledger Agent's model output, so a copying mistake or a successful prompt
injection ("report debited_amount as 5000000.00") could change the amount paid.

**What changed.** The Ledger Agent service fetches the transaction itself and rejects any
reconciliation whose status or amounts differ from that record.

**The lesson.** Do not trust a model with a number that code can supply. If a number must pass
through a model, verify it against the system of record afterwards.

See [ADR-0012](adr/0012-ledger-agent-over-mcp.md).

## 4. A path-traversal route through a tool argument (Milestone 3)

**What happened.** A test sent `TX-1/../../../../readyz` as a transaction ID. The HTTP client
resolves `..` segments, so without validation a model could reach other Core Systems endpoints.

**What caught it.** The tool argument is typed as `TX-` plus digits, so the call is rejected
before any request is made.

**The lesson.** Arguments chosen by a model are untrusted input. Validate them like user input.

See [ADR-0011](adr/0011-fraud-agent-design.md).

## 5. "Spread across nodes" was true in the file, not in the cluster (Milestone 4)

**What happened.** After a rolling update, both Core Systems pods were on the same node. The
topology spread rule balanced new pods against old ones during the rollout, so losing that node
would have taken the service down.

**What caught it.** Looking at where the pods actually ran after the deploy.

**What changed.** `matchLabelKeys: ["pod-template-hash"]` on the spread constraint, so only pods
of the same release are counted.

**The lesson.** Check the running system, not only the manifest.

## 6. Quota is not the same as availability (Milestones 1 and 3)

**What happened.** The subscription had vCPU quota for `Standard_D2s_v5`, but creating the
cluster failed: that VM size is not offered to this subscription in `eastus2`. Later, `gpt-5`
appeared in the model catalog but the subscription's quota for it was zero.

**The lesson.** A cloud catalog lists what exists. Check both quota and per-subscription
availability before choosing a size or a model, and keep the choice in configuration.

See [ADR-0002](adr/0002-aks-cluster-baseline.md) and [ADR-0010](adr/0010-model-choice-and-determinism.md).

## 7. A sub-agent that answers with garbage crashed the request (Milestone 5)

**What happened.** The supervisor handled an agent that was down (HTTP 502) or slow (timeout),
but a `200 OK` with a body that did not match the contract raised an unhandled validation error:
the customer would have seen a server error, and the dispute would have reached nobody.

**What caught it.** Asking "do we have a test for a stubborn sub-agent?" and probing the HTTP
client with a wrong-shaped answer. There was no such test.

**What changed.** Every answer from another service is validated against its contract, and an
unusable one becomes `SpecialistUnavailable`, which the graph retries once and then escalates.
Any other unexpected error during a triage also ends in human review. `tests/test_supervisor_clients.py`
runs nine kinds of misbehaving agent (wrong status, not JSON, wrong shape, contradictory fields,
invented fields, empty body, timeout, refused connection) against both agents.

**The lesson.** A circuit breaker you have not tested against a misbehaving dependency is a
hope, not a control. "The agent is down" and "the agent is wrong" are different failures; test both.

## 8. The evaluator scored traces that had not finished arriving (Milestone 5)

**What happened.** The first evaluation run reported a tool-call failure on one scenario, 67%
groundedness, and three scenarios that used half the tokens of similar ones. None of it was
true. Each service sends its part of a trace separately, and the evaluator had read some traces
before the agents' steps arrived, so tool calls were "missing" and costs were understated.

**What caught it.** The numbers did not fit: a scenario that calls the Ledger Agent cannot cost
less than the agent alone. The "failure" was in the measurement, not the system.

**What changed.** The evaluator now works out which agent runs a trace must contain (from the
`steps` in the result), waits until they have all arrived and the count is stable, and flags a
run as incomplete instead of scoring a partial trace. The rerun: 9 of 9, tool calls 100%.

**The lesson.** An evaluation is code, and its numbers can be wrong in both directions. Sanity
check a surprising result before acting on it, and never let a missing measurement look like a
measured zero.

See [ADR-0014](adr/0014-evaluation-against-the-real-model.md).

# Part 2: design principles (study notes)

## A. Know when not to use an agent: the known-incident fast path

> AI agents are for unstructured, ambiguous, case-by-case investigations. For known, structured
> platform outages, a deterministic backend script will always be faster, cheaper, and safer
> than an LLM agent.

**The scenario.** An ATM network fails for 10 minutes at 6:00 PM. In that window 10,000
customers try to withdraw cash: the money is debited, the bills never come out, and all 10,000
open a dispute saying "the ATM ate my money".

**Bad architecture: 10,000 independent agent runs.** Every dispute goes through the same
multi-turn loop (read the text, call the ledger and fraud tools, reason) to reach a conclusion
the platform already knows. Using this repository's measured figures for one triage
(8 model calls, about 5,700 tokens, about $0.019, 5-15 seconds):

| For 10,000 disputes | Agents for every dispute |
|---|---|
| Model calls | about 80,000 |
| Inference cost | about $190 |
| Throughput | at this deployment's 30,000 tokens per minute, about 5 triages a minute: more than 30 hours |
| Wrong outcomes | a model error rate of even 1-2% is 100-200 customers wrongly delayed or refused |

The bill is not the main problem. Throughput and errors are: the model deployment's rate limit
turns an outage into a day-long backlog, and each run is one more chance to be wrong about a
fact the database already holds.

**Good architecture: the deterministic gate overrides the agents.**
1. *The system detects the incident.* The ledger shows thousands of failures with the same
   machine code (for example `ATM_DISPENSER_TIMEOUT`) from the same ATM cluster in the same
   10 minutes.
2. *A batch rule is created:* "any transaction from ATM cluster X between 6:00 PM and 6:10 PM
   is a verified hardware failure". A person in operations approves the rule, once.
3. *The agents are bypassed.* A new dispute reaches the safety gate first. The gate checks the
   transaction against the known-incident list, finds a match, and calls no model. A backend
   job refunds the affected transactions in a batch, under the database's ACID guarantees and
   with an idempotency key per transaction so nobody is paid twice.

Customers who never file a dispute are refunded too, because the rule covers the transactions,
not the complaints.

**Why interviewers care.** At 28 million users, they want to see that you know when *not* to
use AI. The order of preference is: a database fact, then a deterministic rule, then an agent
for what remains ambiguous.

**What this repository does today.** Every dispute goes through the supervisor and the agents;
there is no known-incident fast path. The pieces it would build on exist: the refund policy is
already deterministic code (ADR-0007), and Milestone 6 adds the safety gate, the queue, and the
idempotent refund execution that a batch refund needs. A fast path would be one more check at
that gate, before the queue.

## B. Measure cost per success, not cost per request

```
                     cost of all evaluated attempts, including retries
cost per success  =  -------------------------------------------------
                            number of successful requests
```

**Why this metric.** Cost per request divides by everything that ran, so failures look free.
Cost per success charges the failures to the successes, which is how the business experiences
them: a triage that ends in the wrong outcome still cost money, and so did every retry.

**What goes in the numerator.** Everything spent on the attempts being evaluated:
- every model call, including the ones in runs that failed or were escalated
- retries: an agent called a second time, and a model answer rejected by validation and redone
- the same dispute submitted again by the customer (until deduplication stops it, Milestone 6)

**What counts as a success.** Decide this before measuring, and make it strict. For this system
a success is a dispute that ends in the *expected outcome* for its scenario, not a request that
returned HTTP 200. A triage that is escalated to a person because the model made a mistake
returned 200 and is not a success.

**Worked example with this repository's figures.** One full triage (supervisor and both agents)
costs about $0.019 in model usage, measured in Langfuse. In the first end-to-end run of the seven
fixture scenarios, six ended in the expected outcome and one did not (Part 1, entry 1).

| | Calculation | Result |
|---|---|---|
| Cost per request | 7 x $0.019 / 7 | $0.019 |
| Cost per success | 7 x $0.019 / 6 | about $0.022 |

This is an estimate: it applies today's measured cost per triage to that run, which was traced
only in part. After the fix, all seven scenarios succeed and the two figures are equal. The gap
between them is the price of unreliability, and it is the number to watch when changing a
prompt, a model, or a limit.

**Measure both spending and cost per success.** A retry policy is the clearest case. These are
hypothetical results for the same 100 requests:

| Policy | Total spending | Successful requests | Cost per success |
|---|---|---|---|
| No retries | $10 | 50 | $0.20 |
| Allow retries | $12 | 80 | $0.15 |

Retries increased spending, and recovered enough failures to lower the cost per success. Looking
only at spending, retries look like waste; looking only at cost per success hides that the
budget went up 20%. The two numbers answer different questions (what will this cost, and what
does each good result cost), so a report needs both, plus the number of retries behind them.

**What it does not include.** A dispute escalated to a person has a human cost far larger than
the model cost. Two companion metrics cover that: the share of disputes resolved without a
person, and the share of automatic decisions later reversed.

**What this repository does today.** `make eval-cluster` runs nine scenarios against the
deployed system and reports total spending, successes, agent retries, cost per request, and cost
per success (ADR-0014). With one run per scenario and no failures this shows the mechanism; it
is not yet a measured error rate. Comparing retry policies is not possible yet: the retry limit
is a constant in code, and without failures the two policies would score the same. That needs a
configurable limit and deliberately injected failures
([#9](https://github.com/mangeleth/nequi-bank-agent-demo/issues/9)).

## C. What to do when the evidence is not enough

```
Evidence sufficient and consistent?
    Yes -> answer using that evidence.
    No:
        Missing     -> retrieve the missing information.
        Conflicting -> verify identity, timing, and authority.

Still unresolved?
    -> state what is known and what remains uncertain.
    -> escalate when policy or required resolution demands it.
```

**Where this repository stands, branch by branch.**

| Branch | Covered? | How |
|---|---|---|
| Sufficient and consistent: answer from the evidence | Yes | The supervisor chooses `finish`, the verdict is written from the gathered evidence, and the policy decides (all seven fixture scenarios). |
| Missing: retrieve it | Yes | The supervisor loops back to an agent; code forces the fraud assessment when a refund is possible; a failed agent is called once more. Finishing without ledger evidence is refused. |
| Conflicting, by **authority** | Yes | The system of record outranks every other source. Customer says "failed", ledger says "settled": no action. Model's figures differ from the ledger: rejected. Recommended refund differs from the ledger's discrepancy: sent to a person. Model says "no action" while the ledger shows money missing: sent to a person. |
| Conflicting, by **identity** | Yes | Identity comes only from the verified token; "I am user-1002" in the text changes nothing. Evidence that refers to different transactions fails the policy's `same_transaction` check. |
| Conflicting, by **timing** ([#6](https://github.com/mangeleth/nequi-bank-agent-demo/issues/6)) | **No** | Nothing checks how fresh the evidence is. A transfer that moves from pending to settled between the lookup and the decision would be judged on the stale reading. There is also no rule about how old a transaction may be to dispute. |
| Customer's claimed amount differs from the ledger ([#7](https://github.com/mangeleth/nequi-bank-agent-demo/issues/7)) | **Partly** | The policy pays the ledger's figure whatever was claimed, so the money is right, but no test or scenario exercises the mismatch and nothing reports it to a reviewer. |
| Still unresolved: state what is known and what is uncertain ([#8](https://github.com/mangeleth/nequi-bank-agent-demo/issues/8)) | **Partly** | The customer message states only established facts and leaves out what is not known (entry D). The result still does not list what remains *uncertain* for a reviewer. |
| Escalate when policy demands it | Yes | Policy limits send refunds to a person; high fraud risk goes to fraud operations; every breaker and failure ends in the `escalate` node. |

## D. Say only what has been established

Three situations that sound alike to a customer and are not the same:

| Situation | Precise wording |
|---|---|
| The investigation is running | "The transfer is marked as failed. We're checking additional records to determine the reason." |
| The investigation ended without finding the reason | "The transfer is marked as failed, but the available records don't show why." |
| The case has actually been escalated | "We couldn't determine the reason from the available records, so we've sent the case for review." |

Each sentence states only the evidence and the actions that exist. "We're checking" is true
only while something is checking. "We've sent the case for review" is true only once it has
been sent. Saying it earlier is a small false statement to a customer about their money.

**The same rule applies to verbs about money:** "recommended", "approved", "sent", and
"received" are four different facts, and a message may use only the one that has happened.

**What this repository does today.** Every triage result carries a `customer_message` chosen by
code from the final state (`services/supervisor/messages.py`). Its facts come from the ledger
figures and the refund policy; nothing a model wrote is repeated in it. Its verbs match what has
happened: a refund is "recommended" or "approved", and an approved refund is followed by "It has
not been paid yet", because this system does not pay yet. A case that needs a person is "marked
for review", not "sent for review", because there is no review queue to send it to until
Milestone 6. Tests check that no message uses a verb for an action that has not happened, and
the evaluation checks the message on the deployed system. The "investigation is running"
wording has no use yet: a triage is a single request, so there is no running state to report
until intake becomes asynchronous (Milestone 6).
