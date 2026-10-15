import { useState } from 'react'

/** Demo messages covering the languages the intake agent handles. */
const SAMPLES = [
  {
    label: 'Hindi',
    text: 'मेरे घर में पानी भर गया है, 4 लोग हैं, बुजुर्ग माँ है चल नहीं सकती, Ward 7 मंदिर के पास',
  },
  {
    label: 'Tamil',
    text: 'உதவி! நாங்கள் 5 பேர் சிக்கிக்கொண்டோம், குழந்தைகள் உள்ளனர், Ward 3 கோயில் தெரு',
  },
  {
    label: 'English',
    text: 'Help!! 6 people stuck on roof near City Bank Market Road, 2 children',
  },
  {
    label: 'Mixed',
    text: 'HELP ward7 paani bahut zyada 3 log please boat jaldi',
  },
  {
    label: 'Medical',
    text: 'Diabetic patient needs insulin urgently near hospital junction, water rising',
  },
]

/**
 * Intake, docked below the map.
 *
 * A single line rather than a textarea: real messages arriving over WhatsApp
 * are a sentence or two, and the row shape lets the map keep the height.
 * Shift+Enter still opens it up for anything longer.
 */
export default function IntakeBar({ onSubmit, onSubmitBatch, busy }) {
  const [text, setText] = useState('')
  const [multiline, setMultiline] = useState(false)

  const submit = async (e) => {
    e?.preventDefault()
    const message = text.trim()
    if (!message || busy) return
    const result = await onSubmit(message)
    // Keep the text on failure so nothing the operator typed is lost.
    if (result) {
      setText('')
      setMultiline(false)
    }
  }

  const onKeyDown = (e) => {
    if (e.key !== 'Enter') return
    if (e.shiftKey) {
      setMultiline(true)
      return
    }
    e.preventDefault()
    submit()
  }

  const fieldProps = {
    value: text,
    onChange: (e) => setText(e.target.value),
    onKeyDown,
    disabled: busy,
    placeholder:
      'Paste a message as a resident would, in any language — Hindi, Tamil, English or mixed',
  }

  return (
    <form className="intake-bar" onSubmit={submit}>
      <div className="intake-row">
        {multiline ? (
          <textarea className="intake-field tall" rows={3} {...fieldProps} />
        ) : (
          <input className="intake-field" type="text" {...fieldProps} />
        )}
        <button className="btn-primary" type="submit" disabled={busy || !text.trim()}>
          {busy ? <><span className="spinner" /> Extracting…</> : 'Process request'}
        </button>
        <button
          type="button"
          className="ghost-btn"
          disabled={busy}
          title="Send all five sample messages at once"
          onClick={() => onSubmitBatch(SAMPLES.map((s) => s.text))}
        >
          Load all 5
        </button>
      </div>

      <div className="intake-meta">
        {SAMPLES.map((s) => (
          <button
            key={s.label}
            type="button"
            className="sample-chip"
            disabled={busy}
            onClick={() => setText(s.text)}
          >
            {s.label}
          </button>
        ))}
        <span className="intake-note">
          Simulates the WhatsApp channel · Enter to send, Shift+Enter for a longer message
        </span>
      </div>
    </form>
  )
}
