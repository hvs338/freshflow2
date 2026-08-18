"""
Model adapter: one interface, two backends, and a no-model default.

Everything downstream calls `LLM.converse` and never learns which backend
answered. Two reasons that matters. Bedrock is the demo path, but a clone of
this repo with no AWS credentials still has to run end to end. And the tool
loop in agent.py is the part worth reading aloud; it should not be threaded
with `if backend == "bedrock"`.

The message and tool shapes below are neutral -- neither Bedrock's nor
Anthropic's. Each adapter translates at its own edge, so the translation lives
in one place per backend instead of leaking into the agent.

    Tool     {"name", "description", "schema"}       schema is plain JSON Schema
    Message  {"role": "user"|"assistant", "content": [Block, ...]}
    Block    {"kind": "text",        "text"}
             {"kind": "tool_use",    "id", "name", "input"}
             {"kind": "tool_result", "id", "text", "is_error"}
    Reply    {"text", "tool_uses": [{"id", "name", "input"}], "stop"}

The two adapters are deliberately not factored into a shared translator. Their
wire formats differ in every key name, so a common one would be a lookup table
pretending to be an abstraction -- harder to read than the two plain versions.
"""

from __future__ import annotations

import json
import os
from typing import Protocol

# Sonnet 4.6 on both backends. It is what router.py already used, and unlike
# the Opus 5 family it does not think unless asked. That keeps the loop simple:
# no reasoning blocks to echo back verbatim on the next turn. Override per
# backend via env if a demo wants more headroom.
BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"

MAX_TOKENS = 4096

DEFAULT_AWS_REGION = "us-west-2"
SUPPORTED_BACKENDS = ("bedrock", "local", "none")


class LLM(Protocol):
    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        ...


def _build_reply(text_parts: list[str], tool_uses: list[dict], stop_reason: str) -> dict:
    """The neutral Reply shape. Written once so both adapters agree on it."""
    return {
        "text": "\n".join(text_parts).strip(),
        "tool_uses": tool_uses,
        "stop": stop_reason or "",
    }


# --- Bedrock ---------------------------------------------------------------


class BedrockLLM:
    """
    bedrock-runtime Converse with tool use.

    Converse rather than InvokeModel because the tool-use contract is the same
    shape across model families, so swapping BEDROCK_MODEL_ID does not mean
    rewriting the adapter.
    """

    name = "bedrock"

    def __init__(self, model_id: str | None = None, region: str | None = None):
        import boto3

        self.model_id = model_id or os.environ.get(
            "BEDROCK_MODEL_ID", BEDROCK_DEFAULT_MODEL
        )
        self.region = region or os.environ.get("AWS_REGION") or DEFAULT_AWS_REGION
        self.client = boto3.client("bedrock-runtime", region_name=self.region)

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[self._encode_message(message) for message in messages],
            toolConfig={"tools": [self._encode_tool(tool) for tool in tools]},
            inferenceConfig={"maxTokens": MAX_TOKENS},
        )
        return self._decode_response(response)

    @staticmethod
    def _encode_tool(tool: dict) -> dict:
        return {
            "toolSpec": {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": {"json": tool["schema"]},
            }
        }

    @staticmethod
    def _encode_message(message: dict) -> dict:
        content = []
        for block in message["content"]:
            kind = block["kind"]

            if kind == "text":
                # Converse rejects empty text blocks, and a tool-only assistant
                # turn legitimately has one.
                if block["text"].strip():
                    content.append({"text": block["text"]})

            elif kind == "tool_use":
                content.append({
                    "toolUse": {
                        "toolUseId": block["id"],
                        "name": block["name"],
                        "input": block["input"],
                    }
                })

            elif kind == "tool_result":
                content.append({
                    "toolResult": {
                        "toolUseId": block["id"],
                        "content": [{"text": block["text"]}],
                        "status": "error" if block.get("is_error") else "success",
                    }
                })

        return {"role": message["role"], "content": content}

    @staticmethod
    def _decode_response(response: dict) -> dict:
        text_parts, tool_uses = [], []

        for block in response["output"]["message"]["content"]:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                requested = block["toolUse"]
                tool_uses.append({
                    "id": requested["toolUseId"],
                    "name": requested["name"],
                    "input": requested["input"],
                })

        return _build_reply(text_parts, tool_uses, response.get("stopReason", ""))


# --- Anthropic API ---------------------------------------------------------


class LocalLLM:
    """Same contract against the Anthropic API, for working without AWS."""

    name = "local"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        import anthropic

        self.model = model or os.environ.get(
            "ANTHROPIC_MODEL", ANTHROPIC_DEFAULT_MODEL
        )
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[self._encode_message(message) for message in messages],
            tools=[self._encode_tool(tool) for tool in tools],
        )
        return self._decode_response(response)

    @staticmethod
    def _encode_tool(tool: dict) -> dict:
        return {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["schema"],
        }

    @staticmethod
    def _encode_message(message: dict) -> dict:
        content = []
        for block in message["content"]:
            kind = block["kind"]

            if kind == "text":
                if block["text"].strip():
                    content.append({"type": "text", "text": block["text"]})

            elif kind == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": block["id"],
                    "name": block["name"],
                    "input": block["input"],
                })

            elif kind == "tool_result":
                content.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": block["text"],
                    "is_error": bool(block.get("is_error")),
                })

        return {"role": message["role"], "content": content}

    @staticmethod
    def _decode_response(response) -> dict:
        text_parts, tool_uses = [], []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append({
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                })

        return _build_reply(text_parts, tool_uses, response.stop_reason)


# --- Selection -------------------------------------------------------------


def get_backend() -> str:
    """
    Which backend this process will use: bedrock, local, or none.

    Defaults to `none` so a clone with no credentials still starts, but `none`
    cannot answer: something has to map a question onto a tool. The query layer
    in queries.py is callable without a model, and verify.py exercises it that
    way, so a deterministic path is possible -- it just does not exist yet.
    """
    requested = (os.environ.get("FRESHFLOW_BACKEND") or "none").strip().lower()

    if requested not in SUPPORTED_BACKENDS:
        return "none"
    if requested == "local" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "none"
    return requested


def get_llm() -> LLM | None:
    """The adapter for the selected backend, or None under `none`."""
    backend = get_backend()
    if backend == "bedrock":
        return BedrockLLM()
    if backend == "local":
        return LocalLLM()
    return None


if __name__ == "__main__":
    print(json.dumps({"backend": get_backend()}))
