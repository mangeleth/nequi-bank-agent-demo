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

## 9. A pod started before its secret list was updated (Milestone 6)

**What happened.** The supervisor needed a third Key Vault secret, the database password. The
new pod crashed at startup: the password file was not there, although the secret list had been
updated in the same deploy. `make deploy` applied every manifest in one stream, in file-name
order, so the Deployment was applied before the updated `SecretProviderClass`. The pod was
created in between and mounted the old list. A Key Vault mount is fixed when the pod starts, so
restarting the container changed nothing.

**What caught it.** The rollout never became ready, and the old pods kept serving
(`maxUnavailable: 0`), so nothing was down. The pod's log named the missing file.

**What changed.** `make deploy` now applies configuration and secret mounts first and the
workload last. The stuck pod was replaced.

**The lesson.** A pod reads its configuration and its mounted secrets once, when it starts.
Order matters in a deploy, and a rolling update that keeps the old version serving turns a bad
release into a non-event.

## 10. A scripted edit cut the name off a Kubernetes manifest (Milestone 6)

**What happened.** A script that rewrote the supervisor's ConfigMap searched for the text
`data:` to find where the settings begin. It matched the end of `metadata:` first and replaced
everything after it, removing the ConfigMap's name and labels.

**What caught it.** Kubernetes refused the file ("resource name may not be empty"), so nothing
was applied and the running pods were untouched. The worker had already been deployed in the
right order, so the system kept working throughout.

**What changed.** The file was rewritten, and `make release` now runs `make validate` first:
every manifest is rendered and checked with a client-side dry run before anything is built.

**The lesson.** Validate generated or edited configuration before it reaches the deploy step,
and treat "the cluster rejected it" as a late safety net, not the check.

## 11. A fake hid a contract mismatch; the payment design contained it (Milestone 6)

**What happened.** On the first deploy of refund payments, every approved dispute went to a
person, although the ledger had paid. The ledger's confirmation describes the whole refund
(customer, transaction, amount, ...). The supervisor's `RefundPayment` contract forbids fields it
does not list, so the client called a successful payment "malformed" and treated it as "no
answer".

**Why the tests passed.** The test fake returned a ready-made `RefundPayment`. It never produced
the ledger's real reply, so the client's parsing of that reply was never tested.

**Why nothing bad happened.** The design assumed "no answer" could mean "paid":
- the retry used the same idempotency key, and the ledger returned the same refund (one row each)
- after the last delivery the dispute went to a person, with the key to look up in the ledger
- the customer was told "could not be paid automatically", never "paid"

**What changed.**
- The client keeps the fields it records and checks that the confirmation is for the
  transaction, amount, and key it asked for.
- New tests run the supervisor's client against the real Core Systems app. Checked: the test
  fails on the old client.
- The evaluator waits until a dispute has settled; a finished run that is still
  `refund_approved` is mid-payment.

**The lesson.** Where two services meet, test at least once against the real other side, not
only a fake. And design the money path so that a bug in reading an answer fails safe: the
unknown case must never pay twice and never claim "paid".

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
idempotent refund execution that a batch refund needs. The fast path is planned as Step 12 of
Milestone 6: one more check at that gate, before the queue.

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

## E. "The agent finished" is not "the customer's issue is resolved"

Two statuses answer two different questions, and merging them produces false statements.

| | Question it answers | Who reads it | Typical values |
|---|---|---|---|
| **Execution status** | What happened to this run of the graph? | Engineers, operations | queued, running, finished, failed |
| **Business status** | Where does the customer's dispute stand? | The customer, support, auditors | received, investigating, pending human approval, refund approved, refund paid, closed |

Why they must be separate:
- A run can **finish successfully** and leave the dispute **unresolved**: the graph did its job
  by sending the case to a person. Reporting "finished" as "resolved" tells the customer their
  problem is over when a human has not looked at it.
- A run can **fail** while the dispute is fine: a retry or a person picks it up, and the customer
  should never see "failed".
- They change at different times and for different reasons. The business status changes on
  events outside any run: a person approves, the ledger confirms a payment, a customer appeals.
- A dispute outlives its runs. One dispute may have several runs (a retry, a re-run after new
  evidence), so the business status belongs to a stored dispute record, not to a run.

That last point is why the business status needs a **database**: Redis here holds claims and
results that expire, a trace describes one run, and neither is the durable record of a
dispute. The plan is PostgreSQL in Milestone 6, Step 10.

**What this repository does today.** Every dispute is a row in PostgreSQL with both statuses
stored separately (ADR-0016). There is no `resolved`: an automatically approved refund is
`refund_approved`, and it becomes `refund_paid` only when the ledger confirms the payment
(Milestone 6, Step 11). A run that fails leaves the dispute as `pending_human_approval`, and the
customer message says "marked for review by a person". Every status change is in an audit table.

## F. A correct status does not prove the explanation

```python
scores = {
    "status_correct": predicted_status == expected_status,
    "id_format_valid": bool(re.fullmatch(r"TX-\d{6}", transaction_id)),
}
# Neither check proves that this explanation is supported:
explanation = "The transfer failed due to insufficient funds."
```

Deterministic checks verify the structured part of an answer. The free text beside it can still
state something no record supports, and it is the part the customer reads.

**Evaluate the explanation separately, against the evidence.** An LLM judge is the usual tool,
and it is only trustworthy with three things:
- **Evidence:** the judge sees exactly what the model had (the tool results), not its own
  knowledge of the world.
- **A rubric:** per claim, *supported*, *contradicted*, or *not found in the evidence*. A single
  1-5 "quality" score cannot be acted on.
- **Calibration:** hand-labelled examples the judge must score correctly. A judge is a model
  too; its agreement with people is a number to report, not an assumption.

Order of preference stays the same as everywhere else: a deterministic check where one is
possible, a judge only for what cannot be checked by code.

**Calibrating the judge.** A judge is a model, so it is measured against people before its
verdicts are trusted. People label a set of answers PASS or FAIL; the judge labels the same set.

```python
cases = [
    {"human": "FAIL", "judge": "PASS"},
    {"human": "FAIL", "judge": "FAIL"},
    {"human": "PASS", "judge": "PASS"},
    {"human": "PASS", "judge": "FAIL"},
]
agreement = sum(c["human"] == c["judge"] for c in cases) / len(cases)                    # 50%
unsafe_passes = sum(c["human"] == "FAIL" and c["judge"] == "PASS" for c in cases)       # 1
```

Overall agreement is useful, but which disagreements occurred tells you what to fix. The two
kinds are not equally bad:

| Disagreement | Meaning | Cost | Likely fix |
|---|---|---|---|
| Human FAIL, judge PASS (**unsafe pass**) | The judge let a bad answer through | A wrong answer reaches customers unnoticed. The one to drive to zero. | The rubric is too loose, or the judge was not shown the evidence it needed |
| Human PASS, judge FAIL (false alarm) | The judge rejected a good answer | Reviewer time, and people stop trusting the alerts | The rubric is ambiguous or stricter than the people applying it |

So a calibration report states three things: agreement, the count of unsafe passes, and the
disagreeing cases themselves, read one by one. It is done per rubric criterion (a judge can be
reliable on groundedness and poor on clarity), and repeated whenever the judge's prompt or
model changes. Four cases illustrate the arithmetic; a real set needs enough FAIL examples for
an unsafe-pass rate to mean something.

**Generalization versus drift.** Two more questions about a judge, checked separately:

| Concept | What we are checking | Example |
|---|---|---|
| **Generalization** | Does the judge work beyond the examples used to tune its rubric? | We corrected "insufficient funds"; can it also reject an unsupported "bank rejection"? |
| **Drift** | Does the judge still behave today as it did when it was calibrated? | The judge model was updated, or disputes now include a new kind of transfer: do agreement and unsafe passes hold? |

- *Generalization* is tested with a **held-out set**: labelled examples that were never used
  while writing the rubric. Tuning the rubric until the tuning examples pass proves little; a
  judge that only learned those examples fails on the next unsupported cause.
- *Drift* is tested by **re-running the same fixed calibration set on a schedule** and after
  every change to the judge's model or prompt, and comparing with the previous run.
- Both belong on screen next to the judge's verdicts: agreement and unsafe passes on the tuning
  set and on the held-out set, and the same numbers over time. A judge whose health cannot be
  seen will be trusted or ignored for the wrong reasons.

**What this repository does today.** The evaluation checks numbers only (*numeric*
groundedness): every amount, score, and count the models write must appear in the tool results.
A false cause with no number in it would pass. Core Systems returns no failure reason at all,
so any cause a model states is unsupported by definition. The customer message avoids the
problem by not using model text. The judge is planned for Milestone 8 ([#10](https://github.com/mangeleth/nequi-bank-agent-demo/issues/10)).

## G. When a fact is missing, retrieve it, and prefer a lookup to an agent

```python
def route(state):
    return "diagnostics" if state["failure_reason"] is None else END
```

A graph can branch on *what is missing*: if the transaction record has no failure reason, go to
a diagnostics step that looks for one, and only then answer.

Two points for the design:
- **A lookup is a node, not an agent.** In the sketch above `diagnostics` calls one function and
  returns its value. That needs no model. An agent earns its place only when the source is
  unstructured (free-text logs) and someone has to interpret it.
- **The step must be allowed to fail.** If diagnostics finds nothing, the honest answer is "the
  transfer is marked as failed, but the available records don't show why" (entry D), not a
  plausible guess.

**What this repository does today.** The supervisor already branches on missing evidence for the
ledger facts and the fraud assessment. It has no failure-reason data and no diagnostics step
([#11](https://github.com/mangeleth/nequi-bank-agent-demo/issues/11)).

## H. Showing progress to the customer

- The backend reads `graph.stream(..., stream_mode="updates")` to receive each node's update as
  the graph runs.
- **Task events are for monitoring the execution** (which node ran, how long, did it fail).
- **Explicitly stored business statuses are for explaining progress to the customer.** "Checking
  additional records" is a status the system sets on purpose, not a translation of whichever
  node happens to be running.
- With a checkpointer configured, the saved state of a run can be inspected with `get_state`,
  and a run can pause for a human and resume.

**What this repository does today.** The triage is one request that returns when it is done,
with the path taken in `steps`. There is no streaming, no checkpointer, and no stored status.
These arrive with asynchronous intake (Milestone 6, Step 10) and the UI (Milestone 7).
