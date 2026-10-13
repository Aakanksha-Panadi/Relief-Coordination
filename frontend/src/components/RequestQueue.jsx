import { useState } from 'react'
import { formatRequestId } from '../formatters'

const URGENCY_RANK = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }

/** Same ordering the dispatcher uses: urgency first, vulnerable ahead within a tier. */
function byPriority(a, b) {
  const rank =
    (URGENCY_RANK[a.urgency] ?? 9) - (URGENCY_RANK[b.urgency] ?? 9)
  if (rank !== 0) return rank
  return (b.vulnerable ? 1 : 0) - (a.vulnerable ? 1 : 0)
}

export default function RequestQueue({
  requests,
  graph,
  selectedId,
  onSelect,
  onReview,
  reviewBusy,
}) {
  const sorted = requests
    .filter((r) => !r.assignmentId
      && !(Array.isArray(r.assignedResources) && r.assignedResources.length > 0)
      && ['PENDING', 'UNPROCESSED', 'UNREACHABLE'].includes(r.status))
    .sort(byPriority)
  const pending = sorted.length

  return (
    <>
      <div className="panel-head">
        <span className="panel-title">Request queue</span>
        <span className="sort-chip">
          {pending} pending · by urgency
        </span>
      </div>

      <div className="scroll-area">
        {pending === 0 ? (
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
              nodes={graph?.nodes ?? {}}
              onReview={onReview}
              reviewBusy={reviewBusy}
            />
          ))
        )}
      </div>
    </>
  )
}

function RequestCard({ req, selected, onSelect, nodes, onReview, reviewBusy }) {
  const [peopleCount, setPeopleCount] = useState(req.people_count ?? '')
  const [nodeId, setNodeId] = useState(req.node_id ?? '')
  const [urgency, setUrgency] = useState(req.urgency ?? 'MEDIUM')
  const className = [
    'req-card',
    selected ? `sel-${urgency}` : '',
  ].filter(Boolean).join(' ')
  const needsReview = req.status === 'UNPROCESSED' || req.human_review_required

  const submitReview = (event) => {
    event.preventDefault()
    onReview(req.requestId, {
      people_count: Number(peopleCount),
      node_id: nodeId,
      urgency,
    })
  }

  return (
    <div>
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
          <span className="tag request-id" title={req.requestId}>
            {formatRequestId(req.requestId)}
          </span>
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
      {needsReview && (
        <form className="request-review" onSubmit={submitReview}>
          <label>
            People
            <input
              type="number"
              min="1"
              required
              value={peopleCount}
              onChange={(event) => setPeopleCount(event.target.value)}
            />
          </label>
          <label>
            Location node
            <select
              required
              value={nodeId}
              onChange={(event) => setNodeId(event.target.value)}
            >
              <option value="">Choose a verified node</option>
              {Object.entries(nodes).map(([id, node]) => (
                <option key={id} value={id}>{id} · {node.name ?? id}</option>
              ))}
            </select>
          </label>
          <label>
            Urgency
            <select value={urgency} onChange={(event) => setUrgency(event.target.value)}>
              {['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </label>
          <button className="ghost-btn" type="submit" disabled={reviewBusy || !nodes[nodeId]}>
            {reviewBusy ? 'Saving…' : 'Confirm and queue'}
          </button>
        </form>
      )}
    </div>
  )
}
