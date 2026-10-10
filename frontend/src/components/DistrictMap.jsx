import { useMemo } from 'react'

const W = 820
const H = 620
const PAD = 54

const KIND_COLOR = {
  depot: '#0f172a',
  hospital: '#dc2626',
  shelter: '#16a34a',
  bridge: '#d97706',
  jetty: '#2563eb',
  ward: '#64748b',
}

/**
 * Project the graph's lat/lng into SVG space.
 *
 * Computed from the actual node extent rather than hardcoded, so the map
 * still fits if nodes are added or moved in road_graph.json. Latitude is
 * flipped because SVG y grows downward.
 */
function useProjection(nodes) {
  return useMemo(() => {
    const list = Object.values(nodes ?? {})
    if (list.length === 0) return null

    const lats = list.map((n) => n.lat)
    const lngs = list.map((n) => n.lng)
    const minLat = Math.min(...lats)
    const maxLat = Math.max(...lats)
    const minLng = Math.min(...lngs)
    const maxLng = Math.max(...lngs)

    // Guard against a degenerate span (all nodes on one line).
    const spanLat = maxLat - minLat || 1e-6
    const spanLng = maxLng - minLng || 1e-6

    return (node) => ({
      x: PAD + ((node.lng - minLng) / spanLng) * (W - PAD * 2),
      y: PAD + ((maxLat - node.lat) / spanLat) * (H - PAD * 2),
    })
  }, [nodes])
}

export default function DistrictMap({
  graph,
  plan,
  replan,
  selectedRequestId,
  requests,
  onCloseEdge,
}) {
  const project = useProjection(graph?.nodes)

  if (!graph || !project) {
    return (
      <div className="map-panel">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="spinner" />
          <div className="empty-sub">Loading district map…</div>
        </div>
      </div>
    )
  }

  const nodes = graph.nodes
  const closed = new Set(graph.closed_edges ?? [])
  const points = Object.fromEntries(
    Object.entries(nodes).map(([id, n]) => [id, project(n)]),
  )

  // Routes currently on screen. After a replan the new route is what matters.
  const assignments = replan?.assignments ?? plan?.assignments ?? []
  const activeEdges = new Map()
  const newEdges = new Set()

  for (const a of assignments) {
    const route = a.new_route ?? a.route
    for (const edgeId of route?.edges ?? []) {
      activeEdges.set(edgeId, a)
      if (a.new_route) newEdges.add(edgeId)
    }
  }

  const selectedRequest = requests?.find((r) => r.requestId === selectedRequestId)
  const selectedNode = selectedRequest?.node_id

  return (
    <div className="map-panel">
      <div className="map-overlay">
        <span className="map-chip mc-boat">{assignments.length} active routes</span>
        {newEdges.size > 0 && <span className="map-chip mc-new">rerouted</span>}
        {closed.size > 0 && (
          <span className="map-chip mc-closed">
            {closed.size} road{closed.size > 1 ? 's' : ''} closed
          </span>
        )}
      </div>

      <svg className="map-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
        <defs>
          <pattern id="dots" width="28" height="28" patternUnits="userSpaceOnUse">
            <circle cx="1.5" cy="1.5" r="1.1" fill="#dbe3ec" />
          </pattern>
          <marker id="arrowBlue" viewBox="0 0 10 10" refX="9" refY="5"
                  markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#2563eb" />
          </marker>
          <marker id="arrowGreen" viewBox="0 0 10 10" refX="9" refY="5"
                  markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#16a34a" />
          </marker>
        </defs>

        <rect width={W} height={H} fill="url(#dots)" />

        {/* Roads */}
        {Object.entries(graph.edges).map(([edgeId, edge]) => {
          const a = points[edge.a]
          const b = points[edge.b]
          if (!a || !b) return null

          const isClosed = closed.has(edgeId)
          const isNew = newEdges.has(edgeId)
          const isActive = activeEdges.has(edgeId)

          let stroke = '#bfccbf'
          let width = 3
          if (isActive) { stroke = '#2563eb'; width = 4 }
          if (isNew) { stroke = '#16a34a'; width = 4.5 }
          if (isClosed) { stroke = '#ef4444'; width = 3 }

          return (
            <g key={edgeId}>
              <line
                className="map-edge"
                x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                stroke={stroke}
                strokeWidth={width}
                strokeLinecap="round"
                strokeDasharray={isClosed ? '8 6' : undefined}
                opacity={isClosed ? 0.95 : isActive ? 0.9 : 0.55}
                markerEnd={isNew ? 'url(#arrowGreen)' : isActive ? 'url(#arrowBlue)' : undefined}
                onClick={() => onCloseEdge?.(edgeId)}
              >
                <title>
                  {`${edge.name} (${edgeId}) — ${edge.minutes} min`}
                  {isClosed ? ' — CLOSED' : ' — click to close'}
                </title>
              </line>
              <text
                className="edge-label"
                x={(a.x + b.x) / 2}
                y={(a.y + b.y) / 2 - 5}
                textAnchor="middle"
              >
                {edge.minutes}m
              </text>
            </g>
          )
        })}

        {/* Nodes */}
        {Object.entries(nodes).map(([id, node]) => {
          const p = points[id]
          const isSelected = id === selectedNode
          const demand = requests?.filter(
            (r) => r.node_id === id && r.status !== 'SERVED',
          ).length ?? 0

          return (
            <g key={id}>
              {isSelected && (
                <circle cx={p.x} cy={p.y} r="19" fill="#ef4444" opacity="0.18">
                  <animate attributeName="r" values="15;23;15" dur="1.8s" repeatCount="indefinite" />
                </circle>
              )}
              <circle
                cx={p.x} cy={p.y} r={demand > 0 ? 8 : 6}
                fill={KIND_COLOR[node.kind] ?? '#64748b'}
                stroke="#fff" strokeWidth="2"
              >
                <title>{`${id} — ${node.name} (${node.kind})`}</title>
              </circle>
              {demand > 0 && (
                <text
                  x={p.x} y={p.y + 3} textAnchor="middle"
                  fontSize="8" fontWeight="800" fill="#fff"
                >
                  {demand}
                </text>
              )}
              <text className="node-label" x={p.x} y={p.y - 13} textAnchor="middle">
                {node.name}
              </text>
            </g>
          )
        })}
      </svg>

      <div className="map-foot">
        Synthetic district graph · {Object.keys(nodes).length} nodes ·{' '}
        {Object.keys(graph.edges).length} roads · click a road to close it
      </div>
    </div>
  )
}
