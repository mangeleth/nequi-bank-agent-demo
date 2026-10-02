# Learnings

Things that went wrong while building this system, what caught them, and what changed.
Each entry is a real event from this repository, not a hypothetical.

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
