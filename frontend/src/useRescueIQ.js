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
  const [resources, setResources] = useState([])
  const [approvedPlans, setApprovedPlans] = useState([])
  const [summary, setSummary] = useState({})
  const [plan, setPlan] = useState(null)
  const [replan, setReplan] = useState(null)
  const [metrics, setMetrics] = useState(null)

  const [busy, setBusy] = useState({})
  const [toasts, setToasts] = useState([])
  const [lastExtraction, setLastExtraction] = useState(null)
  const [offline, setOffline] = useState(false)

  const toastId = useRef(0)
  const reminderKey = useRef('')
  const approvedRequestIds = useRef(new Set())

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
      setRequests((data?.requests ?? []).filter((request) =>
        !approvedRequestIds.current.has(request.requestId)))
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

  const loadResources = useCallback(async () => {
    try {
      const data = await api.resources()
      setResources(data?.resources ?? [])
    } catch {
      /* the fleet list is refreshed on the next poll */
    }
  }, [])

  const loadApprovedPlans = useCallback(async () => {
    try {
      const data = await api.approvedPlans()
      setApprovedPlans(data?.plans ?? [])
    } catch {
      /* approval history is refreshed on the next poll */
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
    await Promise.all([
      loadRequests(), loadSummary(), loadGraph(), loadResources(), loadApprovedPlans(),
    ])
  }, [loadRequests, loadSummary, loadGraph, loadResources, loadApprovedPlans])

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
      loadResources()
      loadApprovedPlans()
      api.currentPlan().then((nextPlan) => {
        setPlan(nextPlan)
        const key = `${nextPlan.planId}:${nextPlan.approval_reminder_count ?? 0}`
        if (nextPlan.approval_reminder_count && reminderKey.current !== key) {
          reminderKey.current = key
          notify(nextPlan.last_approval_reminder_at
            ? `Critical plan has waited ${Math.floor((nextPlan.approval_wait_seconds ?? 0) / 60)} minutes for approval. It has not been dispatched.`
            : 'Critical plan is awaiting coordinator approval. It has not been dispatched.', 'warn')
        }
      }).catch(() => {})
    }, POLL_SECONDS * 1000)
    return () => clearInterval(timer)
  }, [loadRequests, loadSummary, loadResources, loadApprovedPlans, notify])

  // Closure changes are coalesced server-side for up to a minute. Poll only
  // while that batch is pending, then expose the single resulting replan.
  useEffect(() => {
    if (replan?.status !== 'REPLAN_SCHEDULED') return undefined
    const timer = setInterval(async () => {
      try {
        const result = await api.replanStatus()
        if (result?.status === 'READY') {
          setReplan(result)
          notify(result.summary || 'Network replan ready for coordinator review', 'warn')
          await loadGraph()
        } else if (result?.status === 'REPLAN_SCHEDULED') {
          setReplan(result)
        }
      } catch {
        /* the existing offline indicator handles polling failures */
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [replan?.status, notify, loadGraph])

  // ---------- actions ----------

  const submitMessage = useCallback(
    async (message) => {
      const result = await run('intake', () => api.processIntake(message))
      if (result) {
        setLastExtraction(result)
        if (result.replacement_plan) setPlan(result.replacement_plan)
        const message = result.followup_action === 'linked_duplicate'
          ? `Report linked to ${result.linked_request_id} (${result.report_count} reports)`
          : result.followup_action === 'updated'
            ? `${result.linked_request_id} updated from follow-up`
            : result.followup_action === 'cancelled'
              ? `${result.linked_request_id} cancelled; resources replanned`
              : result.status === 'UNPROCESSED'
                ? 'Report held for coordinator review'
                : `${result.requestId} extracted — ${result.language}, ${result.urgency}`
        notify(message, result.status === 'UNPROCESSED' ? 'warn' : 'success')
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

  const reviewRequest = useCallback(async (requestId, review) => {
    const result = await run(
      'reviewRequest',
      () => api.reviewRequest(requestId, review),
    )
    if (result) {
      notify(`${requestId} reviewed and added to the dispatch queue`, 'success')
      await loadRequests()
    }
    return result
  }, [run, notify, loadRequests])

  const dismissRequest = useCallback(async (requestId) => {
    const result = await run(`dismiss:${requestId}`, () => api.dismissRequest(requestId))
    if (result) {
      notify('Request dismissed from the active queue; its history was kept', 'success')
      await loadRequests()
      setPlan((current) => current ? {
        ...current,
        unassigned: (current.unassigned ?? []).filter(
          (request) => request.requestId !== requestId,
        ),
        total_unassigned: Math.max(0, (current.total_unassigned ?? 0) - 1),
        metrics: current.metrics ? {
          ...current.metrics,
          unassigned: Math.max(0, (current.metrics.unassigned ?? 0) - 1),
        } : current.metrics,
      } : current)
    }
    return result
  }, [run, notify, loadRequests])

  const generatePlan = useCallback(async (requestId) => {
    const result = await run('plan', () => api.createPlan(requestId))
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

  const approvePlan = useCallback(async (requestId = null) => {
    const result = await run('approve', () => api.approvePlan('coordinator', requestId))
    if (result) {
      if (result.replanned) {
        setPlan(result.plan)
        notify(result.message, 'warn')
        return result
      }
      notify(`${result.approved_count} assignments approved`, 'success')
      const newlyApprovedIds = (result.assignments ?? [])
        .map((assignment) => assignment.requestId)
        .filter(Boolean)
      newlyApprovedIds.forEach((requestId) => approvedRequestIds.current.add(requestId))
      setRequests((current) => current.filter((request) =>
        !newlyApprovedIds.includes(request.requestId)))
      setPlan((p) => (p ? {
        ...p,
        status: result.plan_status ?? p.status,
        approval_status: result.approval_status ?? p.approval_status,
        total_approved_assignments: result.total_approved_assignments ?? p.total_approved_assignments,
        assignments: p.assignments?.map((assignment) => {
          const approved = (result.assignments ?? []).find(
            (item) => item.requestId === assignment.request?.requestId &&
              item.resourceIds?.includes(assignment.resource?.resourceId) &&
              item.wave === assignment.wave,
          )
          return approved
            ? {
                ...assignment,
                assignmentId: approved.assignmentId,
                approval_status: 'APPROVED',
                coverage_status: approved.coverage_status,
                status: approved.coverage_status === 'PARTIAL' ? 'PARTIAL' : 'APPROVED',
                mission_status: approved.mission_status,
              }
            : assignment
        }),
      } : p))
      await Promise.all([loadRequests(), loadSummary(), loadApprovedPlans()])
    }
    return result
  }, [run, notify, loadRequests, loadSummary, loadApprovedPlans])

  const reviewPlan = useCallback(async (reason) => {
    const result = await run('reviewPlan', () => api.reviewPlan({ decision: 'reject', reason }))
    if (result) {
      setPlan(result.plan)
      notify('Plan rejected and recomputed with its resources excluded', 'warn')
      await loadMetrics()
    }
    return result
  }, [run, notify, loadMetrics])

  const closeRoad = useCallback(
    async (edgeId) => {
      const result = await run('replan', () => api.closeRoad(edgeId))
      if (result) {
        setReplan(result)
        notify(result.summary || 'Road closure recorded', 'warn')
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
      const result = await run('reopen', () => api.reopenRoad(edgeId))
      if (result) {
        if (result.status === 'REPLAN_SCHEDULED') setReplan(result)
        await loadGraph()
      }
      return result
    },
    [run, loadGraph],
  )

  const completeRequest = useCallback(
    async (requestId) => {
      const result = await run('complete', () => api.completeRequest(requestId), {
        successMessage: `${requestId} marked served`,
      })
      if (result) await Promise.all([loadRequests(), loadSummary(), loadResources()])
      return result
    },
    [run, loadRequests, loadSummary, loadResources],
  )

  const reportProgress = useCallback(async (requestId, event) => {
    const result = await run(`progress:${requestId}`, () =>
      api.reportProgress(requestId, event),
    )
    if (!result) return null

    notify(
      event === 'picked_up'
        ? 'Pickup check-in recorded'
        : 'Delivery recorded; assigned resources are available again',
      'success',
    )
    await Promise.all([loadRequests(), loadSummary()])
    try {
      setPlan(await api.currentPlan())
    } catch (error) {
      notify(`Check-in recorded, but the plan could not be refreshed: ${error.message}`, 'warn')
    }
    return result
  }, [run, notify, loadRequests, loadSummary])

  const releaseAll = useCallback(async () => {
    const result = await run('release', () => api.releaseAllResources())
    if (result) {
      notify(`${result.resources_freed} resources released`, 'success')
      await Promise.all([loadSummary(), loadResources()])
    }
    return result
  }, [run, notify, loadSummary, loadResources])

  const resetAll = useCallback(async () => {
    const result = await run('reset', () => api.reset())
    if (result) {
      setPlan(null)
      approvedRequestIds.current.clear()
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
    resources,
    approvedPlans,
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
    reviewRequest,
    dismissRequest,
    generatePlan,
    approvePlan,
    reviewPlan,
    closeRoad,
    approveReplan,
    reopenRoad,
    completeRequest,
    reportProgress,
    releaseAll,
    resetAll,
    loadMetrics,
  }
}
