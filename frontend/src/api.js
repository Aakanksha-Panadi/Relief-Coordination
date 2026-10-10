/**
 * Thin client over the RescueIQ FastAPI backend.
 *
 * The backend reports failures deliberately rather than returning a
 * plausible-looking 200 (a dropped help request must not look successful),
 * so this layer surfaces the detail it sends instead of flattening
 * everything to "request failed".
 */

export const API_BASE =
  import.meta.env.VITE_API_BASE?.replace(/\/$/, '') || 'http://localhost:8080'

export class ApiError extends Error {
  constructor(message, { status, detail } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** Pull a human-readable message out of FastAPI's varied `detail` shapes. */
function readDetail(body, status) {
  const detail = body?.detail
  if (!detail) return `Request failed (HTTP ${status})`
  if (typeof detail === 'string') return detail
  // Intake failures send {error, message, raw_output}.
  if (detail.message) return `${detail.error ?? 'Error'}: ${detail.message}`
  // Pydantic validation errors arrive as an array.
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join('; ')
  return JSON.stringify(detail)
}

async function request(path, { method = 'GET', body } = {}) {
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (cause) {
    // fetch only rejects on network-level failure, which almost always means
    // the backend is not running. Say that rather than "Failed to fetch".
    throw new ApiError(
      `Cannot reach the backend at ${API_BASE}. Is it running?`,
      { status: 0, detail: String(cause) },
    )
  }

  const text = await response.text()
  const payload = text ? safeParse(text) : null

  if (!response.ok) {
    throw new ApiError(readDetail(payload, response.status), {
      status: response.status,
      detail: payload?.detail,
    })
  }
  return payload
}

function safeParse(text) {
  try {
    return JSON.parse(text)
  } catch {
    return { raw: text }
  }
}

export const api = {
  health: () => request('/health'),
  graph: () => request('/graph'),

  processIntake: (message) =>
    request('/intake/process', { method: 'POST', body: { message } }),
  processBatch: (messages) =>
    request('/intake/batch', { method: 'POST', body: { messages } }),

  requests: () => request('/requests'),
  pendingRequests: () => request('/requests/pending'),

  resources: () => request('/resources'),
  resourceSummary: () => request('/resources/summary'),
  releaseAllResources: () =>
    request('/resources/release-all', { method: 'POST' }),

  createPlan: () => request('/dispatch/plan', { method: 'POST' }),
  currentPlan: () => request('/dispatch/current'),
  approvePlan: (approvedBy = 'coordinator') =>
    request('/dispatch/approve', {
      method: 'POST',
      body: { approved_by: approvedBy },
    }),
  reviewPlan: (review) => request('/dispatch/review', { method: 'POST', body: review }),
  completeRequest: (requestId) =>
    request('/dispatch/complete', {
      method: 'POST',
      body: { request_id: requestId },
    }),

  closeRoad: (edgeId) =>
    request('/replan/road-closure', { method: 'POST', body: { edge_id: edgeId } }),
  approveReplan: (approvedBy = 'coordinator') =>
    request('/replan/approve', {
      method: 'POST',
      body: { approved_by: approvedBy },
    }),
  reopenRoad: (edgeId) =>
    request('/replan/reopen', { method: 'POST', body: { edge_id: edgeId } }),
  replanStatus: () => request('/replan/current'),

  metrics: () => request('/metrics'),
  events: () => request('/events'),
  reset: () => request('/reset', { method: 'POST' }),
}
