import { useEffect, useRef, useState } from 'react'
import { API_BASE } from '../api'

/**
 * Everything that used to own a panel or a footer strip.
 *
 * The road-closure simulator was a block in the right rail and the five
 * health dots were a permanent footer; both are controls you touch rarely
 * and read at a glance, so they live here instead of costing the map space.
 */
export default function TopBar({
  health,
  summary,
  requests,
  graph,
  plan,
  tab,
  onTab,
  onCloseRoad,
  onReopenRoad,
  onReset,
  onReleaseAll,
  busy,
}) {
  const pending = requests.filter((r) => r.status === 'PENDING').length
  const critical = requests.filter(
    (r) => r.urgency === 'CRITICAL' && r.status !== 'SERVED',
  ).length

  // The payload is {by_type, source}; counting its top level instead of
  // by_type is what made this tile read 0 whatever the fleet was doing.
  const byType = summary?.by_type ?? {}
  const available = Object.values(byType)
    .filter((s) => s.dispatchable !== false)
    .reduce((n, s) => n + (s.available ?? 0), 0)

  // Mock mode is honest state, not a failure — but it must be visible so a
  // demo is never mistaken for live Gemini output.
  const mode = !health
    ? { cls: 'down', text: 'connecting' }
    : health.mock_mode
      ? { cls: 'warn', text: 'MOCK MODE' }
      : { cls: '', text: 'LIVE GEMINI' }

  return (
    <div className="topbar">
      <div className="logo">
        <div className="logo-mark">R</div>
        <div>
          <div className="logo-name">RescueIQ</div>
          <div className="logo-sub">Relief coordination</div>
        </div>
      </div>

      <div className={`live-pill ${mode.cls}`}>
        <span className="live-dot" />
        {mode.text}
      </div>

      <div className="nav">
        <button
          className={`nav-btn${tab === 'command' ? ' active' : ''}`}
          onClick={() => onTab('command')}
        >
          Command centre
        </button>
        <button
          className={`nav-btn${tab === 'results' ? ' active' : ''}`}
          onClick={() => onTab('results')}
        >
          Results
        </button>
      </div>

      <div className="topbar-right">
        <div className="stat-run">
          <span className="stat"><b>{pending}</b><span>pending</span></span>
          <span className={`stat${critical > 0 ? ' danger' : ''}`}>
            <b>{critical}</b><span>critical</span>
          </span>
          <span className={`stat${available > 0 ? ' ok' : ''}`}>
            <b>{available}</b><span>free</span>
          </span>
        </div>

        <span className="vrule" />

        <ClosureControl
          graph={graph}
          plan={plan}
          busy={busy}
          onClose={onCloseRoad}
          onReopen={onReopenRoad}
        />

        <HealthPill health={health} graph={graph} />

        <OverflowMenu busy={busy} onReset={onReset} onReleaseAll={onReleaseAll} />
      </div>
    </div>
  )
}

/**
 * One select and one button for both directions.
 *
 * Edges carrying a planned route sort first and are marked, because those
 * are the ones that actually force a reroute — closing an unused road
 * correctly reports "0 affected" and looks like nothing happened.
 */
function ClosureControl({ graph, plan, busy, onClose, onReopen }) {
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

  // Keep the operator's choice while it still exists, otherwise fall back to
  // the first open road so the button always has a target.
  const selected = (edgeId && graph.edges[edgeId]) ? edgeId : (open[0]?.[0] ?? '')
  const isClosed = closed.has(selected)
  const working = busy.replan || busy.reopen

  return (
    <div className="closure-ctl">
      <select
        className="road-select"
        value={selected}
        onChange={(e) => setEdgeId(e.target.value)}
        disabled={working}
        title={inUse.size > 0 ? '★ = on an active route' : 'generate a plan first'}
      >
        {open.length > 0 && (
          <optgroup label="Open roads">
            {open.map(([id, edge]) => (
              <option key={id} value={id}>
                {inUse.has(id) ? '★ ' : ''}{edge.name} · {edge.minutes}m
              </option>
            ))}
          </optgroup>
        )}
        {closed.size > 0 && (
          <optgroup label="Closed">
            {[...closed].map((id) => (
              <option key={id} value={id}>
                {graph.edges[id]?.name ?? id} · closed
              </option>
            ))}
          </optgroup>
        )}
      </select>

      {isClosed ? (
        <button
          className="btn-road reopen"
          onClick={() => onReopen(selected)}
          disabled={working || !selected}
        >
          {busy.reopen ? 'Reopening…' : 'Reopen road'}
        </button>
      ) : (
        <button
          className="btn-road close"
          onClick={() => onClose(selected)}
          disabled={working || !selected}
        >
          {busy.replan ? 'Replanning…' : 'Close road'}
        </button>
      )}
    </div>
  )
}

/** The former footer, collapsed to one pill that opens on hover or focus. */
function HealthPill({ health, graph }) {
  const closedCount = graph?.closed_edges?.length ?? 0
  const rows = [
    { tone: health ? 'green' : 'red', label: `API ${health ? 'reachable' : 'unreachable'}`, hint: API_BASE },
    { tone: health?.firestore ? 'green' : 'amber', label: `Firestore ${health?.firestore ? 'connected' : 'in-memory'}` },
    {
      tone: health?.resource_source === 'firestore' ? 'green' : 'amber',
      label: `Fleet: ${health?.resource_source ?? '—'} (${health?.resource_count ?? 0})`,
    },
    { tone: health?.mock_mode ? 'amber' : 'blue', label: health?.mock_mode ? 'Mock extraction' : 'Gemini live' },
  ]
  if (closedCount > 0) {
    rows.push({ tone: 'red', label: `${closedCount} road${closedCount > 1 ? 's' : ''} closed` })
  }

  // The worst dot wins, so a problem is visible without opening anything.
  const tone = !health ? 'red' : closedCount > 0 || health.mock_mode || !health.firestore ? 'amber' : 'green'
  const summary = !health ? 'Offline' : tone === 'amber' ? 'Check status' : 'All systems'

  return (
    <div className="health" tabIndex={0}>
      <div className="health-pill">
        <span className={`fi-dot ${tone}`} />
        {summary}
      </div>
      <div className="health-pop">
        {rows.map((r) => (
          <div className="hp-row" key={r.label}>
            <span className={`fi-dot ${r.tone}`} />
            <span>{r.label}</span>
            {r.hint && <code className="hp-hint">{r.hint}</code>}
          </div>
        ))}
        <div className="hp-foot">Gemini understands · Python calculates · Humans approve</div>
      </div>
    </div>
  )
}

/** Reset and Release all are destructive and rare; they do not need to shout. */
function OverflowMenu({ busy, onReset, onReleaseAll }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => { if (!ref.current?.contains(e.target)) setOpen(false) }
    const onEsc = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onEsc)
    }
  }, [open])

  return (
    <div className="overflow" ref={ref}>
      <button
        className="overflow-btn"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="More actions"
      >
        ⋯
      </button>
      {open && (
        <div className="overflow-menu" role="menu">
          <button
            role="menuitem"
            onClick={() => { setOpen(false); onReleaseAll() }}
            disabled={busy.release}
          >
            Release all resources
            <span>Force every unit back to available</span>
          </button>
          <button
            role="menuitem"
            className="danger"
            onClick={() => { setOpen(false); onReset() }}
            disabled={busy.reset}
          >
            {busy.reset ? 'Resetting…' : 'Reset everything'}
            <span>Clear requests, plan and road closures</span>
          </button>
        </div>
      )}
    </div>
  )
}
