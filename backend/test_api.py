"""End-to-end smoke test over the live HTTP API.

Start the server first:
    uvicorn main:app --port 8080
Then:
    python test_api.py

Walks the full coordinator flow: intake -> dispatch -> approve -> road
closure -> replan -> metrics, and fails loudly if any step regresses.
"""
import os
import sys

import httpx

BASE = os.getenv("RESCUEIQ_API", "http://127.0.0.1:8080")

MESSAGES = [
    "Help!! 6 people stuck on roof near City Bank Market Road, 2 children",
    "मेरे घर में पानी भर गया है, 4 लोग हैं, बुजुर्ग माँ है चल नहीं सकती",
    "HELP ward7 paani bahut zyada 3 log please boat jaldi",
    "Diabetic patient needs insulin urgently, water rising",
]

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def main():
    client = httpx.Client(base_url=BASE, timeout=30.0)

    print("\n== 0. reset ==")
    r = client.post("/reset")
    check("reset returns 200", r.status_code == 200, str(r.status_code))

    print("\n== 1. health ==")
    r = client.get("/health")
    body = r.json()
    check("health ok", body.get("status") == "healthy", str(body))

    print("\n== 2. resources summary ==")
    summary = client.get("/resources/summary").json()
    check("has boats", "boat" in summary, str(summary))
    print(f"      {summary}")

    print("\n== 3. intake (4 languages) ==")
    created_ids = []
    for msg in MESSAGES:
        res = client.post("/intake/process", json={"message": msg}).json()
        created_ids.append(res["requestId"])
        print(
            f"      {res['requestId']}  {res['language']:<20} "
            f"{res['urgency']:<9} people={res['people_count']} "
            f"node={res['node_id']}"
        )
        check(
            f"extracted {res['requestId']}",
            res.get("urgency") in ("CRITICAL", "HIGH", "MEDIUM", "LOW"),
        )

    pending = client.get("/requests/pending").json()
    check("4 pending requests", pending["count"] == 4, f"got {pending['count']}")

    print("\n== 4. dispatch plan ==")
    plan = client.post("/dispatch/plan").json()
    check("plan has assignments", plan.get("total_assigned", 0) > 0,
          f"assigned={plan.get('total_assigned')} unassigned={plan.get('total_unassigned')}")
    for a in plan.get("assignments", []):
        route = " -> ".join(a["route"]["path_names"])
        print(f"      {a['resource']['name']} -> {a['request']['location_description']}")
        print(f"        ETA {a['route']['minutes']} min | {route}")
        print(f"        edges: {a['route']['edges']}")
        print(f"        why: {a['explanation']}")
        check(
            f"{a['resource']['resourceId']} has a real route",
            a["route"]["minutes"] >= 0 and a["route"]["edges"],
        )
    print(f"      tradeoff: {plan.get('tradeoff')}")
    print(f"      plan metrics: {plan.get('metrics')}")

    # Critical requests must be scheduled ahead of non-critical ones.
    urgencies = [a["request"]["urgency"] for a in plan["assignments"]]
    first_non_critical = next(
        (i for i, u in enumerate(urgencies) if u != "CRITICAL"), len(urgencies)
    )
    last_critical = max(
        (i for i, u in enumerate(urgencies) if u == "CRITICAL"), default=-1
    )
    check("critical cases ordered first", last_critical < first_non_critical,
          str(urgencies))

    print("\n== 5. metrics vs baseline ==")
    m = client.get("/metrics").json()
    print(f"      baseline: {m['baseline']}")
    print(f"      rescueiq: {m['rescueiq']}")
    print(f"      improvement: {m['improvement']}")
    check("baseline computed", m["baseline"]["assigned"] > 0)
    check("rescueiq critical-first >= baseline",
          m["rescueiq"]["critical_first_pct"] >= m["baseline"]["critical_first_pct"],
          f"{m['rescueiq']['critical_first_pct']} vs {m['baseline']['critical_first_pct']}")

    print("\n== 6. approve plan ==")
    approved = client.post("/dispatch/approve", json={"approved_by": "coordinator"}).json()
    check("assignments approved", approved.get("approved_count", 0) > 0,
          str(approved.get("approved_count")))
    after = client.get("/resources/summary").json()
    check("resources marked busy",
          after["boat"]["available"] < summary["boat"]["available"],
          f"{summary['boat']} -> {after['boat']}")

    print("\n== 7. road closure replan ==")
    closed_edge = None
    for a in plan["assignments"]:
        if a["route"]["edges"]:
            closed_edge = a["route"]["edges"][0]
            break
    print(f"      closing {closed_edge}")
    replan = client.post("/replan/road-closure", json={"edge_id": closed_edge}).json()
    check("replan ran", "assignments" in replan, str(replan)[:200])
    print(f"      {replan.get('summary')}")
    print(f"      before: {replan.get('metrics_before')}")
    print(f"      after:  {replan.get('metrics_after')}")
    for a in replan.get("assignments", []):
        if a["status"] == "REPLANNED":
            print(f"      REPLANNED {a['resource']['name']}: "
                  f"{a['old_route']['minutes']} -> {a['new_route']['minutes']} min "
                  f"(+{a['delay_minutes']})")
            print(f"        {a['explanation']}")
            check("rerouted away from closed edge",
                  closed_edge not in a["new_route"]["edges"])
        elif a["status"] == "BLOCKED":
            print(f"      BLOCKED {a['resource']['name']}")
    check("after-metrics cover all assignments",
          replan["metrics_after"].get("avg_response_time", 0) > 0,
          str(replan.get("metrics_after")))

    print("\n== 8. unknown edge rejected ==")
    r = client.post("/replan/road-closure", json={"edge_id": "NOPE__NOPE"})
    check("unknown edge -> 400", r.status_code == 400, str(r.status_code))

    print("\n== 9. graph ==")
    g = client.get("/graph").json()
    check("graph has 12 nodes", len(g["nodes"]) == 12, str(len(g["nodes"])))
    check("closed edge listed", closed_edge in g["closed_edges"], str(g["closed_edges"]))

    print("\n== 10. reset restores state ==")
    reset_result = client.post("/reset").json()
    print(f"      {reset_result}")
    check("roads reopened", client.get("/graph").json()["closed_edges"] == [])
    # With Firestore connected, seeded requests legitimately stay pending;
    # what must be gone are the requests this session created.
    remaining = {
        r.get("requestId") for r in client.get("/requests").json()["requests"]
    }
    session_ids = set(created_ids)
    check("session requests cleared", not (remaining & session_ids),
          f"still present: {sorted(remaining & session_ids)}")
    restored = client.get("/resources/summary").json()
    check("resources freed", restored["boat"]["available"] == summary["boat"]["available"],
          f"{restored['boat']} vs {summary['boat']}")

    print("\n" + "=" * 50)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
