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

# The tool the model uses to write its own SQL. Everything else is typed.
SQL_ESCAPE_HATCH = "query"

# Arguments worth showing in the evidence panel's label for a typed call.
LABELLED_ARGUMENTS = ("period", "prior", "by", "dimension", "group_by")


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
    def __init__(self, semantic_view: Semantic | None = None, llm=None):
        self.view = semantic_view or Semantic()
        self.backend = get_backend()
        self.llm = llm if llm is not None else get_llm()
        self.tool_handlers = Tools(self.view)
        self.tool_specs = self.tool_handlers.specs()
        self.system_prompt = router.build_system_prompt(
            self.view, self.tool_handlers.months
        )

    def describe_dataset(self) -> dict:
        """
        What the UI shows about the extract before any question is asked.

        Exists so callers ask the agent rather than reaching through it into the
        semantic view: api.py should not need to know the view has a
        `values_of` at all.
        """
        first_date, last_date = self.view.date_range()
        return {
            "coverage": {"start": first_date, "end": last_date},
            "columns": self.view.columns(),
            "stores": len(self.view.values_of("store_id")),
            "departments": self.view.values_of("dept"),
            "regions": self.view.values_of("region"),
            "banners": self.view.values_of("banner"),
            "months": self.tool_handlers.months,
        }

    def ask(self, question: str) -> Answer:
        """One question in, one answer plus every query it ran back."""
        if self.llm is None:
            return self._no_model_answer(question)

        try:
            return self._run_conversation(question)
        except Exception as error:  # noqa: BLE001
            return Answer(
                question=question,
                text=f"Something went wrong: {error}",
                backend=self.backend,
                warnings=[str(error)],
            )

    def _no_model_answer(self, question: str) -> Answer:
        """`FRESHFLOW_BACKEND=none`. Nothing can map a question onto a tool."""
        return Answer(
            question=question,
            text=(
                "No model is configured, so nothing can map this question onto "
                "a tool. Set FRESHFLOW_BACKEND=bedrock or =local."
            ),
            backend=self.backend,
            warnings=["Running without a model."],
        )

    # -- the loop ----------------------------------------------------------

    def _run_conversation(self, question: str) -> Answer:
        """
        Ask, call tools, feed the rows back, repeat until the model answers.

        `definitions` holds the last successful call's resolved scope and
        measure, because that is what the definition panel reports.
        """
        messages = [{"role": "user", "content": [{"kind": "text", "text": question}]}]
        steps: list[Step] = []
        warnings: list[str] = []
        definitions = None
        answer_text = ""

        for _ in range(MAX_STEPS):
            reply = self.llm.converse(self.system_prompt, messages, self.tool_specs)
            answer_text = reply["text"] or answer_text

            if not reply["tool_uses"]:
                break

            messages.append(_assistant_turn(reply))
            new_steps, results, definitions = self._run_requested_tools(
                reply["tool_uses"], definitions
            )
            steps.extend(new_steps)
            messages.append({"role": "user", "content": results})
        else:
            warnings.append(
                f"Stopped after {MAX_STEPS} queries without a final answer. "
                "Below is the last thing the model said."
            )

        return Answer(
            question=question,
            text=answer_text or "The model returned no answer.",
            backend=self.backend,
            steps=steps,
            choices=definitions["choices"] if definitions else [],
            warnings=warnings,
        )

    def _run_requested_tools(self, tool_uses: list[dict], definitions):
        """
        Every tool the model asked for this turn, answered in one user turn.

        Splitting the results across turns teaches the model to stop batching,
        so they go back together even when one of them failed.
        """
        steps, results = [], []

        for tool_use in tool_uses:
            step, payload = self.run_tool(tool_use["name"], tool_use["input"] or {})
            steps.append(step)

            if step.ok:
                definitions = router.resolve_definitions(
                    tool_use["input"].get("measure"),
                    tool_use["input"].get("scope"),
                )

            results.append({
                "kind": "tool_result",
                "id": tool_use["id"],
                "text": json.dumps(payload, default=str),
                "is_error": not step.ok,
            })

        return steps, results, definitions

    def run_tool(self, name: str, arguments: dict) -> tuple[Step, dict]:
        """
        Run one tool call. A rejection is a result, not an exception -- the model
        reads the reason and fixes the call, the same way a person would.

        Both paths end in the same place: a Step carrying the SQL that ran, and
        a payload carrying the rows plus which definitions were used.
        """
        definitions = router.resolve_definitions(
            arguments.get("measure"), arguments.get("scope")
        )
        purpose = arguments.get("purpose") or _describe_call(name, arguments)

        try:
            if name == SQL_ESCAPE_HATCH:
                result = self._run_model_sql(arguments, definitions)
            else:
                result = self.tool_handlers.call(name, arguments)
        except (QueryRejected, ToolError) as error:
            return _rejected_step(name, purpose, arguments, error)

        step = Step(
            tool=name,
            purpose=purpose,
            sql=result["sql"],
            ok=True,
            columns=result["columns"],
            rows=result["rows"],
            notes=result.get("notes", []),
        )
        return step, _payload_for_model(result, definitions)

    def _run_model_sql(self, arguments: dict, definitions: dict) -> dict:
        """
        The escape hatch. Raises QueryRejected, which run_tool turns into a result.

        The note is not decoration: the model needs to know this answer did not
        come from the reviewed shrink logic, so it can say so.
        """
        sql = (arguments.get("sql") or "").strip()
        result = self.view.run(sql, definitions["scope"], router.MAX_ROWS)

        notes = [
            "This ran your SQL directly, so it did not carry the reviewed "
            "shrink logic. The query text is shown to the user."
        ]
        if not result["rows"]:
            notes.append("No rows. Check the filters before reporting a zero.")

        return {
            "sql": sql,
            "columns": result["columns"],
            "rows": result["rows"],
            "notes": notes,
        }


# --- Shaping what crosses the wire -----------------------------------------


def _assistant_turn(reply: dict) -> dict:
    """The model's own turn, echoed back so the tool results have something to answer."""
    content = []
    if reply["text"]:
        content.append({"kind": "text", "text": reply["text"]})
    content += [{"kind": "tool_use", **use} for use in reply["tool_uses"]]
    return {"role": "assistant", "content": content}


def _payload_for_model(result: dict, definitions: dict) -> dict:
    """
    What the model gets back: the rows, plus which definitions produced them.

    The scope disclosure is read through `was_scope_defaulted` rather than by
    indexing into the choices list, so this file does not have to know how
    router.py orders them.
    """
    payload = {
        "ok": True,
        "scope_used": definitions["scope"],
        "scope_defaulted": _was_scope_defaulted(definitions),
        "measure": definitions["measure"] or "not specified; report both",
        "columns": result["columns"],
        "rows": result["rows"],
    }

    for optional_field in ("notes", "causes", "sorted_by"):
        if result.get(optional_field):
            payload[optional_field] = result[optional_field]
    return payload


def _was_scope_defaulted(definitions: dict) -> bool:
    """Whether the scope in use was chosen by the user or fallen back to."""
    for choice in definitions["choices"]:
        if choice["question"].startswith("Which departments"):
            return choice["defaulted"]
    return True


def _rejected_step(
    name: str, purpose: str, arguments: dict, error: Exception
) -> tuple[Step, dict]:
    """A refused call, shaped so the model can read the reason and retry."""
    message = str(error)
    step = Step(
        tool=name,
        purpose=purpose,
        sql=arguments.get("sql") or "",
        ok=False,
        error=message,
    )
    return step, {"ok": False, "error": message}


def _describe_call(name: str, arguments: dict) -> str:
    """
    A label for a typed call, which has no `purpose` argument to give one.

    Built from the arguments rather than asked for, so the evidence panel reads
    the same whether the model called a tool or wrote SQL.
    """
    present = [key for key in LABELLED_ARGUMENTS if arguments.get(key)]
    detail = ", ".join(f"{key}={arguments[key]}" for key in present)
    return f"{name}({detail})" if detail else name
