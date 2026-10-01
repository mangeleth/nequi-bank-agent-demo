# nequi-bank-agent-demo

Proof-of-concept Dispute Triage & Resolution multi-agent system on AKS.

- Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)
- Architecture decisions: [docs/adr](docs/adr/README.md)

## How refunds are approved

The AI agents investigate and **recommend**; they can never approve or move money.
A deterministic policy (plain Python, no LLM) decides who approves, using facts from the
core banking ledger and risk engine ([ADR-0007](docs/adr/0007-tiered-refund-approval.md)).

```
 Customer dispute ──► AI agents investigate ──► DisputeVerdict (recommendation only)
                                                        │
                                                        ▼
                                      shared/refund_policy.py (no LLM)
                    ledger says FAILED · amount == ledger discrepancy · fraud risk LOW
                    amount <= 100.000 COP · <= 3 auto-refunds / 200.000 COP per 30 days
                                                        │
                                   all pass ────────────┴──────────── any fails
                                      ▼                                   ▼
                              auto-approved refund                 human review queue
```

Limits are configuration (`AUTO_REFUND_*` env vars), with a kill switch `AUTO_REFUND_ENABLED=false`.
