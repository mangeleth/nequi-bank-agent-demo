"""The groundedness requirement is one sentence, defined once, present in every prompt whose
output a customer or reviewer reads."""

from services.fraud_agent.agent import SYSTEM_PROMPT as FRAUD_PROMPT
from services.ledger_agent.agent import SYSTEM_PROMPT as LEDGER_PROMPT
from services.supervisor.graph import SUPERVISOR_PROMPT, VERDICT_PROMPT
from shared.prompts import GROUNDEDNESS_RULE


def test_the_rule_says_both_halves():
    assert "must be supported by the available records or tool results" in GROUNDEDNESS_RULE
    assert "state that the information is unknown" in GROUNDEDNESS_RULE


def test_every_prompt_that_writes_for_people_carries_the_rule():
    for name, prompt in {"fraud": FRAUD_PROMPT, "ledger": LEDGER_PROMPT, "verdict": VERDICT_PROMPT}.items():
        assert prompt.count(GROUNDEDNESS_RULE) == 1, name
        assert "{" not in prompt and "}" not in prompt, f"{name}: unfilled placeholder"


def test_the_routing_prompt_does_not_need_it():
    # The router only picks the next step; nothing it writes reaches a customer as a claim.
    assert GROUNDEDNESS_RULE not in SUPERVISOR_PROMPT
