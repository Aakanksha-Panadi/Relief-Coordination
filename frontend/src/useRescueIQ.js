import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'

const POLL_SECONDS = Number(import.meta.env.VITE_POLL_SECONDS ?? 10)

/**
 * All server state for the console, in one place.
 *
 * Every mutation refreshes the slices it actually invalidates rather than
 * reloading everything, so approving a plan does not blank the map.
 */
export function useRescueIQ() {
  const [health, setHealth] = useState(null)
  const [graph, setGraph] = useState(null)
  const [requests, setRequests] = useState([])
  const [summary, setSummary] = useState({})
  const [plan, setPlan] = useState(null)
  const [replan, setReplan] = useState(null)
  const [metrics, setMetrics] = useState(null)

  const [busy, setBusy] = useState({})
  const [toasts, setToasts] = useState([])
  const [lastExtraction, setLastExtraction] = useState(null)
  const [offline, setOffline] = useState(false)

  const toastId = useRef(0)

  const notify = useCallback((message, tone = 'info') => {
    const id = ++toastId.current
    setToasts((t) => [...t, { id, message, tone }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000)
  }, [])

  const dismissToast = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  /** Run an action with a named busy flag and uniform error reporting. */
  const run = useCallback(
    async (key, fn, { successMessage } = {}) => {
      setBusy((b) => ({ ...b, [key]: true }))
      try {
        const result = await fn()
        if (successMessage) notify(successMessage, 'success')
        setOffline(false)
        return result
      } catch (error) {
        if (error instanceof ApiError && error.status === 0) setOffline(true)
        notify(error.message, 'error')
        return null
      } finally {
        setBusy((b) => ({ ...b, [key]: false }))
      }
    },
    [notify],
  )

  // ---------- loaders ----------

  const loadRequests = useCallback(async () => {
    try {
      const data = await api.requests()
      setRequests(data?.requests ?? [])
    } catch {
      /* polling failures stay silent; the banner already shows offline */
    }
  }, [])

  const loadSummary = useCallback(async () => {
    try {
      setSummary(await api.resourceSummary())
    } catch {
      /* same */
    }
  }, [])

  const loadGraph = useCallback(async () => {
    try {
      setGraph(await api.graph())
    } catch {
      /* same */
    }
  }, [])

  const loadMetrics = useCallback(async () => {
    try {
      setMetrics(await api.metrics())
    } catch {
      // 400 until a plan exists. Not an error worth shouting about.
      setMetrics(null)
    }
  }, [])

  const refreshAll = useCallback(async () => {
    await Promise.all([loadRequests(), loadSummary(), loadGraph()])
  }, [loadRequests, loadSummary, loadGraph])

  // Initial load.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const h = await api.health()
        if (!cancelled) {
          setHealth(h)
          setOffline(false)
        }
      } catch (error) {
        if (!cancelled) {
          setOffline(true)
          notify(error.message, 'error')
        }
      }
      if (!cancelled) await refreshAll()
      // A plan may already exist from an earlier session.
      try {
        const existing = await api.currentPlan()
        if (!cancelled) setPlan(existing)
      } catch {
        /* 404 simply means nothing is planned yet */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [refreshAll, notify])

  // Background refresh so the queue reflects other operators.
  useEffect(() => {
    if (!POLL_SECONDS) return undefined
    const timer = setInterval(() => {
      loadRequests()
      loadSummary()
    }, POLL_SECONDS * 1000)
    return () => clearInterval(timer)
  }, [loadRequests, loadSummary])

  // ---------- actions ----------

  const submitMessage = useCallback(
    async (message) => {
      const result = await run('intake', () => api.processIntake(message))
      if (result) {
        setLastExtraction(result)
        notify(
          `${result.requestId} extracted — ${result.language}, ${result.urgency}`,
          'success',
        )
        await Promise.all([loadRequests(), loadSummary()])
      }
      return result
    },
    [run, notify, loadRequests, loadSummary],
  )

  const submitBatch = useCallback(
    async (messages) => {
      const result = await run('intake', () => api.processBatch(messages))
      if (result) {
        notify(`${result.processed} messages extracted`, 'success')
        await Promise.all([loadRequests(), loadSummary()])
      }
      return result
    },
    [run, notify, loadRequests, loadSummary],
  )

  const generatePlan = useCallback(async () => {
    const result = await run('plan', () => api.createPlan())
    if (result) {
      setPlan(result)
      setReplan(null)
      notify(
        `Plan ready — ${result.total_assigned} assigned, ${result.total_unassigned} unassigned`,
        'success',
      )
      await loadMetrics()
    }
    return result
  }, [run, notify, loadMetrics])

  const approvePlan = useCallback(async () => {
    const result = await run('approve', () => api.approvePlan())
    if (result) {
      notify(`${result.approved_count} assignments approved`, 'success')
      setPlan((p) => (p ? { ...p, status: 'APPROVED' } : p))
      await Promise.all([loadRequests(), loadSummary()])
    }
    return result
  }, [run, notify, loadRequests, loadSummary])

  const closeRoad = useCallback(
    async (edgeId) => {
      const result = await run('replan', () => api.closeRoad(edgeId))
      if (result) {
        setReplan(result)
        notify(result.summary, 'warn')
        await loadGraph()
      }
      return result
    },
    [run, notify, loadGraph],
  )

  const approveReplan = useCallback(async () => {
    const result = await run('approveReplan', () => api.approveReplan())
    if (result) {
      notify(`${result.updated_routes} routes updated`, 'success')
      // Promote replanned routes into the plan the map and list render.
      setPlan((p) =>
        p && replan ? { ...p, assignments: replan.assignments } : p,
      )
      setReplan(null)
      await loadMetrics()
    }
    return result
  }, [run, notify, replan, loadMetrics])

  const reopenRoad = useCallback(
    async (edgeId) => {
      const result = await run('reopen', () => api.reopenRoad(edgeId), {
        successMessage: `${edgeId} reopened`,
      })
      if (result) await loadGraph()
      return result
    },
    [run, loadGraph],
  )

  const completeRequest = useCallback(
    async (requestId) => {
      const result = await run('complete', () => api.completeRequest(requestId), {
        successMessage: `${requestId} marked served`,
      })
      if (result) await Promise.all([loadRequests(), loadSummary()])
      return result
    },
    [run, loadRequests, loadSummary],
  )

  const releaseAll = useCallback(async () => {
    const result = await run('release', () => api.releaseAllResources())
    if (result) {
      notify(`${result.resources_freed} resources released`, 'success')
      await loadSummary()
    }
    return result
  }, [run, notify, loadSummary])

  const resetAll = useCallback(async () => {
    const result = await run('reset', () => api.reset())
    if (result) {
      setPlan(null)
      setReplan(null)
      setMetrics(null)
      setLastExtraction(null)
      notify(
        `Reset — ${result.requests_removed} requests removed, ${result.resources_freed} resources freed`,
        'success',
      )
      await refreshAll()
    }
    return result
  }, [run, notify, refreshAll])

  return {
    health,
    graph,
    requests,
    summary,
    plan,
    replan,
    metrics,
    busy,
    toasts,
    offline,
    lastExtraction,
    dismissToast,
    submitMessage,
    submitBatch,
    generatePlan,
    approvePlan,
    closeRoad,
    approveReplan,
    reopenRoad,
    completeRequest,
    releaseAll,
    resetAll,
    loadMetrics,
  }
}
