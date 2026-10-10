const URGENCY_RANK = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }

/** Same ordering the dispatcher uses: urgency first, vulnerable ahead within a tier. */
function byPriority(a, b) {
  const rank =
    (URGENCY_RANK[a.urgency] ?? 9) - (URGENCY_RANK[b.urgency] ?? 9)
  if (rank !== 0) return rank
  return (b.vulnerable ? 1 : 0) - (a.vulnerable ? 1 : 0)
}

export default function RequestQueue({ requests, selectedId, onSelect }) {
  const sorted = [...requests].sort(byPriority)
  const pending = sorted.filter((r) => r.status !== 'SERVED')

  return (
    <>
      <div className="panel-head">
        <span className="panel-title">Request queue</span>
        <span className="sort-chip">
          {pending.length} pending · by urgency
        </span>
      </div>

      <div className="scroll-area">
        {sorted.length === 0 ? (
          <div className="empty">
            <div className="empty-title">No requests yet</div>
            <div className="empty-sub">
              Paste a message above and press Process request. It will appear
              here with its extracted urgency and location.
            </div>
          </div>
        ) : (
          sorted.map((req) => (
            <RequestCard
              key={req.requestId}
              req={req}
              selected={req.requestId === selectedId}
              onSelect={onSelect}
            />
          ))
        )}
      </div>
    </>
  )
}

function RequestCard({ req, selected, onSelect }) {
  const urgency = req.urgency ?? 'LOW'
  const className = [
    'req-card',
    selected ? `sel-${urgency}` : '',
  ].filter(Boolean).join(' ')

  return (
    <button
      type="button"
      className={className}
      onClick={() => onSelect(selected ? null : req.requestId)}
    >
      <div className="rh">
        <span className={`badge badge-${urgency}`}>{urgency}</span>
        {req.language && <span className="lang-chip">{req.language}</span>}
        {req.status === 'ASSIGNED' && <span className="status-chip">ASSIGNED</span>}
        {req.status === 'SERVED' && <span className="status-chip">SERVED</span>}
        {req.status === 'UNPROCESSED' && <span className="status-chip">UNPROCESSED · REVIEW</span>}
        {req.status === 'UNREACHABLE' && <span className="status-chip">UNREACHABLE · ESCALATE</span>}
        {req.human_review_required && req.status !== 'UNPROCESSED' && <span className="status-chip">REVIEW REQUIRED</span>}
        {typeof req.confidence === 'number' && (
          <span className="conf-chip">{Math.round(req.confidence * 100)}%</span>
        )}
      </div>

      <div className="req-text">
        {req.location_description || 'Unknown location'}
        {req.node_id ? ` · ${req.node_id}` : ''}
      </div>

      {req.raw_text && <div className="req-raw">{req.raw_text}</div>}

      <div className="tags">
        <span className="tag">{req.requestId}</span>
        <span className="tag">{req.people_count == null ? 'Count unknown' : `${req.people_count} people`}</span>
        {(req.report_count ?? 1) > 1 && <span className="tag">{req.report_count} reports linked</span>}
        {(req.needs ?? []).map((need) => (
          <span className="tag" key={need}>{need}</span>
        ))}
        {req.vulnerable && (
          <span className="warn-chip">
            {req.vulnerable_details || 'vulnerable'}
          </span>
        )}
      </div>
    </button>
  )
}
