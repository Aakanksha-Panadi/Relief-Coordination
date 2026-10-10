export default function Toasts({ toasts, onDismiss }) {
  if (toasts.length === 0) return null

  return (
    <div className="toasts">
      {toasts.map((t) => (
        <div className={`toast ${t.tone}`} key={t.id}>
          <span>{t.message}</span>
          <button
            className="toast-close"
            onClick={() => onDismiss(t.id)}
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  )
}
