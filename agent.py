"""
The conversation loop.

Ask the model. If it wants to call a tool, call it and hand back the rows.
Repeat until it answers, or until we hit the cap. That is the entire product,
and it is short on purpose -- this is the file most likely to be edited on a
shared screen.

The trust property is not enforced here, it is structural: every tool bottoms
out in one SELECT against the governed view, tools only return aggregated rows,
and the SQL each one ran is shown to the user next to the answer -- including the
SQL the harness generated for a typed tool, which the model never saw. If a
number in the prose is wrong, the query that produced it is right there to check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import router
from llm import get_backend, get_llm
from semantic import QueryRejected, Semantic
from tools import ToolError, Tools

MAX_STEPS = 5


@dataclass
class Step:
    """One tool call. This is what the user sees to check the answer."""
    tool: str
    purpose: str
    sql: str
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
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
        self.typed = Tools(self.sem)
        self.tools = self.typed.specs()
        self.system = router.system_prompt(self.sem, self.typed.months)

    def ask(self, question: str) -> Answer:
        if self.llm is None:
            return Answer(
                question=question,
                text=("No model is configured, so nothing can map this question "
                      "onto a tool. Set FRESHFLOW_BACKEND=bedrock or =local."),
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
                step, payload = self._run(use["name"], use["input"] or {})
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

    def _run(self, name: str, args: dict) -> tuple[Step, dict]:
        """
        Run one tool call. A rejection is a result, not an exception -- the model
        reads the reason and fixes the call, the same way a person would.

        Both paths end in the same place: a Step carrying the SQL that ran, and
        a payload carrying the rows plus which definitions were used.
        """
        resolved = router.resolve(args.get("measure"), args.get("scope"))
        purpose = args.get("purpose") or _purpose(name, args)

        try:
            out = (self._query(args, resolved) if name == "query"
                   else self.typed.call(name, args))
        except (QueryRejected, ToolError) as e:
            step = Step(tool=name, purpose=purpose, sql=args.get("sql") or "",
                        ok=False, error=str(e))
            return step, {"ok": False, "error": str(e)}

        notes = out.get("notes", [])
        step = Step(tool=name, purpose=purpose, sql=out["sql"], ok=True,
                    columns=out["columns"], rows=out["rows"], notes=notes)

        payload = {
            "ok": True,
            "scope_used": resolved["scope"],
            "scope_defaulted": resolved["choices"][0]["defaulted"],
            "measure": resolved["measure"] or "not specified; report both",
            "columns": out["columns"],
            "rows": out["rows"],
        }
        for key in ("notes", "causes", "sorted_by"):
            if out.get(key):
                payload[key] = out[key]
        return step, payload

    def _query(self, args: dict, resolved: dict) -> dict:
        """The escape hatch. Raises QueryRejected, which _run turns into a result."""
        sql = (args.get("sql") or "").strip()
        out = self.sem.run(sql, resolved["scope"], router.MAX_ROWS)
        notes = ["This ran your SQL directly, so it did not carry the reviewed "
                 "shrink logic. The query text is shown to the user."]
        if not out["rows"]:
            notes.append("No rows. Check the filters before reporting a zero.")
        return {"sql": sql, "columns": out["columns"], "rows": out["rows"], "notes": notes}


def _purpose(name: str, args: dict) -> str:
    """
    A label for a typed call, which has no `purpose` argument to give one.

    Built from the arguments rather than asked for, so the evidence panel reads
    the same whether the model called a tool or wrote SQL.
    """
    bits = [k for k in ("period", "prior", "by", "dimension", "group_by") if args.get(k)]
    detail = ", ".join(f"{k}={args[k]}" for k in bits)
    return f"{name}({detail})" if detail else name
