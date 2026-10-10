export default function TopBar({
  health,
  summary,
  requests,
  tab,
  onTab,
  onReset,
  onReleaseAll,
  busy,
}) {
  const pending = requests.filter((r) => r.status === 'PENDING').length
  const critical = requests.filter(
    (r) => r.urgency === 'CRITICAL' && r.status !== 'SERVED',
  ).length

  const dispatchable = Object.entries(summary ?? {}).filter(
    ([, s]) => s.dispatchable !== false,
  )
  const available = dispatchable.reduce((n, [, s]) => n + (s.available ?? 0), 0)

  // Mock mode is honest state, not a failure — but it must be visible so a
  // demo is never mistaken for live Gemini output.
  const mode = !health
    ? { cls: 'down', text: 'connecting' }
    : health.mock_mode
      ? { cls: 'warn', text: 'MOCK MODE' }
      : { cls: '', text: 'LIVE GEMINI' }

  return (
    <div className="topbar">
      <div className="logo">
        <div className="logo-mark">R</div>
        <div>
          <div className="logo-name">RescueIQ</div>
          <div className="logo-sub">Relief coordination</div>
        </div>
      </div>

      <div className={`live-pill ${mode.cls}`}>
        <span className="live-dot" />
        {mode.text}
      </div>

      <div className="nav">
        <button
          className={`nav-btn${tab === 'command' ? ' active' : ''}`}
          onClick={() => onTab('command')}
        >
          Command centre
        </button>
        <button
          className={`nav-btn${tab === 'results' ? ' active' : ''}`}
          onClick={() => onTab('results')}
        >
          Results
        </button>
      </div>

      <div className="topbar-right">
        <div className="stat-pill">
          <span className="stat-val">{pending}</span>
          <span className="stat-label">PENDING</span>
        </div>
        <div className="stat-pill">
          <span className={`stat-val${critical > 0 ? ' danger' : ''}`}>{critical}</span>
          <span className="stat-label">CRITICAL</span>
        </div>
        <div className="stat-pill">
          <span className="stat-val">{available}</span>
          <span className="stat-label">FREE</span>
        </div>

        <button className="ghost-btn" onClick={onReleaseAll} disabled={busy.release}
                title="Force every resource back to available">
          Release all
        </button>
        <button className="ghost-btn" onClick={onReset} disabled={busy.reset}
                title="Clear requests, plan and road closures">
          {busy.reset ? 'Resetting…' : 'Reset'}
        </button>
      </div>
    </div>
  )
}
