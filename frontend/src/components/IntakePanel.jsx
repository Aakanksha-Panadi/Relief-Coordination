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

export default function IntakePanel({ onSubmit, onSubmitBatch, busy, extraction }) {
  const [text, setText] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    const message = text.trim()
    if (!message || busy) return
    const result = await onSubmit(message)
    // Keep the text on failure so nothing the operator typed is lost.
    if (result) setText('')
  }

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submit(e)
  }

  return (
    <div className="intake">
      <form onSubmit={submit}>
        <div className="intake-label">
          <span>New help request</span>
          <span className="intake-hint">Ctrl+Enter to send</span>
        </div>

        <textarea
          className="intake-box"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          placeholder="Paste a message in any language — Hindi, Tamil, English or mixed. Gemini extracts location, people count, urgency and vulnerability."
        />

        <div className="intake-actions">
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
      </form>

      <div className="samples">
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
      </div>

      {extraction && <ExtractionResult data={extraction} />}
    </div>
  )
}

function ExtractionResult({ data }) {
  const rows = [
    ['Language', data.language],
    ['Urgency', data.urgency],
    ['People', data.people_count],
    ['Location', data.location_description],
    ['Node', data.node_id ?? 'unresolved'],
    ['Needs', (data.needs ?? []).join(', ') || '—'],
  ]
  if (data.vulnerable) rows.push(['Vulnerable', data.vulnerable_details || 'yes'])
  if (data.translated_text && data.translated_text !== data.raw_text) {
    rows.push(['Translated', data.translated_text])
  }

  return (
    <div className="extract-card">
      <div className="extract-head">
        <span>Extracted — {data.requestId}</span>
        {typeof data.confidence === 'number' && (
          <span>{Math.round(data.confidence * 100)}% conf</span>
        )}
      </div>
      <div className="extract-grid">
        {rows.map(([key, value]) => (
          <Row key={key} label={key} value={value} />
        ))}
      </div>
      {data.human_review_required && <div className="why-box why-amber">
        Held for coordinator review: {(data.processing_flags ?? []).join(', ') || 'low confidence'}
      </div>}
      {data.raw_text && <div className="ac-route">Raw report: {data.raw_text}</div>}
    </div>
  )
}

function Row({ label, value }) {
  return (
    <>
      <span className="extract-key">{label}</span>
      <span className="extract-val">{String(value ?? '—')}</span>
    </>
  )
}
