"""Verify the Azure OpenAI deployment the way the agents will use it (ADR-0010):
Entra ID login (no API key), temperature 0, fixed seed, called through LangChain.

Sends the same prompt several times and reports whether the answers are identical.

Usage: make aoai-check   (reads AOAI_* settings from .env via the Makefile)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `shared` importable

from shared.llm import SEED, build_chat_model  # noqa: E402

RUNS = 3
PROMPT = (
    "A customer says 50000.00 COP left their account but the recipient never received it. "
    "The ledger shows debited=50000.00, credited=0.00, status=failed. "
    "In one sentence, what happened to the money?"
)


def main() -> int:
    model = build_chat_model(max_tokens=100)
    answers = []
    for run in range(1, RUNS + 1):
        response = model.invoke(PROMPT)
        meta = response.response_metadata
        answers.append(response.content)
        print(f"[{run}/{RUNS}] model={meta.get('model_name')} fingerprint={meta.get('system_fingerprint')}")
        print(f"      {response.content}")

    distinct = len(set(answers))
    print(f"\n{RUNS} runs, {distinct} distinct answer(s) at temperature=0, seed={SEED}")
    if distinct > 1:
        print("Note: temperature 0 is nearly, not strictly, deterministic (see ADR-0010).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
