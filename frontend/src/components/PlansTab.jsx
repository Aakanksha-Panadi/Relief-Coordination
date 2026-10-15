import { useState } from 'react'
import { formatRequestId } from '../formatters'

/**
 * Collapse a route to the roads it actually uses.
 *
 * The node chain ("Community Hall → Ward 5 Market Road → …") is four lines
 * of text for something a coordinator reads as "via Market Road". The chain
 * is still there, one click down.
 */
function viaSummary(route, graph) {
  const names = (route?.edges ?? [])
    .map((id) => graph?.edges?.[id]?.name)
    .filter(Boolean)
  // The same road often spans several edges; say it once.
  const deduped = names.filter((name, i) => name !== names[i - 1])
  if (deduped.length > 0) return `via ${deduped.join(', ')}`

  const path = route?.path_names ?? []
  return path.length > 1 ? `via ${path[path.length - 1]}` : ''
}

/* Assignment states the backend actually emits.
   PENDING_APPROVAL and PARTIAL are exactly what approve_plan() commits;
   REPLANNED/RETURNING/UNCHANGED are produced by the replan agent on
   assignments that were already dispatched. */
const PROPOSED = new Set(['PENDING_APPROVAL', 'PARTIAL'])
const REROUTED = new Set(['REPLANNED', 'RETURNING'])
/* The backend's own terminal set. SUPERSEDED means a newer assignment
   replaced this one, so it is history, not live work. */
const DISCARDED = new Set(['CANCELLED', 'SUPERSEDED', 'UNAVAILABLE'])

/**
 * Which bucket one assignment belongs in.
 *
 * Decided per assignment rather than from the plan's approval_status,
 * because a plan is not uniform: a resource failure builds a replacement
 * plan that is WAITING overall while still carrying the already-approved
 * missions that were in flight at the time. Reading the plan-level flag put
 * boats with people aboard under "needs approval".
 */
function bucketOf(a, replanPending) {
  if (DISCARDED.has(a.status)) return null
  if (a.status === 'BLOCKED') return 'blocked'
  if (a.status === 'COMPLETED' || a.mission_status === 'DELIVERED') return 'completed'

  // Never dispatched, so it is still a proposal.
  if (PROPOSED.has(a.status) && a.approval_status !== 'APPROVED') return 'needsApproval'

  // In flight, but the closure changed its route — that needs a decision.
  // An UNCHANGED assignment in the same replan does not.
  if (replanPending && REROUTED.has(a.status)) return 'needsApproval'

  if (a.mission_status) return 'inProgress'
  if (a.approval_status === 'APPROVED' || a.status === 'APPROVED') return 'inProgress'

  // Unrecognised shape: surface it rather than hide it.
  return 'needsApproval'
}

function groupAssignments(plan, replan) {
  const view = replan ?? plan
  const buckets = { needsApproval: [], inProgress: [], completed: [], blocked: [] }

  for (const a of view?.assignments ?? []) {
    const bucket = bucketOf(a, Boolean(replan))
    if (bucket) buckets[bucket].push(a)
  }
  return buckets
}

const MISSION_TEXT = {
  EN_ROUTE: 'En route to caller — awaiting pickup check-in',
  ABOARD: 'People aboard — en route to shelter',
  AT_SCENE: 'On scene with the caller',
  DELIVERED: 'Delivered — vehicle available again',
}

export default function PlansTab({
  plan,
  replan,
  graph,
  approvedPlans = [],
  busy,
  selectedRequestId,
  onApprove,
  onApproveAssignment,
  onReject,
  onApproveReplan,
  onReportProgress,
  onSelectRequest,
  onDismissRequest,
}) {
  const [openKey, setOpenKey] = useState(null)

  const { needsApproval, inProgress, completed, blocked } = groupAssignments(plan, replan)
  const unassigned = (plan?.unassigned ?? []).filter(
    (request) => request.status !== 'DISMISSED',
  )
  const replanPending = replan?.status === 'REPLAN_SCHEDULED'

  const toggle = (key) => setOpenKey((k) => (k === key ? null : key))

  const nothingYet =
    !plan && !replan && approvedPlans.length === 0 && unassigned.length === 0

  return (
    <>
      <div className="tab-body">
        {replan && (
          <div className="closure-bar">
            <span>⚠</span>
            <span>
              {replan.summary || (replanPending
                ? `Network change recorded. Replanning once after ${replan.seconds_remaining ?? replan.debounce_seconds ?? 60}s of stability.`
                : 'Routes recalculated after network changes.')}
            </span>
          </div>
        )}

        {(replan?.alerts ?? []).map((alert, i) => (
          <div className="rail-note why-amber" key={`ra-${i}-${alert}`}>{alert}</div>
        ))}
        {replan?.unreachable_areas?.length > 0 && (
          <div className="rail-note why-red">
            Unreachable areas:{' '}
            {replan.unreachable_areas
              .map((area) => area.map((item) => item.name).join(', '))
              .join(' · ')}
          </div>
        )}

        {nothingYet && (
          <div className="empty">
            <div className="empty-title">No plan yet</div>
            <div className="empty-sub">
              Add requests, then plan them. Each assignment is scored in Python
              and explained by Gemini.
            </div>
          </div>
        )}

        {needsApproval.length > 0 && (
          <Group
            label="Needs approval"
            count={needsApproval.length}
            action={
              replan ? (
                <button
                  className="btn-approve sm"
                  onClick={onApproveReplan}
                  disabled={busy.approveReplan || replanPending}
                >
                  {replanPending
                    ? 'Waiting to replan…'
                    : busy.approveReplan ? 'Approving…' : 'Approve replan'}
                </button>
              ) : (
                <>
                  <button
                    className="btn-approve sm"
                    onClick={() => onApprove()}
                    disabled={busy.approve || plan?.approval_status !== 'WAITING'}
                  >
                    {busy.approve ? 'Approving…' : `Approve all ${needsApproval.length}`}
                  </button>
                  <button
                    className="ghost-btn sm"
                    onClick={() => onReject?.('Coordinator rejected the proposed assignments.')}
                    disabled={busy.reviewPlan}
                  >
                    Reject
                  </button>
                </>
              )
            }
          >
            {needsApproval.map((a, index) => {
              const requestId = a.request?.requestId
              const firstForRequest = needsApproval.findIndex(
                (item) => item.request?.requestId === requestId,
              ) === index
              return (
                <PlanRow
                  key={keyOf(a)} a={a} graph={graph} tone="urgent"
                  open={openKey === keyOf(a)}
                  onToggle={() => toggle(keyOf(a))}
                  onSelect={onSelectRequest}
                  selected={requestId === selectedRequestId}
                  approvalAction={!replan && firstForRequest && (
                    <button
                      type="button"
                      className="btn-approve sm"
                      onClick={(event) => {
                        event.stopPropagation()
                        onApproveAssignment?.(requestId)
                      }}
                      disabled={busy.approve || plan?.approval_status !== 'WAITING'}
                      aria-label={`Approve ${a.request?.location_description || 'request'}`}
                    >
                      Approve
                    </button>
                  )}
                />
              )
            })}
          </Group>
        )}

        {inProgress.length > 0 && (
          <Group label="In progress" count={inProgress.length}>
            {inProgress.map((a, index) => {
              const requestId = a.request?.requestId
              const firstForRequest = inProgress.findIndex(
                (item) => item.request?.requestId === requestId,
              ) === index
              return (
                <PlanRow
                  key={keyOf(a)} a={a} graph={graph} tone="calm"
                  open={openKey === keyOf(a)}
                  onToggle={() => toggle(keyOf(a))}
                  onSelect={onSelectRequest}
                  selected={requestId === selectedRequestId}
                  rowAction={firstForRequest && (
                    <ProgressAction a={a} busy={busy} onReportProgress={onReportProgress} />
                  )}
                />
              )
            })}
          </Group>
        )}

        {(blocked.length > 0 || unassigned.length > 0) && (
          <Group label="Unassigned" count={blocked.length + unassigned.length}>
            {blocked.map((a) => (
              <PlanRow
                key={keyOf(a)} a={a} graph={graph} tone="stuck"
                open={openKey === keyOf(a)}
                onToggle={() => toggle(keyOf(a))}
                onSelect={onSelectRequest}
                selected={a.request?.requestId === selectedRequestId}
              />
            ))}
            {unassigned.map((req) => (
              <div className="plan stuck" key={req.requestId}>
                <div className="plan-row static">
                  <div className="plan-main">
                    <div className="plan-title">
                      {req.location_description || formatRequestId(req.requestId)}
                    </div>
                    <div className="plan-sub" title={req.requestId}>
                      {req.people_count ?? '?'} people · {formatRequestId(req.requestId)}
                    </div>
                  </div>
                  <span className={`badge badge-${req.urgency}`}>{req.urgency}</span>
                  <button
                    type="button"
                    className="ghost-btn sm"
                    disabled={busy[`dismiss:${req.requestId}`]}
                    onClick={() => onDismissRequest?.(req.requestId)}
                  >
                    {busy[`dismiss:${req.requestId}`] ? 'Dismissing…' : 'Dismiss'}
                  </button>
                </div>
                <div className="plan-detail">
                  <div className="why-box why-red">
                    {req.blockedReason ?? 'No resource could be assigned.'}
                  </div>
                </div>
              </div>
            ))}
          </Group>
        )}

        {(completed.length > 0 || approvedPlans.length > 0) && (
          <Group label="Completed" count={completed.length + approvedPlans.length}>
            {completed.map((a) => (
              <PlanRow
                key={keyOf(a)} a={a} graph={graph} tone="done"
                open={openKey === keyOf(a)}
                onToggle={() => toggle(keyOf(a))}
                onSelect={onSelectRequest}
                selected={a.request?.requestId === selectedRequestId}
              />
            ))}
            {approvedPlans.map((p) => (
              <HistoryRow
                key={p.planId} p={p}
                open={openKey === p.planId}
                onToggle={() => toggle(p.planId)}
              />
            ))}
          </Group>
        )}

        {plan?.tradeoff && (
          <div className="rail-note why-amber">
            <strong>Tradeoff:</strong> {plan.tradeoff}
          </div>
        )}
        {(plan?.alerts ?? []).map((alert, i) => (
          <div className="rail-note why-amber" key={`pa-${i}-${alert}`}>{alert}</div>
        ))}
      </div>

      <div className="rail-foot">
        <span className="audit-line">
          {plan
            ? `${plan.total_assigned} in this plan · ${plan.total_approved_assignments ?? 0} approved · ${plan.total_unassigned} unassigned`
            : 'Coordinator approval required before dispatch'}
        </span>
      </div>
    </>
  )
}

/**
 * Pickup and drop-off check-ins.
 *
 * Nothing observes an arrival, a reporter logs it: a vehicle stays EN_ROUTE
 * until someone records the pickup, and only once people are aboard can the
 * delivery be reported. The busy flag is per request so one check-in does not
 * disable every other row.
 */
function ProgressAction({ a, busy, onReportProgress }) {
  const requestId = a.request?.requestId
  const canCheckIn = a.approval_status === 'APPROVED'
    && !['COMPLETED', 'CANCELLED', 'SUPERSEDED', 'UNAVAILABLE', 'BLOCKED', 'RETURNING'].includes(a.status)
  const needsPickup = a.mission_status === 'EN_ROUTE'
  const needsDelivery = ['ABOARD', 'AT_SCENE'].includes(a.mission_status)

  if (!requestId || !canCheckIn || !(needsPickup || needsDelivery)) return null

  const working = busy[`progress:${requestId}`]

  return (
    <button
      type="button"
      className={`plan-inline-action${needsDelivery ? ' deliver' : ''}`}
      disabled={working}
      onClick={(e) => {
        e.stopPropagation()
        onReportProgress?.(requestId, needsPickup ? 'picked_up' : 'delivered')
      }}
    >
      {working ? 'Recording…' : needsPickup ? 'Mark picked up' : 'Mark reached'}
    </button>
  )
}

function keyOf(a) {
  return a.assignmentId ??
    ([a.request?.requestId, a.resource?.resourceId, a.wave].filter(Boolean).join('-') ||
      JSON.stringify(a).slice(0, 40))
}

function Group({ label, count, action, children }) {
  return (
    <div className="group">
      <div className="group-head">
        <span>{label}</span>
        <span className="group-count">{count}</span>
        <span className="rule" />
        {action}
      </div>
      {children}
    </div>
  )
}

function PlanRow({
  a, graph, tone, open, onToggle, onSelect, selected, action, approvalAction, rowAction,
}) {
  const route = a.new_route ?? a.route ?? {}
  const old = a.old_route
  const replanned = a.status === 'REPLANNED'
  const returning = a.status === 'RETURNING'
  const blocked = a.status === 'BLOCKED'
  const delay = a.delay_minutes

  const cls = ['plan', tone, open ? 'open' : '', selected ? 'selected' : ''].filter(Boolean).join(' ')
  const via = viaSummary(route, graph)

  const subParts = [
    a.request?.people_count != null ? `${a.request.people_count} people` : null,
    a.mission_status ? MISSION_TEXT[a.mission_status]?.toLowerCase() : via,
  ].filter(Boolean)

  return (
    <div className={cls}>
      <div
        className="plan-row"
        onClick={() => { onToggle(); onSelect?.(a.request?.requestId) }}
      >
        <div className="plan-main">
          <div className="plan-title">
            {a.resource?.name ?? a.resource?.resourceId} → {a.request?.location_description ?? 'unknown'}
          </div>
          <div className="plan-sub">{subParts.join(' · ')}</div>
        </div>

        {replanned && old && <span className="plan-old">{old.minutes} min</span>}
        {!blocked && route.minutes != null && (
          <span className={`plan-eta${replanned ? ' late' : ''}${tone === 'done' ? ' ghost' : ''}`}>
            {route.minutes} min
          </span>
        )}
        {approvalAction}
        {rowAction}
        <svg className="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </div>

      {open && (
        <div className="plan-detail">
          {a.mission_status && (
            <div className={`mission${a.mission_status === 'ABOARD' ? ' aboard' : ''}`}>
              {MISSION_TEXT[a.mission_status]}
            </div>
          )}

          {route.path_names?.length > 0 && (
            <div className="route-chain">{route.path_names.join(' → ')}</div>
          )}

          {a.shelter && (
            <div className="detail-line"><b>Shelter</b><span>{a.shelter.name}</span></div>
          )}

          {/* The headline ETA is time to reach the caller. With a shelter leg
              the people are not safe until that is driven too, so show both. */}
          {typeof a.total_eta_minutes === 'number' && a.total_eta_minutes !== route.minutes ? (
            <div className="detail-line">
              <b>Timing</b>
              <span>
                {route.minutes} min to reach them · {a.total_eta_minutes} min to safety
                {typeof a.shelter_eta_minutes === 'number' && ` (+${a.shelter_eta_minutes} min to shelter)`}
              </span>
            </div>
          ) : via && (
            <div className="detail-line"><b>Route</b><span>{via}</span></div>
          )}

          {replanned && typeof delay === 'number' && (
            <div className="detail-line">
              <b>Change</b><span>Rerouted · {delay >= 0 ? `+${delay}` : delay} min</span>
            </div>
          )}
          {returning && (
            <div className="detail-line">
              <b>Change</b>
              <span>Returning to safety · ETA {a.return_eta_minutes ?? route.minutes} min</span>
            </div>
          )}
          {a.wave && (
            <div className="detail-line">
              <b>Wave</b>
              <span>{a.wave} of {a.wave_count} · {a.wave_people} people</span>
            </div>
          )}

          {a.explanation && (
            <div className={`why-box ${blocked ? 'why-red' : replanned ? 'why-amber' : 'why-green'}`}>
              {a.explanation}
            </div>
          )}

          {action}
        </div>
      )}
    </div>
  )
}

function HistoryRow({ p, open, onToggle }) {
  return (
    <div className={`plan done${open ? ' open' : ''}`}>
      <div className="plan-row" onClick={onToggle}>
        <div className="plan-main">
          <div className="plan-title">{p.planId}</div>
          <div className="plan-sub">
            {p.assignmentCount} assignment{p.assignmentCount === 1 ? '' : 's'} ·
            {' '}approved by {p.approvedBy || 'coordinator'}
          </div>
        </div>
        <svg className="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </div>
      {open && (
        <div className="plan-detail">
          {p.approvedAt && (
            <div className="detail-line">
              <b>Approved</b><span>{new Date(p.approvedAt).toLocaleString()}</span>
            </div>
          )}
          {(p.assignments ?? []).map((as, i) => (
            <div className="history-line" key={as.assignmentId ?? `${p.planId}-${i}`} title={as.requestId}>
              {as.requestId ? formatRequestId(as.requestId) : 'Request'}
              {' → '}{(as.resourceIds ?? []).join(', ') || 'Resource'}
              {as.wave && ` · wave ${as.wave}/${as.wave_count ?? '?'}`}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
