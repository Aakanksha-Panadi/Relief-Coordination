import { useState } from 'react'

/**
 * Road closure controls.
 *
 * Edges carrying a planned route are listed first and marked, because those
 * are the ones that actually trigger a reroute — closing an unused road
 * correctly reports "0 affected" and looks like nothing happened.
 */
export default function ReplanBar({ graph, plan, busy, onClose, onReopen }) {
  const [edgeId, setEdgeId] = useState('')

  if (!graph) return null

  const closed = new Set(graph.closed_edges ?? [])
  const inUse = new Set(
    (plan?.assignments ?? []).flatMap((a) => (a.new_route ?? a.route)?.edges ?? []),
  )

  const open = Object.entries(graph.edges)
    .filter(([id]) => !closed.has(id))
    .sort(([a], [b]) => {
      const rank = (inUse.has(b) ? 1 : 0) - (inUse.has(a) ? 1 : 0)
      return rank !== 0 ? rank : a.localeCompare(b)
    })

  const selected = edgeId || open[0]?.[0] || ''

  return (
    <div className="replan-bar">
      <div className="intake-label">
        <span>Simulate disruption</span>
        <span className="intake-hint">
          {inUse.size > 0 ? '★ = on an active route' : 'generate a plan first'}
        </span>
      </div>

      <div className="replan-row">
        <select
          className="select"
          value={selected}
          onChange={(e) => setEdgeId(e.target.value)}
          disabled={busy.replan || open.length === 0}
        >
          {open.map(([id, edge]) => (
            <option key={id} value={id}>
              {inUse.has(id) ? '★ ' : ''}{edge.name} ({id}) · {edge.minutes}m
            </option>
          ))}
        </select>

        <button
          className="btn-danger"
          onClick={() => selected && onClose(selected)}
          disabled={busy.replan || !selected}
        >
          {busy.replan ? 'Replanning…' : 'Close road'}
        </button>
      </div>

      {closed.size > 0 && (
        <div className="replan-row" style={{ marginTop: 7 }}>
          <span className="intake-hint" style={{ flex: 1 }}>
            Closed: {[...closed].join(', ')}
          </span>
          {[...closed].map((id) => (
            <button
              key={id}
              className="ghost-btn"
              style={{ padding: '5px 9px', fontSize: 11 }}
              onClick={() => onReopen(id)}
              disabled={busy.reopen}
            >
              Reopen {id}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
