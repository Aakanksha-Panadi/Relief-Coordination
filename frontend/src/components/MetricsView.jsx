/**
 * RescueIQ vs the first-come-first-served baseline.
 *
 * Every figure here is computed by the backend from the current plan over
 * the same road graph and fleet. Nothing is hardcoded, so the numbers move
 * with the request mix that is actually loaded.
 */
export default function MetricsView({ metrics, summary, requests, onRefresh, busy }) {
  if (!metrics) {
    return (
      <div className="res-view">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="empty-title">No metrics yet</div>
          <div className="empty-sub">
            Metrics compare the current dispatch plan against a
            first-come-first-served baseline. Generate a plan on the Command
            centre tab first.
          </div>
          <button className="ghost-btn" style={{ marginTop: 10 }} onClick={onRefresh}>
            Refresh
          </button>
        </div>
      </div>
    )
  }

  const { baseline, rescueiq, improvement, total_requests: total } = metrics

  return (
    <div className="res-view">
      <div className="res-head">
        <span className="res-title">Results</span>
        <span className="res-chip">{total} requests in this run</span>
        <span className="res-chip">
          {rescueiq.assigned} assigned · {rescueiq.unassigned} unassigned
        </span>
        <button
          className="ghost-btn"
          style={{ marginLeft: 'auto' }}
          onClick={onRefresh}
          disabled={busy?.metrics}
        >
          Refresh
        </button>
      </div>

      <div className="metrics-grid">
        <MetricCard
          label="AVG RESPONSE TIME"
          value={rescueiq.avg_response_time_min}
          unit="min"
          baseline={`baseline ${baseline.avg_response_time_min} min`}
          delta={improvement.response_time_pct_faster}
          deltaLabel="% faster"
          good={improvement.response_time_pct_faster > 0}
        />
        <MetricCard
          label="COVERAGE"
          value={rescueiq.coverage_pct}
          unit="%"
          baseline={`baseline ${baseline.coverage_pct}%`}
          delta={improvement.coverage_pct_points}
          deltaLabel="pts"
          good={improvement.coverage_pct_points >= 0}
        />
        <MetricCard
          label="CRITICAL SERVED FIRST"
          value={rescueiq.critical_first_pct}
          unit="%"
          baseline={`baseline ${baseline.critical_first_pct}%`}
          delta={improvement.critical_first_pct_points}
          deltaLabel="pts"
          good={improvement.critical_first_pct_points >= 0}
        />
        <MetricCard
          label="CONSTRAINT VIOLATIONS"
          value={rescueiq.constraint_violations}
          unit=""
          baseline={`baseline ${baseline.constraint_violations}`}
          delta={improvement.violations_avoided}
          deltaLabel="avoided"
          good={improvement.violations_avoided >= 0}
          invert
        />
      </div>

      <div className="bottom-grid">
        <div className="card">
          <div className="card-title">
            RescueIQ vs baseline
            <span className="card-sub">same graph, same fleet</span>
          </div>
          <table className="rt">
            <thead>
              <tr>
                <th>METRIC</th>
                <th>BASELINE (FCFS)</th>
                <th>RESCUEIQ</th>
              </tr>
            </thead>
            <tbody>
              <Row label="Avg response time"
                   a={`${baseline.avg_response_time_min} min`}
                   b={`${rescueiq.avg_response_time_min} min`}
                   better={rescueiq.avg_response_time_min <= baseline.avg_response_time_min} />
              <Row label="Max response time"
                   a={`${baseline.max_response_time_min} min`}
                   b={`${rescueiq.max_response_time_min} min`}
                   better={rescueiq.max_response_time_min <= baseline.max_response_time_min} />
              <Row label="Coverage"
                   a={`${baseline.coverage_pct}%`}
                   b={`${rescueiq.coverage_pct}%`}
                   better={rescueiq.coverage_pct >= baseline.coverage_pct} />
              <Row label="Critical served first"
                   a={`${baseline.critical_first_pct}%`}
                   b={`${rescueiq.critical_first_pct}%`}
                   better={rescueiq.critical_first_pct >= baseline.critical_first_pct} />
              <Row label="Constraint violations"
                   a={baseline.constraint_violations}
                   b={rescueiq.constraint_violations}
                   better={rescueiq.constraint_violations <= baseline.constraint_violations} />
              <Row label="Unassigned"
                   a={baseline.unassigned}
                   b={rescueiq.unassigned}
                   better={rescueiq.unassigned <= baseline.unassigned} />
            </tbody>
          </table>
          <div className="mc-note">
            The headline number is <strong>critical served first</strong>:
            the share of life-critical cases dispatched before any routine
            one. Triage is the thing first-come-first-served structurally
            cannot do.
          </div>
        </div>

        <div className="card">
          <div className="card-title">
            Fleet status
            <span className="card-sub">live from the backend</span>
          </div>
          {Object.entries(summary ?? {}).length === 0 ? (
            <div className="empty-sub">No resource data.</div>
          ) : (
            Object.entries(summary).map(([type, s]) => {
              const pct = s.total ? Math.round((s.available / s.total) * 100) : 0
              return (
                <div className="prog-row" key={type}>
                  <div className="prog-lbl">
                    <span>
                      {type.replace('_', ' ')}
                      {s.dispatchable === false ? ' (destination)' : ''}
                    </span>
                    <span>
                      {s.available}/{s.total} free
                      {s.capacity ? ` · cap ${s.capacity}` : ''}
                    </span>
                  </div>
                  <div className="prog-bar">
                    <div
                      className={`prog-fill${pct < 50 ? ' blue' : ''}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              )
            })
          )}

          <div className="card-title" style={{ marginTop: 16 }}>
            Languages seen
            <span className="card-sub">this run</span>
          </div>
          <LanguageBreakdown requests={requests} />
        </div>
      </div>
    </div>
  )
}

function MetricCard({ label, value, unit, baseline, delta, deltaLabel, good, invert }) {
  return (
    <div className="metric-card">
      <div className="mc-label">{label}</div>
      <div className={`mc-val${invert && value > 0 ? ' neutral' : ''}`}>
        {value}
        {unit && <span> {unit}</span>}
      </div>
      <div className="mc-base">{baseline}</div>
      <div className="mc-note" style={{ marginTop: 0 }}>
        <strong style={{ color: good ? 'var(--green)' : 'var(--amber)' }}>
          {delta > 0 ? '+' : ''}{delta}
        </strong>{' '}
        {deltaLabel} vs baseline
      </div>
    </div>
  )
}

function Row({ label, a, b, better }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="td-none">{a}</td>
      <td className={better ? 'td-good' : 'td-pos'}>{b}</td>
    </tr>
  )
}

function LanguageBreakdown({ requests }) {
  const counts = {}
  for (const r of requests ?? []) {
    const lang = r.language || 'Unknown'
    counts[lang] = (counts[lang] ?? 0) + 1
  }
  const total = Object.values(counts).reduce((s, n) => s + n, 0)

  if (total === 0) return <div className="empty-sub">No requests processed yet.</div>

  return Object.entries(counts)
    .sort((x, y) => y[1] - x[1])
    .map(([lang, n]) => (
      <div className="prog-row" key={lang}>
        <div className="prog-lbl">
          <span>{lang}</span>
          <span>{n} of {total}</span>
        </div>
        <div className="prog-bar">
          <div className="prog-fill" style={{ width: `${(n / total) * 100}%` }} />
        </div>
      </div>
    ))
}
