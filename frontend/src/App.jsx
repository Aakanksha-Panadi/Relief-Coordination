import { useState } from 'react'
import { API_BASE } from './api'
import { useRescueIQ } from './useRescueIQ'
import TopBar from './components/TopBar'
import IntakePanel from './components/IntakePanel'
import RequestQueue from './components/RequestQueue'
import DistrictMap from './components/DistrictMap'
import DispatchPanel from './components/DispatchPanel'
import ReplanBar from './components/ReplanBar'
import MetricsView from './components/MetricsView'
import Toasts from './components/Toasts'

export default function App() {
  const [tab, setTab] = useState('command')
  const [selectedId, setSelectedId] = useState(null)
  const rq = useRescueIQ()

  return (
    <div className="app">
      <TopBar
        health={rq.health}
        summary={rq.summary}
        requests={rq.requests}
        tab={tab}
        onTab={setTab}
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
            <div className="left-panel">
              <IntakePanel
                onSubmit={rq.submitMessage}
                onSubmitBatch={rq.submitBatch}
                busy={rq.busy.intake}
                extraction={rq.lastExtraction}
              />
              <RequestQueue
                requests={rq.requests}
                selectedId={selectedId}
                onSelect={setSelectedId}
              />
            </div>

            <DistrictMap
              graph={rq.graph}
              plan={rq.plan}
              replan={rq.replan}
              requests={rq.requests}
              selectedRequestId={selectedId}
              onCloseEdge={rq.closeRoad}
            />

            <div className="right-panel">
              <DispatchPanel
                plan={rq.plan}
                replan={rq.replan}
                busy={rq.busy}
                onGenerate={rq.generatePlan}
                onApprove={rq.approvePlan}
                onApproveReplan={rq.approveReplan}
                onSelectRequest={setSelectedId}
              />
              <ReplanBar
                graph={rq.graph}
                plan={rq.plan}
                busy={rq.busy}
                onClose={rq.closeRoad}
                onReopen={rq.reopenRoad}
              />
            </div>
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

      <Footer health={rq.health} graph={rq.graph} />
      <Toasts toasts={rq.toasts} onDismiss={rq.dismissToast} />
    </div>
  )
}

function Footer({ health, graph }) {
  const closed = graph?.closed_edges?.length ?? 0

  return (
    <div className="footer">
      <span className="fi">
        <span className={`fi-dot ${health ? 'green' : 'red'}`} />
        API {API_BASE}
      </span>
      <span className="fi">
        <span className={`fi-dot ${health?.firestore ? 'green' : 'amber'}`} />
        Firestore {health?.firestore ? 'connected' : 'in-memory'}
      </span>
      <span className="fi">
        <span className={`fi-dot ${health?.resource_source === 'firestore' ? 'green' : 'amber'}`} />
        Fleet: {health?.resource_source ?? '—'} ({health?.resource_count ?? 0})
      </span>
      <span className="fi">
        <span className={`fi-dot ${health?.mock_mode ? 'amber' : 'blue'}`} />
        {health?.mock_mode ? 'Mock extraction' : 'Gemini live'}
      </span>
      {closed > 0 && (
        <span className="fi">
          <span className="fi-dot red" />
          {closed} road{closed > 1 ? 's' : ''} closed
        </span>
      )}
      <span className="fi" style={{ marginLeft: 'auto' }}>
        Gemini understands · Python calculates · Humans approve
      </span>
    </div>
  )
}
