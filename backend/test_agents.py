import json
from app.main_orchestrator import RescueIQOrchestrator
from app.optimizer.router import DistrictRouter

print("Initializing RescueIQ...")
system = RescueIQOrchestrator()

print("\n--- TEST 1: English intake ---")
r1 = system.process_intake(
    "Help!! 6 people stuck on roof near City Bank Market Road, 2 children"
)
print(f"Language: {r1['language']}")
print(f"Urgency: {r1['urgency']}")
print(f"People: {r1['people_count']}")

print("\n--- TEST 2: Hindi intake ---")
r2 = system.process_intake(
    "मेरे घर में पानी भर गया है, 4 लोग हैं, बुजुर्ग माँ है"
)
print(f"Language: {r2['language']}")
print(f"Urgency: {r2['urgency']}")
print(f"Vulnerable: {r2['vulnerable_details']}")

print("\n--- TEST 3: Mixed intake ---")
r3 = system.process_intake(
    "HELP ward7 paani bahut zyada 3 log please boat jaldi"
)
print(f"Language: {r3['language']}")
print(f"Urgency: {r3['urgency']}")

print("\n--- TEST 4: Dispatch plan ---")
plan = system.generate_dispatch_plan()
print(f"Assignments: {plan['total_assigned']}")
print(f"Unassigned: {plan['total_unassigned']}")
for a in plan['assignments']:
    print(f"\n  {a['resource']['name']} → {a['request']['location_description']}")
    print(f"  ETA: {a['route']['minutes']} min")
    print(f"  Edges: {a['route']['edges']}")
    print(f"  Why: {a['explanation']}")
print(f"\nTradeoff: {plan['tradeoff']}")

print("\n--- DEBUG: Route from N12 to N05 ---")
with open('../data/road_graph.json') as f:
    graph = json.load(f)
router = DistrictRouter(graph)
result = router.shortest_path("N12", "N05")
print(f"Path: {' → '.join(result.get('path_names', []))}")
print(f"Minutes: {result.get('minutes')}")
print(f"Edges used: {result.get('edges')}")

print("\n--- TEST 5: Road closure replan ---")
replan = system.simulate_road_closure("N05__N09")
print(f"Affected: {replan['affected_count']}")
print(f"Summary: {replan['summary']}")
for a in replan['assignments']:
    if a['status'] == 'REPLANNED':
        print(f"\n  {a['resource']['name']}")
        print(f"  Old ETA: {a['old_route']['minutes']} min")
        print(f"  New ETA: {a['new_route']['minutes']} min")
        print(f"  Delay: +{a['delay_minutes']} min")
        print(f"  Why: {a['explanation']}")
    elif a['status'] == 'BLOCKED':
        print(f"\n  BLOCKED: {a['resource']['name']}")
        print(f"  {a['explanation']}")
    else:
        print(f"  Unchanged: {a['resource']['name']}")