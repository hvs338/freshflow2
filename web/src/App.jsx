import { useEffect, useRef, useState } from 'react'
import Answer from './Answer'
import { ask, getMeta } from './api'

export default function App() {
  const [meta, setMeta] = useState(null)
  const [turns, setTurns] = useState([])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const bottom = useRef(null)

  useEffect(() => {
    getMeta().then(setMeta).catch(() => setMeta({ error: true }))
  }, [])

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns, busy])

  async function send(question) {
    const q = question.trim()
    if (!q || busy) return
    setDraft('')
    setTurns((t) => [...t, { role: 'user', text: q }])
    setBusy(true)
    try {
      const result = await ask(q)
      setTurns((t) => [...t, { role: 'assistant', result }])
    } catch (e) {
      setTurns((t) => [...t, { role: 'error', text: String(e.message || e) }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="shell">
      <Sidebar meta={meta} busy={busy} onPick={send} />

      <main className="main">
        <header className="header">
          <h1>Ask about Meridian shrink</h1>
          <p className="sub">
            {meta && !meta.error
              ? `${meta.stores} stores, ${meta.coverage.start} to ${meta.coverage.end}. Ask anything about shrink or sales; every answer shows the SQL behind it.`
              : 'Loading…'}
          </p>
        </header>

        <div className="thread">
          {turns.length === 0 && <Empty meta={meta} onPick={send} />}

          {turns.map((turn, i) => {
            if (turn.role === 'user') {
              return (
                <div key={i} className="turn turn-user">
                  <div className="bubble">{turn.text}</div>
                </div>
              )
            }
            if (turn.role === 'error') {
              return (
                <div key={i} className="turn">
                  <p className="notice notice-alert">{turn.text}</p>
                </div>
              )
            }
            return (
              <div key={i} className="turn">
                <Answer answer={turn.result} />
              </div>
            )
          })}

          {busy && (
            <div className="turn">
              <p className="thinking">
                <span className="dot" /> Writing SQL and running it…
              </p>
            </div>
          )}
          <div ref={bottom} />
        </div>

        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault()
            send(draft)
          }}
        >
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Ask about shrink…"
            disabled={busy}
            autoFocus
          />
          <button type="submit" disabled={busy || !draft.trim()}>
            Ask
          </button>
        </form>
      </main>
    </div>
  )
}

function Empty({ meta, onPick }) {
  if (!meta || meta.error) return null
  return (
    <div className="empty">
      <p>
        Every number comes from a SQL query you can read, shown under each
        answer with the rows it returned.
      </p>
      <div className="chips">
        {meta.examples.map((e) => (
          <button key={e} className="chip" onClick={() => onPick(e)}>
            {e}
          </button>
        ))}
      </div>
    </div>
  )
}

function Sidebar({ meta, busy, onPick }) {
  if (!meta) return <aside className="sidebar" />
  if (meta.error) {
    return (
      <aside className="sidebar">
        <p className="notice notice-alert">
          Cannot reach the API. Start it with <code>python api.py</code>.
        </p>
      </aside>
    )
  }

  // Which backend answered is not a debug detail. `none` means no model ran at
  // all and the deterministic keyword path produced the answer, which changes
  // how much the phrasing should be trusted.
  const backends = {
    bedrock: ['Bedrock', 'Writes SQL via Bedrock Converse.'],
    local: ['Anthropic API', 'Writes SQL via the Anthropic API.'],
    none: ['No model', 'No model configured, so no SQL can be written.'],
  }
  const [label, note] = backends[meta.backend] || ['Unknown', '']

  return (
    <aside className="sidebar">
      <div className="brand">FreshFlow</div>

      <div className={`backend backend-${meta.backend}`}>
        <span className="backend-dot" />
        <div>
          <p className="backend-name">{label}</p>
          <p className="backend-note">{note}</p>
        </div>
      </div>

      <section>
        <h5>Try one</h5>
        {meta.examples.map((e) => (
          <button
            key={e}
            className="example"
            disabled={busy}
            onClick={() => onPick(e)}
          >
            {e}
          </button>
        ))}
      </section>

      <section>
        <h5>The table</h5>
        <dl className="facts">
          <dt>Grain</dt>
          <dd>one row per store-item-day</dd>
          <dt>Covers</dt>
          <dd>{meta.coverage.start} to {meta.coverage.end}</dd>
          <dt>Departments</dt>
          <dd>{meta.departments.join(', ')}</dd>
          <dt>Regions</dt>
          <dd>{meta.regions.join(', ')}</dd>
          <dt>Banners</dt>
          <dd>{meta.banners.join(', ')}</dd>
        </dl>
      </section>

      <section>
        <h5>Metrics it knows</h5>
        {/* Named so "shrink rate" is one definition, not whatever the model
            improvises this time. These go into the prompt verbatim. */}
        <dl className="facts">
          {Object.entries(meta.metrics).map(([name, sql]) => (
            <div key={name}>
              <dt>{name}</dt>
              <dd className="metric-sql">{sql}</dd>
            </div>
          ))}
        </dl>
        <p className="fine">
          No conversation memory: each question is answered on its own, so an
          answer never depends on something you cannot see.
        </p>
      </section>
    </aside>
  )
}
