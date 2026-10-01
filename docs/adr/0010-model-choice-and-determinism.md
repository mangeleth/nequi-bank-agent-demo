# ADR-0010: gpt-4o at temperature 0, pinned and replaceable

- **Status:** Accepted
- **Date:** 2026-10-01
- **Milestone:** M3

## Context
The agents need an LLM for tool selection, routing, and filling structured contracts. In a bank
we want behaviour that is repeatable and auditable. Constraints found on this subscription
(Free Trial, `eastus2`):

| Option | Quota | Temperature control | Deployment | Outcome |
|---|---|---|---|---|
| `gpt-4o` 2024-11-20 | 50K TPM | Yes | Standard (regional) | **Chosen** |
| `gpt-4o-mini` | 200K TPM | Yes | Standard (regional) | Viable, weaker |
| `gpt-5` family | 0 (mini: 500K) | No (reasoning models) | Global only | Rejected |
| Claude on Azure (Foundry) | 0, Marketplace billing | Current models: no | Global / Data Zone | Not available on Free Trial |

## Decision
- **Model:** Azure OpenAI `gpt-4o`, version `2024-11-20`, Standard deployment in `eastus2`
  (inference stays in one region), 30K TPM.
- **Sampling:** `temperature=0` and a fixed `seed` on every decision-making call.
- **Why temperature 0:** it makes the model pick the most likely token each time, so the same
  input gives nearly the same output: stable tool choices, fewer schema-validation retries,
  stable tests, and re-runnable audits.
- **What temperature 0 does NOT do:** it does not prevent hallucination. A model at temperature 0
  can be consistently wrong. Hallucination is controlled elsewhere:
  1. *Grounding:* agents get facts from tools that call the systems of record (ADR-0008).
  2. *Validation:* every output must pass the Pydantic contracts (ADR-0006).
  3. *Deterministic approval:* money decisions use ledger facts in plain code (ADR-0007).
- **Authentication:** Entra ID only; API keys are disabled on the resource
  (`disableLocalAuth=true`). Pods use Workload Identity with the `Cognitive Services OpenAI User`
  role (ADR-0001).
- **The model is a replaceable dependency:** name, version, and API version are configuration
  (`AOAI_*`); the version is pinned with `versionUpgradeOption=NoAutoUpgrade`; code talks to
  LangChain's chat-model interface, not to a vendor SDK.

## Consequences
- + Repeatable behaviour verified by `make aoai-check` (3 identical answers across 3 runs).
- + No stored model credentials anywhere.
- - `gpt-4o` 2024-11-20 retires on **2027-04-14**; a migration is required before then.
- - Temperature 0 with a seed is *nearly* deterministic, not strictly: backend changes
  (`system_fingerprint`) can alter outputs.
- - 30K TPM is enough for a demo, not for load.

## Model retirement: migration procedure
1. Deploy the candidate model next to the current one (new deployment name).
2. Run the evaluation set (the 7 scenarios in `core_systems/adapters/fixtures.py`, each with a
   known correct outcome) against both; compare in Langfuse (M5).
3. If outcomes match, change `AOAI_DEPLOYMENT` and redeploy; otherwise adjust prompts and re-run.
4. Watch traces after the switch; roll back by restoring the previous setting.

## When temperature is no longer available
Newer models from both OpenAI (GPT-5 family) and Anthropic (current Claude models) reject a
custom temperature, so the successor to `gpt-4o` will likely not offer it. Repeatability then
comes from the system, not from a sampling parameter:
1. **Constrain the output:** strict structured output with enums, so the model chooses among a
   few allowed values instead of writing free text.
2. **Keep decisions in code:** the model recommends; deterministic policy decides (ADR-0007).
3. **Measure consistency:** run each evaluation scenario several times and require the same
   decision every time (decision agreement rate) before accepting a model or prompt change.
4. **Audit from records, not re-runs:** store every verdict and its full trace once (Langfuse);
   an audit reads what the model actually said, it does not ask the model again.
5. **Decide once per dispute:** persist the verdict under the dispute ID (idempotency), so a
   retry returns the stored answer instead of generating a possibly different one.
6. **Use the controls that exist:** fixed low `reasoning_effort`, pinned model version, and
   versioned prompts.
7. **For high-stakes steps, vote:** run the step three times and take the majority, escalating
   to a human on disagreement (costs 3x; reserve for decisions that warrant it).

## Production delta
Provisioned throughput for predictable latency; a second region or model as failover; content
filtering policies reviewed with compliance; automated evaluation in CI gating every model,
prompt, or tool change; alerts on retirement dates; Data Zone or regional deployments chosen
to meet data-residency requirements.
