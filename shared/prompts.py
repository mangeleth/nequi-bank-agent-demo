"""Text shared by every prompt that produces something a customer or reviewer will read.

One definition, used by the Fraud Agent, the Ledger Agent, and the supervisor's verdict, so the
requirement cannot drift between them. A rule in a prompt is advice to the model: it makes the
behaviour more likely. What checks it is the evaluation (evals/) and, later, the judge.
"""

GROUNDEDNESS_RULE = (
    "Groundedness requirement: every factual claim about the transaction must be supported by "
    "the available records or tool results. If the evidence is missing, state that the "
    "information is unknown."
)
