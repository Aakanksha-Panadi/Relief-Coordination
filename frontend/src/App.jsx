import { useState } from 'react'
import { API_BASE } from './api'
import { useRescueIQ } from './useRescueIQ'
import TopBar from './components/TopBar'
import IntakeBar from './components/IntakeBar'
import ExtractionStrip from './components/ExtractionStrip'
import DistrictMap from './components/DistrictMap'
import SideRail from './components/SideRail'
import MetricsView from './components/MetricsView'
import Toasts from './components/Toasts'

export default function App() {
  const [tab, setTab] = useState('command')
  const [selectedId, setSelectedId] = useState(null)
  const [requestFocusVersion, setRequestFocusVersion] = useState(0)
  const rq = useRescueIQ()

  const submitMessage = async (message) => {
    const result = await rq.submitMessage(message)
    if (result) {
      const requestId = result.linked_request_id ?? result.requestId
      if (requestId) {
        setSelectedId(requestId)
        setRequestFocusVersion((version) => version + 1)
      }
    }
    return result
  }

  // An approved request leaves the queue, so a stale selection would keep
  // pointing at a card that is no longer there.
  const approvePlan = async (requestId) => {
    const result = await rq.approvePlan(requestId)
    if (result && !result.replanned) {
      const approvedIds = new Set(
        (result.assignments ?? []).map((assignment) => assignment.requestId),
      )
      if (approvedIds.has(selectedId)) setSelectedId(null)
    }
    return result
  }

  const dismissRequest = async (requestId) => {
    const result = await rq.dismissRequest(requestId)
    if (result && selectedId === requestId) setSelectedId(null)
    return result
  }

  return (
    <div className="app">
      <TopBar
        health={rq.health}
        summary={rq.summary}
        requests={rq.requests}
        graph={rq.graph}
        plan={rq.plan}
        tab={tab}
        onTab={setTab}
        onCloseRoad={rq.closeRoad}
        onReopenRoad={rq.reopenRoad}
        onReset={rq.resetAll}
        onReleaseAll={rq.releaseAll}
        busy={rq.busy}
      />

      {rq.offline && (
        <div className="offline-bar">
          <span>●</span>
          <span>
            Backend unreachable at <code>{API_BASE}</code> — start it with{' '}
            <code>uvicorn main:app --port 8080</code> from the backend folder.
          </span>
        </div>
      )}

      <div className="body">
        {tab === 'command' ? (
          <div className="cmd-view">
            {/* Map takes the width; intake docks beneath it rather than
                occupying a column of its own. */}
            <div className="map-col">
              <DistrictMap
                graph={rq.graph}
                plan={rq.plan}
                replan={rq.replan}
                requests={rq.requests}
                selectedRequestId={selectedId}
                onCloseEdge={rq.closeRoad}
              />
              <ExtractionStrip data={rq.lastExtraction} />
              <IntakeBar
                onSubmit={submitMessage}
                onSubmitBatch={rq.submitBatch}
                busy={rq.busy.intake}
              />
            </div>

            <SideRail
              rq={rq}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onApprovePlan={approvePlan}
              onDismissRequest={dismissRequest}
              requestFocusVersion={requestFocusVersion}
            />
          </div>
        ) : (
          <MetricsView
            metrics={rq.metrics}
            summary={rq.summary}
            requests={rq.requests}
            onRefresh={rq.loadMetrics}
            busy={rq.busy}
          />
        )}
      </div>

      <Toasts toasts={rq.toasts} onDismiss={rq.dismissToast} />
    </div>
  )
}
