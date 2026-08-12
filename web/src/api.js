// The only place the UI talks to Python. Everything else takes plain objects.

async function json(path, options) {
  const res = await fetch(path, options)
  if (!res.ok) throw new Error(`${path} returned ${res.status}`)
  return res.json()
}

export function getMeta() {
  return json('/api/meta')
}

export function ask(question) {
  return json('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
}
