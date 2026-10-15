import { useState } from 'react'
import { formatRequestId } from '../formatters'

const URGENCY_RANK = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }

/** Same ordering the dispatcher uses: urgency first, vulnerable ahead within a tier. */
function byPriority(a, b) {
  const rank = (URGENCY_RANK[a.urgency] ?? 9) - (URGENCY_RANK[b.urgency] ?? 9)
  if (rank !== 0) return rank
  return (b.vulnerable ? 1 : 0) - (a.vulnerable ? 1 : 0)
}

function isAssigned(r) {
  return Boolean(r.assignmentId) ||
    (Array.isArray(r.assignedResources) && r.assignedResources.length > 0)
}

/**
 * The whole request ledger, in one place.
 *
 * Pending records are the work, so they keep the full card — raw text in the
 * sender's own script, extracted tags, the vulnerability note. Anything
 * already handled collapses to a line, because it is reference, not a task.
 */
export default function RequestsTab({
  requests,
  graph,
  selectedId,
  onSelect,
  onReview,
  reviewBusy,
}) {
  const pending = requests
    .filter((r) => !isAssigned(r) && ['PENDING', 'UNPROCESSED', 'UNREACHABLE'].includes(r.status))
    .sort(byPriority)
  const assigned = requests.filter((r) => isAssigned(r) && r.status !== 'SERVED')
  const served = requests.filter((r) => r.status === 'SERVED')

  if (requests.length === 0) {
    return (
      <div className="tab-body">
        <div className="empty">
          <div className="empty-title">No requests yet</div>
          <div className="empty-sub">
            Paste a message below and press Process request. It will appear here
            with its extracted urgency and location.
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="tab-body">
      {pending.length > 0 && (
        <div className="group">
          <div className="group-head">
            <span>Pending</span>
            <span className="group-count">{pending.length}</span>
            <span className="rule" />
            <span className="group-note">by urgency</span>
          </div>
          {pending.map((req) => (
            <RequestCard
              key={req.requestId}
              req={req}
              selected={req.requestId === selectedId}
              onSelect={onSelect}
              nodes={graph?.nodes ?? {}}
              onReview={onReview}
              reviewBusy={reviewBusy}
            />
          ))}
        </div>
      )}

      {assigned.length > 0 && (
        <CompactGroup
          label="Assigned" items={assigned} tone="ASSIGNED"
          selectedId={selectedId} onSelect={onSelect}
        />
      )}
      {served.length > 0 && (
        <CompactGroup
          label="Served" items={served} tone="SERVED"
          selectedId={selectedId} onSelect={onSelect}
        />
      )}
    </div>
  )
}

function CompactGroup({ label, items, tone, selectedId, onSelect }) {
  return (
    <div className="group">
      <div className="group-head">
        <span>{label}</span>
        <span className="group-count">{items.length}</span>
        <span className="rule" />
      </div>
      {items.map((req) => (
        <button
          type="button"
          className={`req-row${req.requestId === selectedId ? ' selected' : ''}`}
          key={req.requestId}
          onClick={() => onSelect(req.requestId === selectedId ? null : req.requestId)}
        >
          <span className="req-row-main">
            <span className="req-row-title">
              {req.location_description || 'Unknown location'}
            </span>
            <span className="req-row-sub" title={req.requestId}>
              {formatRequestId(req.requestId)}
              {req.people_count != null && ` · ${req.people_count} people`}
              {Array.isArray(req.assignedResources) && req.assignedResources.length > 0 &&
                ` · ${req.assignedResources.join(', ')}`}
            </span>
          </span>
          <span className={`status-chip st-${tone}`}>{tone}</span>
        </button>
      ))}
    </div>
  )
}

function RequestCard({ req, selected, onSelect, nodes, onReview, reviewBusy }) {
  const urgency = req.urgency ?? 'LOW'
  // Anything the extractor could not resolve is held until a human supplies
  // the missing fields, rather than being dispatched on a guess.
  const needsReview = req.status === 'UNPROCESSED' || req.human_review_required
  const className = [
    'req-card',
    selected ? `sel-${urgency}` : '',
    needsReview ? 'has-review' : '',
  ].filter(Boolean).join(' ')

  return (
    <>
    <button
      type="button"
      className={className}
      onClick={() => onSelect(selected ? null : req.requestId)}
    >
      <div className="rh">
        <span className={`badge badge-${urgency}`}>{urgency}</span>
        {req.language && <span className="lang-chip">{req.language}</span>}
        {req.status === 'UNPROCESSED' && <span className="warn-chip">UNPROCESSED · REVIEW</span>}
        {req.status === 'UNREACHABLE' && <span className="warn-chip">UNREACHABLE · ESCALATE</span>}
        {req.human_review_required && req.status !== 'UNPROCESSED' && (
          <span className="warn-chip">REVIEW REQUIRED</span>
        )}
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
        <span className="tag">
          {req.people_count == null ? 'Count unknown' : `${req.people_count} people`}
        </span>
        {(req.report_count ?? 1) > 1 && (
          <span className="tag">{req.report_count} reports linked</span>
        )}
        {(req.needs ?? []).map((need) => (
          <span className="tag" key={need}>{need}</span>
        ))}
        {req.vulnerable && (
          <span className="warn-chip">{req.vulnerable_details || 'vulnerable'}</span>
        )}
      </div>
    </button>
    {needsReview && (
      <ReviewForm req={req} nodes={nodes} onReview={onReview} busy={reviewBusy} />
    )}
    </>
  )
}

/** Correct what the extractor could not resolve, then queue the request. */
function ReviewForm({ req, nodes, onReview, busy }) {
  const [peopleCount, setPeopleCount] = useState(req.people_count ?? '')
  const [nodeId, setNodeId] = useState(req.node_id ?? '')
  const [urgency, setUrgency] = useState(req.urgency ?? 'MEDIUM')

  const submit = (event) => {
    event.preventDefault()
    onReview?.(req.requestId, {
      people_count: Number(peopleCount),
      node_id: nodeId,
      urgency,
    })
  }

  return (
    <form className="request-review" onSubmit={submit}>
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
      <button className="ghost-btn" type="submit" disabled={busy || !nodes[nodeId]}>
        {busy ? 'Saving…' : 'Confirm and queue'}
      </button>
    </form>
  )
}
