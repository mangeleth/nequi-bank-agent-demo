"""Verify the Azure OpenAI deployment the way the agents will use it (ADR-0010):
Entra ID login (no API key), temperature 0, fixed seed, called through LangChain.

Sends the same prompt several times and reports whether the answers are identical.

Usage: make aoai-check   (reads AOAI_* settings from .env via the Makefile)
"""

import os
import sys

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI

RUNS = 3
PROMPT = (
    "A customer says 50000.00 COP left their account but the recipient never received it. "
    "The ledger shows debited=50000.00, credited=0.00, status=failed. "
    "In one sentence, what happened to the money?"
)


def build_model() -> AzureChatOpenAI:
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureChatOpenAI(
        azure_endpoint=f"https://{os.environ['AOAI_NAME']}.openai.azure.com/",
        azure_deployment=os.environ["AOAI_DEPLOYMENT"],
        api_version=os.environ["AOAI_API_VERSION"],
        azure_ad_token_provider=token_provider,  # short-lived Entra ID tokens, refreshed automatically
        temperature=0,  # always pick the most likely next word: repeatable, auditable
        seed=42,  # removes most of the remaining run-to-run variation
        max_tokens=100,
        timeout=30,
        max_retries=2,
    )


def main() -> int:
    model = build_model()
    answers = []
    for run in range(1, RUNS + 1):
        response = model.invoke(PROMPT)
        meta = response.response_metadata
        answers.append(response.content)
        print(f"[{run}/{RUNS}] model={meta.get('model_name')} fingerprint={meta.get('system_fingerprint')}")
        print(f"      {response.content}")

    distinct = len(set(answers))
    print(f"\n{RUNS} runs, {distinct} distinct answer(s) at temperature=0, seed=42")
    if distinct > 1:
        print("Note: temperature 0 is nearly, not strictly, deterministic (see ADR-0010).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
