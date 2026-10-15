import { useEffect, useState } from 'react'
import PlansTab from './PlansTab'
import RequestsTab from './RequestsTab'
import ResourcesTab from './ResourcesTab'

/**
 * One column, three views.
 *
 * Dispatch, the queue and the fleet used to be stacked panels competing for
 * the same strip of screen. Only one of them answers the question you are
 * asking at any moment, so only one is on screen.
 */
export default function SideRail({
  rq,
  selectedId,
  onSelect,
  onApprovePlan,
  onDismissRequest,
  requestFocusVersion,
}) {
  const [tab, setTab] = useState('plans')

  const planCount =
    (rq.replan?.assignments ?? rq.plan?.assignments ?? []).length +
    (rq.plan?.unassigned?.length ?? 0) +
    rq.approvedPlans.length

  // A pending replan needs a decision, so surface it rather than leaving the
  // operator on whichever tab they happened to be reading.
  useEffect(() => {
    if (rq.replan) setTab('plans')
  }, [rq.replan])

  useEffect(() => {
    if (requestFocusVersion > 0) setTab('reqs')
  }, [requestFocusVersion])

  const tabs = [
    { id: 'plans', label: 'Plans', count: planCount },
    { id: 'reqs', label: 'Requests', count: rq.requests.length },
    { id: 'res', label: 'Resources', count: rq.resources.length },
  ]

  return (
    <div className="right-panel">
      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`tab${tab === t.id ? ' active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            <span className="tab-count">{t.count}</span>
          </button>
        ))}
      </div>

      {tab === 'plans' && (
        <PlansTab
          plan={rq.plan}
          replan={rq.replan}
          graph={rq.graph}
          approvedPlans={rq.approvedPlans}
          busy={rq.busy}
          selectedRequestId={selectedId}
          onApprove={onApprovePlan ?? rq.approvePlan}
          onApproveAssignment={onApprovePlan ?? rq.approvePlan}
          onReject={rq.reviewPlan}
          onApproveReplan={rq.approveReplan}
          onReportProgress={rq.reportProgress}
          onSelectRequest={onSelect}
          onDismissRequest={onDismissRequest ?? rq.dismissRequest}
        />
      )}

      {tab === 'reqs' && (
        <RequestsTab
          requests={rq.requests}
          graph={rq.graph}
          selectedId={selectedId}
          onSelect={onSelect}
          onReview={rq.reviewRequest}
          reviewBusy={rq.busy.reviewRequest}
          plan={rq.plan}
          planBusy={rq.busy.plan}
          onGenerate={rq.generatePlan}
        />
      )}

      {tab === 'res' && (
        <ResourcesTab resources={rq.resources} summary={rq.summary} graph={rq.graph} />
      )}
    </div>
  )
}
