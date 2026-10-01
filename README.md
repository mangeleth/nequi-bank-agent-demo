# nequi-bank-agent-demo

Proof-of-concept Dispute Triage & Resolution multi-agent system on AKS.

- Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)
- Architecture decisions: [docs/adr](docs/adr/README.md)

## How refunds are approved

The AI agents investigate and **recommend**; they can never approve or move money.
A deterministic policy (plain Python, no LLM) decides who approves, using facts from the
core banking ledger and risk engine ([ADR-0007](docs/adr/0007-tiered-refund-approval.md)).

```mermaid
flowchart TD
    customer["📱 Customer (Nequi app)<br/><b>DisputeRequest</b><br/>transaction_id, reason,<br/>claimed_amount, description"]
    supervisor["🤖 Supervisor<br/>LLM + LangGraph<br/>decides which agent to ask"]
    fraud["🤖 Fraud Agent<br/>LLM + tools"]
    ledger["🤖 Ledger Agent<br/>LLM + tools"]
    core[("🏦 Mock Core Banking + Risk Engine<br/>plain API, no LLM")]
    fa["<b>FraudAssessment</b><br/>risk_score, risk_level, signals"]
    lr["<b>LedgerReconciliation</b><br/>settlement_status = failed<br/>debited / credited"]
    verdict["🤖 Supervisor writes <b>DisputeVerdict</b><br/>decision = refund_recommended<br/>refund_amount<br/><i>recommendation only</i>"]
    policy["⚙️ <b>refund_policy.evaluate()</b> - no LLM<br/>kill switch on<br/>same transaction everywhere<br/>ledger status = failed<br/>amount == ledger discrepancy<br/>fraud risk = low<br/>amount ≤ 100.000 COP<br/>≤ 3 auto refunds in 30 days<br/>≤ 200.000 COP total in 30 days"]
    auto["✅ AUTO_APPROVED<br/>status: resolved<br/>pays the <b>ledger</b> amount"]
    human["👤 HUMAN_REQUIRED<br/>status: pending_human_approval<br/>goes to review queue"]

    customer --> supervisor
    supervisor --> fraud
    supervisor --> ledger
    fraud <-. "tool calls" .-> core
    core <-. "tool calls" .-> ledger
    fraud --> fa
    ledger --> lr
    fa --> verdict
    lr --> verdict
    verdict --> policy
    policy -- "all checks pass" --> auto
    policy -- "any check fails" --> human

    classDef llm fill:#fff4e5,stroke:#e69500,color:#222
    classDef det fill:#e8f4ff,stroke:#2b7bd6,color:#222
    classDef data fill:#f4f4f4,stroke:#888,color:#222
    classDef ok fill:#e7f7ec,stroke:#2e9b4f,color:#222
    classDef review fill:#fdecec,stroke:#d0453f,color:#222
    class supervisor,fraud,ledger,verdict llm
    class core,policy det
    class customer,fa,lr data
    class auto ok
    class human review
```

🟧 Orange = LLM (probabilistic: investigates and recommends) · 🟦 Blue = deterministic code (decides) ·
⬜ Grey = validated Pydantic contracts ([field formats](shared/schemas.py))

Limits are configuration (`AUTO_REFUND_*` env vars), with a kill switch `AUTO_REFUND_ENABLED=false`.
