export default function DispatchPanel({
  plan,
  replan,
  busy,
  onGenerate,
  onApprove,
  onApproveReplan,
  onSelectRequest,
}) {
  // After a closure the replan result is the live view of the plan.
  const view = replan ?? plan
  const assignments = view?.assignments ?? []
  const unassigned = plan?.unassigned ?? []
  const approved = plan?.status === 'APPROVED'

  return (
    <>
      <div className="rp-head">
        <div className="rp-title-row">
          <span className="rp-title">Dispatch plan</span>
          {plan?.planId && <span className="rp-v">{plan.planId}</span>}
          {replan && <span className="rp-s">replanned</span>}
          {approved && !replan && <span className="rp-ok">approved</span>}
        </div>
      </div>

      {replan && (
        <div className="closure-bar">
          <span>⚠</span>
          <span>{replan.summary}</span>
        </div>
      )}

      <div className="scroll-area">
        {!plan ? (
          <div className="empty">
            <div className="empty-title">No plan yet</div>
            <div className="empty-sub">
              Add requests, then generate a plan. Each assignment is scored in
              Python and explained by Gemini.
            </div>
          </div>
        ) : (
          <>
            {assignments.map((a, i) => (
              <AssignmentCard key={a.resource?.resourceId ?? i} a={a} onSelect={onSelectRequest} />
            ))}

            {unassigned.length > 0 && (
              <>
                <div className="panel-title" style={{ margin: '14px 0 8px' }}>
                  Unassigned ({unassigned.length})
                </div>
                {unassigned.map((req) => (
                  <div className="ac blocked" key={req.requestId}>
                    <div className="ac-top">
                      <span className="ac-name">{req.requestId}</span>
                      <span className={`badge badge-${req.urgency}`}>{req.urgency}</span>
                    </div>
                    <div className="ac-sub">
                      {req.location_description} · {req.people_count} people
                    </div>
                    <div className="why-box why-red">
                      {req.blockedReason ?? 'No resource could be assigned.'}
                    </div>
                  </div>
                ))}
              </>
            )}

            {plan.tradeoff && (
              <div className="why-box why-amber" style={{ marginTop: 10 }}>
                <strong>Tradeoff:</strong> {plan.tradeoff}
              </div>
            )}
          </>
        )}
      </div>

      <div className="rp-foot">
        <div className="foot-btns">
          <button className="ghost-btn" onClick={onGenerate} disabled={busy.plan}
                  style={{ flex: 1 }}>
            {busy.plan ? <><span className="spinner" /> Planning…</> : 'Generate plan'}
          </button>

          {replan ? (
            <button className="btn-approve" onClick={onApproveReplan}
                    disabled={busy.approveReplan} style={{ flex: 1 }}>
              {busy.approveReplan ? 'Approving…' : 'Approve replan'}
            </button>
          ) : (
            <button className="btn-approve" onClick={onApprove}
                    disabled={busy.approve || !plan || approved} style={{ flex: 1 }}>
              {busy.approve ? 'Approving…' : approved ? 'Approved' : 'Approve plan'}
            </button>
          )}
        </div>
        <div className="audit-line">
          {plan
            ? `${plan.total_assigned} assigned · ${plan.total_unassigned} unassigned`
            : 'Coordinator approval required before dispatch'}
        </div>
      </div>
    </>
  )
}

function AssignmentCard({ a, onSelect }) {
  const route = a.new_route ?? a.route ?? {}
  const old = a.old_route
  const blocked = a.status === 'BLOCKED'
  const replanned = a.status === 'REPLANNED'
  const delay = a.delay_minutes

  const cls = ['ac', blocked ? 'blocked' : '', replanned ? 'replanned' : '']
    .filter(Boolean).join(' ')

  return (
    <div className={cls} onClick={() => onSelect?.(a.request?.requestId)}>
      <div className="ac-top">
        <span className="ac-name">
          {a.resource?.name ?? a.resource?.resourceId}
        </span>
        <span className="ac-eta-wrap">
          {replanned && old && <span className="ac-old">{old.minutes} min</span>}
          {!blocked && (
            <span className={`ac-eta${replanned ? ' late' : ''}`}>
              {route.minutes} min
            </span>
          )}
        </span>
      </div>

      <div className="ac-sub">
        → {a.request?.location_description} · {a.request?.people_count} people
        {a.request?.urgency === 'CRITICAL' ? ' · CRITICAL' : ''}
      </div>

      {route.path_names?.length > 0 && (
        <div className="ac-route">{route.path_names.join(' → ')}</div>
      )}

      {replanned && typeof delay === 'number' && (
        <div className="ac-change">
          Rerouted · {delay >= 0 ? `+${delay}` : delay} min
        </div>
      )}

      {a.explanation && (
        <div className={`why-box ${blocked ? 'why-red' : replanned ? 'why-amber' : 'why-green'}`}>
          {a.explanation}
        </div>
      )}
    </div>
  )
}
