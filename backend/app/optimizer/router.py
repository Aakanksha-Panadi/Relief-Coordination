import heapq

class DistrictRouter:
    def __init__(self, graph_data: dict):
        self.nodes = graph_data.get("nodes", {})
        self.edges = graph_data.get("edges", {})
        self.closed_edges = set(graph_data.get("closedEdges", []))

    def close_edge(self, edge_id: str):
        self.closed_edges.add(edge_id)
        print(f"Edge closed: {edge_id}")

    def open_edge(self, edge_id: str):
        self.closed_edges.discard(edge_id)

    def get_neighbors(self, node_id: str) -> list:
        neighbors = []
        for edge_id, edge in self.edges.items():
            if edge_id in self.closed_edges:
                continue
            # Handle both directions regardless of a/b order
            a = edge.get("a")
            b = edge.get("b")
            minutes = edge.get("minutes", 99)
            if a == node_id:
                neighbors.append((b, minutes, edge_id))
            elif b == node_id:
                neighbors.append((a, minutes, edge_id))
        return neighbors

    def shortest_path(self, start: str, end: str) -> dict:
        if start == end:
            return {
                "path": [start],
                "path_names": [self.nodes.get(start, {}).get("name", start)],
                "minutes": 0,
                "edges": [],
                "blocked": False
            }

        distances = {start: 0}
        previous = {start: None}
        edge_used = {start: None}
        pq = [(0, start)]

        while pq:
            dist, node = heapq.heappop(pq)
            if node == end:
                break
            if dist > distances.get(node, float('inf')):
                continue
            for neighbor, weight, eid in self.get_neighbors(node):
                new_dist = dist + weight
                if new_dist < distances.get(neighbor, float('inf')):
                    distances[neighbor] = new_dist
                    previous[neighbor] = node
                    edge_used[neighbor] = eid
                    heapq.heappush(pq, (new_dist, neighbor))

        if end not in distances:
            return {
                "path": [],
                "path_names": [],
                "minutes": -1,
                "edges": [],
                "blocked": True
            }

        # Reconstruct path
        path = []
        edges = []
        current = end
        while current is not None:
            path.append(current)
            if edge_used.get(current):
                edges.append(edge_used[current])
            current = previous.get(current)
        path.reverse()
        edges.reverse()

        return {
            "path": path,
            "path_names": [
                self.nodes.get(n, {}).get("name", n) for n in path
            ],
            "minutes": distances[end],
            "edges": edges,
            "blocked": False
        }

    def debug_path(self, start: str, end: str):
        result = self.shortest_path(start, end)
        print(f"\nPath {start} → {end}:")
        print(f"  Route: {' → '.join(result.get('path_names', []))}")
        print(f"  Minutes: {result.get('minutes')}")
        print(f"  Edges used: {result.get('edges')}")
        print(f"  Blocked: {result.get('blocked')}")
        return result