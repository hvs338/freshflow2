# FreshFlow

Ask questions about Meridian Markets' shrink and sales in English. The model
picks a tool, the tool runs SQL against one governed view, and every answer
shows the queries and the rows behind it.

## Run it

```bash
pip install -r requirements.txt

python verify.py                              # 21 checks, no model needed
FRESHFLOW_BACKEND=bedrock python demo.py      # terminal
FRESHFLOW_BACKEND=bedrock python api.py       # http://localhost:8501
```

`FRESHFLOW_BACKEND` is `bedrock`, `local` (Anthropic API), or `none`. For UI
development, `cd web && npm run dev` serves on 5173 and proxies `/api` to 8501.
`npm run build` lets `api.py` serve the UI itself, so the demo is one port.

## The seven files

Three of them are the spine, and they split cleanly into meaning, query, and
execution:

| File | Job | Lines |
|---|---|---|
| `metrics.py` | **Meaning.** What the numbers and columns mean. No imports, no database. | ~195 |
| `queries.py` | **Query.** The SQL for the four named questions, built from those definitions. | ~230 |
| `semantic.py` | **Execution.** One view everything reads, and the gate in front of it. | ~195 |
| `tools.py` | What the model may ask for, and every argument checked first. | ~330 |
| `router.py` | The prompt, the SQL escape hatch, the disclosure. | ~190 |
| `agent.py` | The conversation loop. Ask, call the tool, feed it back, repeat. | ~175 |
| `llm.py` | Bedrock or Anthropic behind one interface, so nothing else branches. | ~200 |

The dependency runs one way — `semantic` and `queries` both read `metrics`,
which reads nothing — but the calls run the other: `queries.shrink(sem, ...)`
takes the view as an argument rather than importing it. That is why `metrics.py`
can be imported with no database at all, and why there is no import cycle.

Plus `api.py` (HTTP), `verify.py` (correctness), `demo.py` (terminal), `web/`
(React).

## How it answers anything

Five tools. Four are named questions, computed by reviewed code in `metrics.py`:

| Tool | Question |
|---|---|
| `shrink` | how much shrink in a month, optionally split one way |
| `compare` | up or down versus another month, deltas on both measures |
| `rank` | top or bottom N of a dimension |
| `drivers` | why a move happened: which slices, and shipments-vs-sales for each |

The fifth is `query`: one read-only SELECT against `daily`, one row per
store-item-day. It is the escape hatch, so "which week did Northeast Dairy fall
off" and "best sellers in Produce" need no new code — they were never
enumerated, and they work.

Both paths end in the same place. A typed tool builds its SQL from the same
`METRICS` fragments the prompt shows, runs it through the same gate, and the
generated SQL is displayed as evidence exactly like SQL the model wrote. The
difference is who chose the arithmetic. `drivers` always decomposes the same
way and always splits an ordering problem from a demand problem; a model asked
to do that in freehand SQL may or may not.

The four things that keep that safe:

**The join is stated once.** Shipments are deliveries — episodic. Sales are
daily. 77% of sales rows have no same-day shipment, so joining the two facts
`ON` the date throws most sales away and overstates June shrink by ~860%. The
error looks entirely plausible. The model is never given the raw tables; it gets
a view that already did the full outer join at store-item-day, zero-filled, so a
`SUM` over any period and any grouping is right. `verify.py` recomputes from the
raw CSVs in pandas and asserts the view agrees.

**The gate.** One statement, `SELECT`/`WITH` only, a token denylist, exactly one
readable table, a row cap applied by the harness, and a DuckDB connection with
`enable_external_access` off. A rejected query is returned to the model as a
result, not raised — it reads the reason and fixes its SQL.

**The metrics are named.** `shrink_rate` is one expression in `metrics.py`, put
into the prompt verbatim and interpolated into every typed tool's SQL, rather
than whatever the model improvises each time.

**The data model is stated, not implied.** A column list is not a data model.
`metrics.COLUMNS` says what each column means — `net_sales` is the only retail
column, the cost columns are cost, `unit_cost` is one stable value per item —
and `data_model()` appends every value each dimension actually takes, read from
the view at startup so it cannot go stale. The typed tools don't need it,
because their arguments are validated against the data. `query` does: it writes
SQL from scratch, so what isn't in the prompt isn't known. `verify.py` asserts
the described columns and the real columns are the same set.

**Arguments are checked before any SQL is built.** A month outside the extract,
an unresolvable region, a dimension that does not exist — each comes back as an
error the model reads and corrects. A filter is never silently dropped, because
a dropped filter is a number computed over the wrong rows that still looks
right. This is also why the string interpolation in `metrics.py` is safe:
nothing reaches a SQL builder that did not first match a real value.

**The evidence is shown.** Every answer renders the SQL and the rows it
returned — for typed tools, the SQL the harness generated, which the model
never saw. Nothing machine-verifies the prose; the check is that a wrong number
can be caught by looking at the table directly beneath it.

## The two contested definitions

Meridian's data dictionary calls out two ambiguities, and handling them is the
substance of the assignment:

1. **Units or cost.** They do not move together — May to June, unit shrink is
   +12.7% while cost shrink is −0.6%.
2. **Scope.** Ops means the five fresh departments and excludes Grocery. Not
   everyone does. Grocery is about 4% of unit shrink.

There is arguably a third that the dictionary does not call out. 70 of the 200
items are sold by the pound and 130 by the each, and `shrink_units` adds them
together — June's 61,782 fresh units is 34,576 eaches plus 27,206 pounds. That
is Meridian's own definition, so the number is not wrong, but it is not a count
of things either. `unit_of_measure` is a dimension you can filter and group by,
the prompt says the mixing happens, and `verify.py` asserts the split
reconciles. It is surfaced rather than resolved, because resolving it would mean
changing what shrink means.

Neither is resolved silently. `measure` and `scope` are nullable arguments on
every tool, and null is the expected answer when the user did not say. Scope is
enforced by the harness, not by the model's `WHERE` clause: `daily` is rebound
to the right departments before every query, so identical SQL returns different
numbers under different scopes and the system always knows which it used. Every
answer renders which definition it used, why, and what the alternative is —
marked `default` when the user did not choose it.

`rank` and `drivers` need one column to sort or decompose on, so null cannot
survive there. It becomes `units` plus a note telling the model to say that was
a default and name the alternative — disclosed rather than silent. And when the
two measures disagree, `compare` says so in a note rather than leaving the model
to notice it in four decimal places.

## Known limits

- **No conversation memory.** Each question is resolved on its own, so an answer
  never depends on something you cannot see.
- **`measure` is enforced for `rank` and `drivers` only.** They sort and
  decompose by it. Everywhere else it is a declaration: scope is enforced by
  view binding, but a `query` picks its own columns and `shrink`/`compare`
  return both measures regardless.
- **`FRESHFLOW_BACKEND=none` cannot answer.** Something has to map a question
  onto a tool. The metric layer is callable without a model — `verify.py`
  exercises it that way — so a deterministic keyword path is a small increment,
  but it does not exist.
- **The typed tools are month-grained.** Weekly and daily questions fall through
  to `query`, which does not carry the reviewed shrink logic. Adding a `grain`
  argument is the obvious next step.
- **No numeric provenance check.** An earlier version machine-verified every
  numeral in the prose against the query results. It was dropped: it caught no
  real fabrications in live testing while producing six classes of false
  positive, and showing the rows is a more convincing answer to "how do I know"
  than a green tick. The trade is real — a fabricated number in the prose is now
  caught by a reader, not by code.
- **No input guardrails.** Prompt-injection and PII screening on the question
  would be the next increment.
- **Read-only.** Nothing writes anywhere.
