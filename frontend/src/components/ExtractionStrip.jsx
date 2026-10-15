import { useEffect, useState } from 'react'

/**
 * What Gemini pulled out of the last message.
 *
 * This is the only visible evidence that extraction happened at all, so it
 * stays in the main flow rather than becoming a toast. Laid out across the
 * width because it now sits under the map, not in a narrow column.
 */
export default function ExtractionStrip({ data }) {
  const [dismissed, setDismissed] = useState(false)

  // A new extraction is new information; un-dismiss for it.
  useEffect(() => { setDismissed(false) }, [data?.requestId])

  if (!data || dismissed) return null

  const held = data.status === 'UNPROCESSED' || data.human_review_required

  const fields = [
    ['Language', data.language],
    ['Urgency', data.urgency],
    ['People', data.people_count ?? 'unknown'],
    ['Location', data.location_description],
    ['Node', data.node_id ?? 'unresolved'],
    ['Needs', (data.needs ?? []).join(', ') || '—'],
  ]
  if (data.vulnerable) fields.push(['Vulnerable', data.vulnerable_details || 'yes'])
  if (data.translated_text && data.translated_text !== data.raw_text) {
    fields.push(['Translated', data.translated_text])
  }

  return (
    <div className={`extract-strip${held ? ' held' : ''}`}>
      <div className="extract-head">
        <span>Extracted — {data.requestId}</span>
        {typeof data.confidence === 'number' && (
          <span className="extract-conf">{Math.round(data.confidence * 100)}% confidence</span>
        )}
        {data.linked_request_id && (
          <span className="extract-conf">linked to {data.linked_request_id}</span>
        )}
        <button className="extract-x" onClick={() => setDismissed(true)} title="Dismiss">
          ×
        </button>
      </div>

      <div className="extract-fields">
        {fields.map(([key, value]) => (
          <span className="ef" key={key}>
            <b>{key}</b>
            <span>{String(value ?? '—')}</span>
          </span>
        ))}
      </div>

      {held && (
        <div className="extract-held">
          Held for coordinator review: {(data.processing_flags ?? []).join(', ') || 'low confidence'}
        </div>
      )}
    </div>
  )
}
