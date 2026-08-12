"""
The conversation loop.

Ask the model. If it wants to run a query, run it and hand back the rows. Repeat
until it answers, or until we hit the cap. That is the entire product, and it is
short on purpose -- this is the file most likely to be edited on a shared screen.

The trust property is not enforced here, it is structural: the model has exactly
one tool, that tool only returns aggregated rows, and every query it ran is shown
to the user next to the answer. If a number in the prose is wrong, the query that
produced it is right there to check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import router
from llm import get_backend, get_llm
from semantic import QueryRejected, Semantic

MAX_STEPS = 5


@dataclass
class Step:
    """One query the model ran. This is what the user sees to check the answer."""
    purpose: str
    sql: str
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class Answer:
    question: str
    text: str
    backend: str
    steps: list[Step] = field(default_factory=list)
    choices: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Agent:
    def __init__(self, sem: Semantic | None = None, llm=None):
        self.sem = sem or Semantic()
        self.backend = get_backend()
        self.llm = llm if llm is not None else get_llm()
        self.system = router.system_prompt(self.sem)
        self.tools = [router.query_tool()]

    def ask(self, question: str) -> Answer:
        if self.llm is None:
            return Answer(
                question=question,
                text=("No model is configured, so I cannot write SQL for this "
                      "question. Set FRESHFLOW_BACKEND=bedrock or =local."),
                backend=self.backend,
                warnings=["Running without a model."],
            )
        try:
            return self._loop(question)
        except Exception as e:  # noqa: BLE001
            return Answer(question=question, text=f"Something went wrong: {e}",
                          backend=self.backend, warnings=[str(e)])

    # -- the loop ----------------------------------------------------------

    def _loop(self, question: str) -> Answer:
        messages = [{"role": "user", "content": [{"kind": "text", "text": question}]}]
        steps: list[Step] = []
        warnings: list[str] = []
        resolved = None
        text = ""

        for _ in range(MAX_STEPS):
            reply = self.llm.converse(self.system, messages, self.tools)
            text = reply["text"] or text
            if not reply["tool_uses"]:
                break

            # Echo the model's turn back, then answer every tool call in one
            # user turn -- splitting them teaches the model to stop batching.
            assistant = [{"kind": "text", "text": reply["text"]}] if reply["text"] else []
            assistant += [{"kind": "tool_use", **u} for u in reply["tool_uses"]]
            messages.append({"role": "assistant", "content": assistant})

            results = []
            for use in reply["tool_uses"]:
                step, payload = self._run(use["input"])
                steps.append(step)
                if step.ok:
                    resolved = router.resolve(use["input"].get("measure"),
                                              use["input"].get("scope"))
                results.append({
                    "kind": "tool_result", "id": use["id"],
                    "text": json.dumps(payload, default=str),
                    "is_error": not step.ok,
                })
            messages.append({"role": "user", "content": results})
        else:
            warnings.append(
                f"Stopped after {MAX_STEPS} queries without a final answer. "
                "Below is the last thing the model said."
            )

        return Answer(
            question=question,
            text=text or "The model returned no answer.",
            backend=self.backend,
            steps=steps,
            choices=resolved["choices"] if resolved else [],
            warnings=warnings,
        )

    def _run(self, args: dict) -> tuple[Step, dict]:
        """
        Run one query. A rejection is a result, not an exception -- the model
        reads the reason and fixes its SQL, the same way a person would.
        """
        sql = (args.get("sql") or "").strip()
        purpose = args.get("purpose") or "not stated"
        resolved = router.resolve(args.get("measure"), args.get("scope"))

        try:
            out = self.sem.run(sql, resolved["scope"], router.MAX_ROWS)
        except QueryRejected as e:
            step = Step(purpose=purpose, sql=sql, ok=False, error=str(e))
            return step, {"ok": False, "error": str(e)}

        step = Step(purpose=purpose, sql=sql, ok=True,
                    columns=out["columns"], rows=out["rows"])

        payload = {
            "ok": True,
            "scope_used": resolved["scope"],
            "scope_defaulted": resolved["choices"][0]["defaulted"],
            "measure": resolved["measure"] or "not specified; report both",
            "columns": out["columns"],
            "rows": out["rows"],
        }
        if not out["rows"]:
            payload["note"] = "No rows. Check the filters before reporting a zero."
        return step, payload
