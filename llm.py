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


class LLM(Protocol):
    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        ...


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
        self.region = region or os.environ.get("AWS_REGION") or "us-west-2"
        self.client = boto3.client("bedrock-runtime", region_name=self.region)

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        resp = self.client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[self._encode(m) for m in messages],
            toolConfig={
                "tools": [
                    {
                        "toolSpec": {
                            "name": t["name"],
                            "description": t["description"],
                            "inputSchema": {"json": t["schema"]},
                        }
                    }
                    for t in tools
                ]
            },
            inferenceConfig={"maxTokens": MAX_TOKENS},
        )
        return self._decode(resp)

    @staticmethod
    def _encode(msg: dict) -> dict:
        content = []
        for b in msg["content"]:
            if b["kind"] == "text":
                # Converse rejects empty text blocks, and a tool-only assistant
                # turn legitimately has one.
                if b["text"].strip():
                    content.append({"text": b["text"]})
            elif b["kind"] == "tool_use":
                content.append({
                    "toolUse": {
                        "toolUseId": b["id"],
                        "name": b["name"],
                        "input": b["input"],
                    }
                })
            elif b["kind"] == "tool_result":
                content.append({
                    "toolResult": {
                        "toolUseId": b["id"],
                        "content": [{"text": b["text"]}],
                        "status": "error" if b.get("is_error") else "success",
                    }
                })
        return {"role": msg["role"], "content": content}

    @staticmethod
    def _decode(resp: dict) -> dict:
        text, tool_uses = [], []
        for b in resp["output"]["message"]["content"]:
            if "text" in b:
                text.append(b["text"])
            elif "toolUse" in b:
                u = b["toolUse"]
                tool_uses.append({
                    "id": u["toolUseId"], "name": u["name"], "input": u["input"],
                })
        return {
            "text": "\n".join(text).strip(),
            "tool_uses": tool_uses,
            "stop": resp.get("stopReason", ""),
        }


# --- Anthropic API ---------------------------------------------------------


class LocalLLM:
    """Same contract against the Anthropic API, for working without AWS."""

    name = "local"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        import anthropic

        self.model = model or os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_DEFAULT_MODEL)
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    def converse(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[self._encode(m) for m in messages],
            tools=[
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["schema"],
                }
                for t in tools
            ],
        )
        return self._decode(resp)

    @staticmethod
    def _encode(msg: dict) -> dict:
        content = []
        for b in msg["content"]:
            if b["kind"] == "text":
                if b["text"].strip():
                    content.append({"type": "text", "text": b["text"]})
            elif b["kind"] == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": b["id"],
                    "name": b["name"],
                    "input": b["input"],
                })
            elif b["kind"] == "tool_result":
                content.append({
                    "type": "tool_result",
                    "tool_use_id": b["id"],
                    "content": b["text"],
                    "is_error": bool(b.get("is_error")),
                })
        return {"role": msg["role"], "content": content}

    @staticmethod
    def _decode(resp) -> dict:
        text, tool_uses = [], []
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                tool_uses.append({"id": b.id, "name": b.name, "input": dict(b.input)})
        return {
            "text": "\n".join(text).strip(),
            "tool_uses": tool_uses,
            "stop": resp.stop_reason or "",
        }


# --- Selection -------------------------------------------------------------


def get_backend() -> str:
    """
    Which backend this process will use: bedrock, local, or none.

    Defaults to `none` so a clone with no credentials still starts, but `none`
    cannot answer: something has to map a question onto a tool. The metric layer
    in metrics.py is callable without a model, and verify.py exercises it that
    way, so a deterministic path is possible -- it just does not exist yet.
    """
    want = (os.environ.get("FRESHFLOW_BACKEND") or "none").strip().lower()
    if want not in ("bedrock", "local", "none"):
        return "none"
    if want == "local" and not os.environ.get("ANTHROPIC_API_KEY"):
        return "none"
    return want


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
