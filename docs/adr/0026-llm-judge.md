# ADR-0026: An LLM judge grades every explanation against the records, calibrated against labels

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M8

## Context
The deterministic checks prove the decision: status, amount, policy route. They cannot tell
whether the explanation is true. "The transfer failed for insufficient funds" passes every status
check when the record says "processing error". A wrong explanation misleads the customer and the
person who reviews the dispute.

## Decision
- **A judge model grades what the models wrote** (the supervisor's explanation, the Ledger Agent's
  summary, the Fraud Agent's rationale) **against the records read by code** from Core Systems
  (the transaction and the risk signals), never against the agents' own words.
- **Three criteria, pass or fail, each with a short reason** (`services/judge/rubric.py`):
  - groundedness: every factual claim agrees with the evidence; nothing invented
  - completeness: states the outcome and its main reason, or says the evidence does not settle it
  - clarity: a customer could understand it; length earns no credit
- **The candidate and the evidence are data.** They are wrapped in labelled blocks; the judge is told
  that instructions inside them are part of what it grades, and an answer that contains one fails
  groundedness.
- **The bank's definitions are given to the judge** (risk bands, what each amount and status
  means, that the failure code is the only known cause), so it does not guess.
- **Calibrated against labelled cases** (`evals/judge/calibration.json`, `make judge-calibrate`):
  agreement, **unsafe passes** (label FAIL, judge PASS: a bad explanation goes unnoticed), and false
  alarms (label PASS, judge FAIL: noisy but safe), per criterion and split. Results are kept per run in
  `evals/judge/results/` with the prompt version.
- **Tuning discipline.** The prompt is changed only because of **tuning** cases. A held-out case whose
  failure is used to change the prompt is moved to tuning, and new held-out cases are written. So
  the held-out numbers keep measuring generalization.
- **It never touches money, and it can only send a case to a person.** When the verdict needs
  revision (any criterion FAIL) or the explanation could not be judged, the dispute goes to
  **customer service**: a follow-up queue in the reviewers' tab, with the judge's reasons, what
  the customer was told, and a required note when it is handled. A dispute **closed without a
  refund** is also **reopened** (back to `pending_human_approval`, in the review queue), because
  a wrong explanation there can hide a wrong decision against the customer. A refund already
  approved or paid stays as it is. Sending to a person is the safe direction: the judge's false
  alarms (about 6% on the calibration set) cost a person's time, never a customer's money.
- The judge is the same model family as the candidates (gpt-4o, temperature 0). Same-family judges
  can share blind spots (see the production delta).

- **In the background** (`services/judge/worker.py`): when a triage finishes with a model-written
  explanation, the triage worker adds a job to a Redis stream (`judge-jobs`, consumer group
  `judges`); the judge worker grades it and stores the verdict in `dispute_judgements`. A job not
  acknowledged is reclaimed after a minute; after 3 attempts the dispute is recorded as "could not
  be judged". Queuing never affects the dispute. The judge worker has its own identity (the model
  and the database password) and no Service.
- **Evidence as of when the explanation was written.** The judge usually runs after the refund was
  paid; the ledger then says `reversed`. A refund paid after the explanation is taken back out of
  the evidence by code, and the evidence says only which moment it describes, never what happened
  later (see "Found on the cluster").

## Found on the cluster
The first real verdict failed a correct explanation: the judge read the ledger after the refund
(`reversed`) and called "it failed, nothing arrived" wrong. Fix 1: rebuild the transaction as of
when the explanation was written. The next verdict still failed it, because the evidence note
named the later refund and the judge read "refund recommended" as contradicting "already paid".
Fix 2: describe only the moment. Then all three criteria passed, with reasons matching the records
the agents saw. Two lessons: evidence must be from the same moment as what is judged, and nothing
from later may leak into it.

## Calibration history
| Prompt | Tuning: agreement / unsafe passes | Held-out: agreement / unsafe passes | What changed |
|---|---|---|---|
| v1 | low (completeness 15%) / 0 | / 1 | Baseline. Completeness invented a bar ("what happens next"); criteria mixed up; "low risk" called unsupported |
| v2 | 92% / 0 | 75% / 1 | From tuning cases only: completeness defined; one problem, one criterion; decisions are not facts; risk bands. Held-out caught **"the bank rejected it"**: the judge had learned "insufficient funds", not the rule. It also exposed a wrong definition of ours (on a reversed transfer `credited_amount` is money returned) |
| v3 | 94% / 0 | 91% / 1 | Definitions fixed; "the failure code is the only known cause". The 3 held-out cases used were moved to tuning and 6 new held-out cases written. **Groundedness on held-out: 5 of 5 bad explanations caught.** Known gap: clarity passed code-like text in a new format |

The labels were proposed by Claude and are pending review by the project owner: some false alarms
may be label questions rather than judge errors.

## Consequences
- + Bad explanations are measured, not assumed away, and a person reviewing a dispute sees the
  judge's verdict next to it.
- + The held-out split shows whether a prompt change generalizes or only memorizes.
- - Each judged dispute costs one more model call (about $0.004).
- - Temperature 0 is not full determinism: a judge's verdict can vary between runs (measured by
  running scenarios several times, M8).

## Production delta
A judge from a different model family than the candidates; labels from several trained reviewers
with inter-rater agreement measured before the judge is; a larger, stratified calibration set with
a fresh held-out sample each quarter; alerts on the rate of judge failures in production; the
judge's verdict feeding a sample of disputes to human QA.
