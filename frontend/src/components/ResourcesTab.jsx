/**
 * The fleet, which until now was a single number in the footer.
 *
 * Shelters are destinations rather than responders, so they are listed under
 * their own heading instead of inflating the count of units you can send.
 */
export default function ResourcesTab({ resources, summary, graph }) {
  const byType = summary?.by_type ?? {}

  if (resources.length === 0) {
    return (
      <div className="tab-body">
        <div className="empty">
          <div className="empty-title">No fleet loaded</div>
          <div className="empty-sub">
            Resources come from Firestore, or from the built-in fallback fleet
            when Firestore is unreachable.
          </div>
        </div>
      </div>
    )
  }

  const dispatchable = resources.filter((r) => byType[r.type]?.dispatchable !== false)
  const sites = resources.filter((r) => byType[r.type]?.dispatchable === false)

  // Free units first — those are the ones an operator is looking for.
  const order = (a, b) =>
    (b.available ? 1 : 0) - (a.available ? 1 : 0) ||
    String(a.resourceId).localeCompare(String(b.resourceId))

  return (
    <div className="tab-body">
      <div className="group">
        <div className="group-head">
          <span>Responders</span>
          <span className="group-count">{dispatchable.length}</span>
          <span className="rule" />
          <span className="group-note">
            {dispatchable.filter((r) => r.available).length} free
          </span>
        </div>
        {[...dispatchable].sort(order).map((r) => (
          <FleetRow key={r.resourceId} r={r} graph={graph} />
        ))}
      </div>

      {sites.length > 0 && (
        <div className="group">
          <div className="group-head">
            <span>Shelters &amp; sites</span>
            <span className="group-count">{sites.length}</span>
            <span className="rule" />
            <span className="group-note">not dispatchable</span>
          </div>
          {[...sites].sort(order).map((r) => (
            <FleetRow key={r.resourceId} r={r} graph={graph} site />
          ))}
        </div>
      )}

      <div className="group">
        <div className="group-head">
          <span>By type</span>
          <span className="rule" />
        </div>
        {Object.entries(byType).map(([type, s]) => (
          <div className="type-row" key={type}>
            <span className="type-name">{type}</span>
            <span className="type-bar">
              <span
                className="type-fill"
                style={{ width: `${s.total ? (s.available / s.total) * 100 : 0}%` }}
              />
            </span>
            <span className="type-nums">
              {s.available}/{s.total} free · {s.in_use}/{s.capacity} loaded
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function FleetRow({ r, graph, site }) {
  const where = graph?.nodes?.[r.node_id]?.name ?? r.location ?? r.node_id ?? '—'
  const load = r.currentLoad ?? 0
  const capacity = r.capacity ?? 0

  return (
    <div className="fleet-row">
      <span className={`fleet-dot ${r.available ? 'free' : 'busy'}`} />
      <span className="fleet-main">
        <span className="fleet-name">{r.name ?? r.resourceId}</span>
        <span className="fleet-sub">
          {where}
          {!site && ` · ${r.available ? 'available' : 'in use'}`}
          {r.fuelLevel && ` · fuel ${r.fuelLevel}`}
        </span>
      </span>
      <span className="fleet-cap">
        {load > 0 ? `${load}/${capacity}` : `cap ${capacity}`}
      </span>
    </div>
  )
}
