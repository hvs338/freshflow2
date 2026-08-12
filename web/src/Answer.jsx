import { useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// One answer, in the order the trust argument runs: what the model said, what
// it decided on your behalf, then every query it ran with the rows that came
// back. Nothing verifies the prose for you -- the evidence is just here, so a
// number that looks wrong can be checked against the table that produced it.
export default function Answer({ answer }) {
  return (
    <div className="answer">
      <div className="prose">
        <Markdown remarkPlugins={[remarkGfm]}>{answer.text}</Markdown>
      </div>

      {answer.warnings.map((w, i) => (
        <p key={i} className="notice">{w}</p>
      ))}

      <Definitions choices={answer.choices} />
      <Evidence steps={answer.steps} />
    </div>
  )
}

// The two contested definitions, named every time. This is the assignment, so
// it is not hidden behind a toggle.
function Definitions({ choices }) {
  if (!choices?.length) return null
  return (
    <section className="definitions">
      <h4>What I decided for you</h4>
      <div className="definition-grid">
        {choices.map((c) => (
          <div key={c.question} className="definition">
            <p className="definition-q">{c.question}</p>
            <p className="definition-a">
              {c.chosen}
              {c.defaulted && <span className="tag">default</span>}
            </p>
            <p className="definition-why">{c.why}</p>
            {c.alternative && (
              <p className="definition-alt">Alternative: {c.alternative}</p>
            )}
          </div>
        ))}
      </div>
    </section>
  )
}

function Evidence({ steps }) {
  const [open, setOpen] = useState(false)
  if (!steps?.length) return null

  const failed = steps.filter((s) => !s.ok).length
  return (
    <section className="evidence">
      <button className="disclosure" onClick={() => setOpen(!open)}>
        <span className="chevron" data-open={open}>›</span>
        Show the {steps.length} quer{steps.length === 1 ? 'y' : 'ies'} behind this
        {failed > 0 && <span className="pill pill-error">{failed} rejected</span>}
      </button>

      {open && steps.map((step, i) => (
        <div key={i} className="step">
          <p className="step-purpose">
            {/* Which tool ran matters: the named four carry the reviewed shrink
                logic, `query` runs whatever SQL the model wrote. */}
            <span className={`pill ${step.tool === 'query' ? 'pill-raw' : 'pill-tool'}`}>
              {step.tool}
            </span>
            {step.purpose}
            <span className={`pill ${step.ok ? 'pill-ok' : 'pill-error'}`}>
              {step.ok ? `${step.rows.length} rows` : 'rejected'}
            </span>
          </p>
          {step.sql && <pre className="sql">{step.sql.trim()}</pre>}
          {step.ok ? <ResultTable step={step} /> : <p className="notice">{step.error}</p>}
          {step.notes?.length > 0 && (
            <ul className="step-notes">
              {step.notes.map((n, j) => <li key={j}>{n}</li>)}
            </ul>
          )}
        </div>
      ))}
    </section>
  )
}

// The point of the whole panel: the rows the number actually came from.
function ResultTable({ step }) {
  if (!step.rows.length) return <p className="empty-rows">No rows.</p>
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>{step.columns.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {step.rows.map((row, i) => (
            <tr key={i}>
              {step.columns.map((c) => (
                <td key={c} className={typeof row[c] === 'number' ? 'num' : ''}>
                  {format(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function format(value) {
  if (value === null || value === undefined) return '—'
  if (typeof value !== 'number') return String(value)
  // Rates come back as fractions. Showing four places keeps 0.0900 readable
  // without pretending it is a percentage the query did not ask for.
  const digits = Math.abs(value) < 1 && !Number.isInteger(value) ? 4 : 2
  return value.toLocaleString(undefined, { maximumFractionDigits: digits })
}
