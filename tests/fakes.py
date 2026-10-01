"""Test doubles shared by the agent tests."""

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool


class ScriptedChatModel(BaseChatModel):
    """Stands in for the LLM: plays back prepared AI messages and records what it was shown.

    Lets a test script any model behaviour, including a model fooled by prompt injection.
    """

    script: list[AIMessage]
    seen: list[list] = []  # messages received on each call
    tool_schemas: list[dict] = []  # tool descriptions the model was given

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        reply = self.script[min(len(self.seen), len(self.script)) - 1]
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, **kwargs):
        self.tool_schemas = [convert_to_openai_tool(t) for t in tools]
        return self

    def everything_shown_to_model(self) -> str:
        return json.dumps([m.model_dump() for call in self.seen for m in call], default=str)

    def tool_results(self) -> list[str]:
        return [str(m.content) for m in self.seen[-1] if isinstance(m, ToolMessage)]

    def lookup_tool_parameters(self) -> dict[str, set[str]]:
        return {t["function"]["name"]: set(t["function"]["parameters"]["properties"])
                for t in self.tool_schemas if t["function"]["name"].startswith("get_")}


def tool_call(name: str, call_id: str, **args) -> dict:
    return {"name": name, "args": args, "id": call_id}


def ai(*calls: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))
