"""Road graph resilience test.

The replanning demo only works if closing a road forces a *reroute*. If any
node hangs off a single edge, closing that edge strands it and the agent can
only report BLOCKED. This test fails if that ever becomes true again.

    python test_graph.py
"""
import itertools
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.optimizer.router import DistrictRouter  # noqa: E402

GRAPH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "road_graph.json"
)


def load():
    with open(GRAPH_PATH, encoding="utf-8") as f:
        return json.load(f)


def main():
    graph = load()
    nodes = list(graph["nodes"])
    failures = []

    print(f"Graph: {len(nodes)} nodes, {len(graph['edges'])} edges\n")

    # 1. No dead ends.
    degree = Counter()
    for edge in graph["edges"].values():
        degree[edge["a"]] += 1
        degree[edge["b"]] += 1
    for node in nodes:
        if degree[node] < 2:
            failures.append(f"{node} ({graph['nodes'][node]['name']}) is a dead end")
    print(f"[{'PASS' if not failures else 'FAIL'}] every node has >= 2 roads")

    # 2. Every single-edge closure still leaves the district connected.
    unreachable = 0
    for edge_id in graph["edges"]:
        router = DistrictRouter(load())
        router.closed_edges.add(edge_id)  # add directly to keep output quiet
        for a, b in itertools.permutations(nodes, 2):
            if router.shortest_path(a, b).get("blocked"):
                failures.append(f"{a}->{b} unreachable when {edge_id} closed")
                unreachable += 1
    print(f"[{'PASS' if unreachable == 0 else 'FAIL'}] "
          f"all {len(graph['edges'])} single-edge closures keep district connected "
          f"({unreachable} unreachable pairs)")

    # 3. The scripted demo closure reroutes instead of blocking.
    router = DistrictRouter(load())
    before = router.shortest_path("N01", "N05")
    router.closed_edges.add("N09__N10")
    after = router.shortest_path("N01", "N05")
    rerouted = not after["blocked"] and after["minutes"] > before["minutes"]
    print(f"[{'PASS' if rerouted else 'FAIL'}] Bridge Road closure reroutes "
          f"Control Room -> Ward 7: {before['minutes']} -> {after['minutes']} min")
    print(f"       before: {' -> '.join(before['path_names'])}")
    print(f"       after : {' -> '.join(after['path_names'])}")
    if not rerouted:
        failures.append("Bridge Road closure does not produce a reroute")

    # 4. Edges are traversable in both directions regardless of a/b order.
    router = DistrictRouter(load())
    for edge_id, edge in graph["edges"].items():
        fwd = router.shortest_path(edge["a"], edge["b"])
        rev = router.shortest_path(edge["b"], edge["a"])
        if fwd["minutes"] != rev["minutes"]:
            failures.append(f"{edge_id} asymmetric: {fwd['minutes']} vs {rev['minutes']}")
    print(f"[{'PASS' if not any('asymmetric' in f for f in failures) else 'FAIL'}] "
          "all edges traversable in both directions")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures[:10]:
            print(f"  - {f}")
        sys.exit(1)
    print("Graph OK.")


if __name__ == "__main__":
    main()
