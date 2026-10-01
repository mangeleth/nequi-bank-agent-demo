# ADR-0003: Langfuse Cloud tracing without PII masking

- **Status:** Accepted (demo only)
- **Date:** 2026-10-01
- **Milestone:** M5

## Context
Every supervisor decision, agent call, and tool argument is traced to Langfuse Cloud, a SaaS
outside our Azure boundary. In production this would export customer data to a third party.

## Decision
Send traces to Langfuse Cloud unmasked. All data in the demo is synthetic.

## Consequences
- + Simpler setup; full trace fidelity for the demo.
- - Not acceptable with real customer data.

## Production delta
Colombian data-protection law (Ley 1581 / Habeas Data) and SFC outsourcing rules apply.
Either mask PII client-side with Langfuse's `mask=` hook (account numbers, names, `user_id`)
or self-host Langfuse inside the Azure network.
