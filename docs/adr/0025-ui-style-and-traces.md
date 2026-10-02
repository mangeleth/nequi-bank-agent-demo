# ADR-0025: A Nequi-inspired look, clearly labelled as independent, and traces drawn inside the demo

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M7.5

## Context
The demo should feel like Nequi's world, and the viewer should see how a dispute was decided
without leaving the page for Langfuse. The page is public (ADR-0024), so a copy of Nequi's
identity could be mistaken for Nequi itself, or for a phishing page.

## Decision
- **Nequi-inspired, not a copy.** The palette and type from Nequi's public website: magenta
  `#DA0081` (accent), dark purple `#200020` (text, header), lilac `#ECE7F5` (panels), the
  open-source Manrope font, and rounded corners (`.streamlit/config.toml`). **No logo, no
  artwork, no product names**, and the page is titled "AI dispute triage demo".
- **The interface is in Spanish**, for Nequi's customers: labels, buttons, statuses, transfer
  descriptions, badges, the dashboard, the disclaimer. What the system produced is shown as it
  is (in English): the customer message, the steps taken, the trace's step names, and the models'
  inputs and outputs. The page says so next to the customer message.
- **Labelled as independent, on every view:** "Demo independiente para una entrevista: no es un producto de Nequi
  ni está afiliada a Nequi. Solo clientes y datos sintéticos; nunca ingreses datos personales o
  bancarios reales." The page never asks for credentials.
- **The trace inside the page.** For an agent investigation, the UI reads the run's observations
  from Langfuse's public API and draws them: the meaningful steps in time order, indented under
  their parents (the run, the supervisor's decisions, each agent, including the agent services in
  other pods that joined the trace, every model call and tool call), a timeline chart, totals
  (model calls, tool calls, tokens, cost, duration), and each step's input and output.
  LangChain's internal steps are hidden by default; totals always count every model call.
- **The UI reads the Langfuse keys** from Key Vault with its own identity (`id-demo-ui` gains
  read access to `langfuse-public-key` and `langfuse-secret-key`). Without keys, it falls back to
  a link.
- Traces arrive in Langfuse asynchronously: the trace is read when the viewer asks, and the page
  says so if it is still arriving.

## Consequences
- + The whole story of a decision is visible in one place, live, during the demo.
- - **The public UI's pod holds the Langfuse project keys.** Langfuse project keys are not
  read-only: someone who took over that pod could read every trace of the project (synthetic
  data) and write fake ones. The page itself only shows the trace of a dispute the viewer just
  submitted.

## Production delta
A read-only trace API inside the bank (or Langfuse's role-based access with a read-only service
account) in front of the tracing system; traces shown only to operations staff, after sign-in,
with personal data masked (ADR-0003); the UI never holds the tracing project's keys.
