import heapq

class DistrictRouter:
    def __init__(self, graph_data: dict):
        self.nodes = graph_data.get("nodes", {})
        self.edges = graph_data.get("edges", {})
        self.closed_edges = set(graph_data.get("closedEdges", []))
        self.revision = 0

    def close_edge(self, edge_id: str):
        changed = edge_id not in self.closed_edges
        if changed:
            self.closed_edges.add(edge_id)
            self.revision += 1
            print(f"Edge closed: {edge_id}")
        return changed

    def open_edge(self, edge_id: str):
        changed = edge_id in self.closed_edges
        if changed:
            self.closed_edges.discard(edge_id)
            self.revision += 1
        return changed

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

    def edge_minutes(self, edge_id: str) -> int:
        return self.edges.get(edge_id, {}).get("minutes", 0)

    def path_minutes(self, edge_ids: list) -> int:
        """Travel time over a list of edges, used to price a partial journey."""
        return sum(self.edge_minutes(e) for e in edge_ids)

    def connected_components(self) -> list[list[str]]:
        """Areas separated by the current set of road closures."""
        unseen = set(self.nodes)
        components = []
        while unseen:
            start = min(unseen)
            stack, component = [start], set()
            while stack:
                node = stack.pop()
                if node not in unseen:
                    continue
                unseen.remove(node)
                component.add(node)
                stack.extend(n for n, _minutes, _edge in self.get_neighbors(node) if n in unseen)
            components.append(sorted(component))
        return components

    def debug_path(self, start: str, end: str):
        result = self.shortest_path(start, end)
        print(f"\nPath {start} → {end}:")
        print(f"  Route: {' → '.join(result.get('path_names', []))}")
        print(f"  Minutes: {result.get('minutes')}")
        print(f"  Edges used: {result.get('edges')}")
        print(f"  Blocked: {result.get('blocked')}")
        return result
