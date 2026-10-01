"""The chat model used by every agent, built from configuration (ADR-0010).

Only agent services import this module, so only their images need LangChain and Azure Identity.
"""

import os

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import AzureChatOpenAI

SEED = 42


def build_chat_model(**overrides) -> AzureChatOpenAI:
    """gpt-4o on Azure OpenAI: Entra ID login (no API key), temperature 0, fixed seed."""
    names = ["AOAI_NAME", "AOAI_DEPLOYMENT", "AOAI_API_VERSION"]
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError(f"missing environment variables: {', '.join(missing)}")

    # Locally this is your `az login`; in AKS it is the pod's Workload Identity (ADR-0001).
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    settings = {
        "azure_endpoint": f"https://{os.environ['AOAI_NAME']}.openai.azure.com/",
        "azure_deployment": os.environ["AOAI_DEPLOYMENT"],
        "api_version": os.environ["AOAI_API_VERSION"],
        "azure_ad_token_provider": token_provider,
        "temperature": 0,
        "seed": SEED,
        "timeout": 30,
        "max_retries": 2,
    }
    return AzureChatOpenAI(**(settings | overrides))
