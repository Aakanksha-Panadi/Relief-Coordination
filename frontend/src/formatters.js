export function formatRequestId(requestId) {
  if (!requestId) return 'Request'

  const value = String(requestId)
  const generatedId = value.match(/^REQ_([A-F0-9]{32})$/i)
  if (generatedId) return `REQ-${generatedId[1].slice(-8)}`
  if (value.length > 20) return `${value.slice(0, 8)}…${value.slice(-8)}`
  return value
}
