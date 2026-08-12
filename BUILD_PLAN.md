# FreshFlow prototype: build plan

## Context you need before touching anything

This is a take-home for an FDE role at a grocery-tech company. A panel will
stress-test the architecture, then pick one feature to extend **live, on screen,
in my own editor**. Optimize for code I can read aloud and change under pressure.
Clever is a liability here.

The design partner is Meridian Markets. A 90-day extract of 10 stores sits in
`data/` as three CSVs. Shrink is derived, not stored:

    shrink_units = units_shipped - units_sold, over a period
    units_shipped = cases_received * case_size

Two business definitions are genuinely contested inside Meridian, and the data
dictionary calls both out:

1. **Units or cost.** Shrink counted in units, or in dollars via `unit_cost`.
   They do not move together. May to June: units +12.7%, cost -0.6%.
2. **Scope.** Ops means the five fresh departments and excludes Grocery.
   Not everyone does. Grocery is about 4% of unit shrink.

**Handling those two well is the whole assignment.** A number without its
definition is worse than no number. The system must name which definition it
used, say why, and offer the alternative. Never resolve silently.

There is a second planted story in the data, which is the demo: Northeast Dairy
shrink rate goes 4.3%, 4.3%, then 9.0% in June while other regions stay flat.
Shipments held (76,421 to 76,244) and sales fell 5.2%. That makes it a demand
problem, not an over-ordering problem, and those imply opposite corrective
actions.

## Hard constraints

- **Do not modify `metrics.py` or `verify.py` unless a task says so.** They are
  the correctness foundation and `verify.py` currently passes 14/14.
- `verify.py` must keep passing after every task. Run it before you say done.
- **The model never emits a number.** Numbers reach the user only from tool
  results. This is the core trust property.
- Everything must run locally with no network, via a fallback path. AWS is the
  primary demo path but must never be the only path.
- No new dependencies beyond: `pandas`, `duckdb`, `streamlit`, `boto3`.
- Comments explain *why*, not *what*. I have to defend every line.

## Current state

    freshflow/
      metrics.py     metric layer: Data, Period, Filters, shrink, compare,
                     rank, drivers, mix_effect, Provenance. Definitions live in
                     DEPT_SCOPES and MEASURES. Do not touch.
      verify.py      recomputes key numbers from raw CSVs and asserts the metric
                     layer agrees. 14/14 passing. Do not touch.
      router.py      single-shot LLM router: question -> one validated intent.
                     Being replaced in phase 2.
      answer.py      intent -> executed result -> templated narrative, plus the
                     definition-choice panel. Keep the templating and the
                     _choices logic; the entry point changes in phase 2.
      demo.py        terminal harness over five demo questions.
      app.py         Streamlit UI with the trust panel.
      data/          items.csv, shipments.csv, sales_daily.csv

Known rough edges, all deliberate: only three question types, no charts, no
conversation memory, keyword fallback router is crude.

---

## Phase 1: model adapter (do this first, ~20 min)

Create `llm.py` with one interface and two implementations.

    class LLM(Protocol):
        def converse(self, system: str, messages: list, tools: list) -> dict

- `BedrockLLM` using `boto3` `bedrock-runtime.converse` with tool use. Read
  region and model id from env (`AWS_REGION`, `BEDROCK_MODEL_ID`).
- `LocalLLM` using the Anthropic API if `ANTHROPIC_API_KEY` is set.
- Selection via `FRESHFLOW_BACKEND=bedrock|local|none`. Default `none` so a
  clone with no credentials still runs.

Normalize both to one response shape so nothing downstream branches on backend.

**Acceptance:** `python -c "import llm; print(llm.get_backend())"` works with no
credentials set and reports `none`. `verify.py` still passes.

---

## Phase 2: agent tool loop (the main event, ~45 min)

Replace the single-shot router with a real tool-use loop. This is what I pitched
in the first interview and it fixes the coverage complaint: an agent that can
call `shrink` twice with different filters answers questions no template covers.

Create `tools.py`:

- JSON schemas for four tools: `shrink`, `compare`, `rank`, `drivers`. Argument
  names mirror `metrics.py` exactly.
- **`measure` and `scope` must be nullable in every schema**, and the
  description must say null means "the user did not specify." The model must be
  able to decline to resolve the ambiguity. This is the grounding mechanism.
- A dispatcher that validates arguments against the actual data before
  executing. Unresolvable entity means an error result the model can see and
  react to, never a guess.
- Every tool result carries the resolved definition and a `values` list of every
  number in the result. Phase 3 needs that list.

Create `agent.py`:

- Loop: send question and tool schemas, execute any tool calls, feed results
  back, repeat. Cap at 5 iterations, then stop and report.
- Keep a transcript of every tool call and result for the UI.
- System prompt: domain vocabulary, the shrink definition, the two ambiguities
  with explicit instruction not to resolve them alone, and a hard rule that
  every number in the response must come from a tool result.

Keep `answer.py`'s `_choices` logic. Wire it to read the resolved definition off
the tool results so the panel still renders.

Preserve the keyword fallback from `router.py` for the `none` backend.

**Acceptance:** all five questions in `demo.py` run under `FRESHFLOW_BACKEND=none`
and under `bedrock` if credentials exist. "Why is dairy shrink up in the
Northeast in June?" produces at least two tool calls. "How did strawberry sales
trend?" is declined rather than stretched.

---

## Phase 3: numeric provenance check (~20 min, do not skip)

Once the model writes the prose, hallucinated numbers become possible again.
This closes it and it is the best answer I have for "why should a user trust
this."

Create `provenance.py`:

- Extract every numeral from the model's final text, including percentages,
  currency, and thousands separators.
- Assert each traces to a value in some tool result. Allow derived values that
  the tool result supports: rounding, a percentage computed from two values in
  the same result, a difference between two values in the same result. Be
  explicit about which derivations are allowed; do not allow arbitrary
  arithmetic.
- Return `(ok, [unverified_numbers])`.
- Unverified numbers get flagged in the UI, not silently dropped.

Write `test_provenance.py` with cases: exact match, rounded, percentage derived
from two result values, and a fabricated number that must fail.

**Acceptance:** the fabricated case fails, the three legitimate cases pass.

---

## Phase 4: Guardrails on input (~20 min)

`ApplyGuardrail` on the user's question only, for prompt injection and PII.
Standalone boto3 call, no infrastructure.

Deliberately **not** using contextual grounding for numeric correctness. It
scores generated text against source text, so a wrong number reported faithfully
still passes. Phase 3 handles that deterministically. Put that reasoning in a
comment; I will be asked about it.

Skip silently if no guardrail id is configured.

**Acceptance:** app runs unchanged with no guardrail configured.

---

## Phase 5: UI and packaging (~25 min)

Update `app.py`:

- Answer, then the definition panel, then an expander with the full tool
  transcript: each call, its arguments, and its result.
- Backend indicator in the sidebar (bedrock / local / none).
- Provenance warnings rendered prominently when any number is unverified.

Add a `Dockerfile`, python:3.11-slim, non-root user, expose 8501. Do not set up
a cluster. It should be containerized and orchestrator-ready, which is honest,
and nobody expects a provisioned EKS cluster from a two-hour take-home.

Add a `README.md`: what was built, what was cut and why, the two ambiguities and
how they are handled, how to run both backends, and known rough edges.

**Acceptance:** `docker build` succeeds. App runs with zero credentials.

---

## Explicitly out of scope

- Text-to-SQL. The pitch was tools, not SQL. A guarded SQL escape hatch over a
  semantic view is the stated next increment, not this build.
- Bedrock Knowledge Bases. Vector retrieval cannot compute a SUM over 181k rows,
  and the structured-retrieval variant is managed NL2SQL I cannot edit during a
  live session.
- Charts, conversation memory, auth, caching.
- Any question type outside shrink.

## Order of work

Phases 1, 2, 3 are the demo. 4 and 5 are polish. If time runs short, stop after
3 and tell me what is missing rather than rushing the rest.
