"""Directed-graph helpers: topological order and deterministic cycle report."""

from __future__ import annotations

from collections import deque
from typing import Dict, Iterable, List, Optional, Tuple


def topological_order(
    nodes: Iterable[str], edges: Dict[str, List[str]]
) -> Tuple[List[str], List[str]]:
    """Kahn's algorithm with smallest-id tie-breaking.

    ``edges[u]`` lists the successors of ``u`` (u -> v means u feeds v).
    Returns ``(order, remaining)``; ``remaining`` is non-empty iff a cycle
    exists.
    """
    nodes = sorted(set(nodes))
    indeg: Dict[str, int] = {n: 0 for n in nodes}
    adj: Dict[str, List[str]] = {n: [] for n in nodes}
    for u in edges:
        if u not in indeg:
            continue
        for v in edges[u]:
            if v not in indeg:
                continue
            # A self-loop simply keeps the node from ever reaching indegree 0.
            adj[u].append(v)
            indeg[v] += 1

    ready = deque(n for n in nodes if indeg[n] == 0)
    order: List[str] = []
    while ready:
        n = ready.popleft()
        order.append(n)
        for v in sorted(adj[n]):
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
    remaining = [n for n in nodes if indeg[n] > 0]
    return order, sorted(remaining)


def _tarjan_scc(nodes: List[str], adj: Dict[str, List[str]]) -> List[List[str]]:
    """Iterative Tarjan (avises recursion limits on big graphs)."""
    index = {}
    low = {}
    on_stack = set()
    stack: List[str] = []
    sccs: List[List[str]] = []
    counter = 0

    for root in nodes:
        if root in index:
            continue
        work: List[Tuple[str, int]] = [(root, 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            n, i = work[-1]
            neighbours = adj.get(n, [])
            if i < len(neighbours):
                v = neighbours[i]
                work[-1] = (n, i + 1)
                if v not in index:
                    index[v] = low[v] = counter
                    counter += 1
                    stack.append(v)
                    on_stack.add(v)
                    work.append((v, 0))
                elif v in on_stack:
                    low[n] = min(low[n], index[v])
            else:
                if low[n] == index[n]:
                    comp = []
                    while True:
                        v = stack.pop()
                        on_stack.discard(v)
                        comp.append(v)
                        if v == n:
                            break
                    sccs.append(comp)
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[n])
    return sccs


def find_cycle(nodes: Iterable[str], edges: Dict[str, List[str]]) -> Optional[List[str]]:
    """Return one cycle as an ordered node list (closing node repeated).

    Self loops report as ``[n, n]``.  Nodes participating in the cycle are
    reported in edge order; smallest cycle member starts the list, giving a
    stable message across requests.
    """
    nodes = sorted(set(nodes))
    adj = {n: sorted(v for v in edges.get(n, []) if v in nodes) for n in nodes}

    # Self-loop first (cheapest, and clearest message).
    for n in nodes:
        if n in adj[n]:
            return [n, n]

    sccs = _tarjan_scc(nodes, adj)
    cyclic = [c for c in sccs if len(c) > 1]
    if not cyclic:
        return None
    comp = min(cyclic, key=lambda c: min(c))
    members = set(comp)
    start = min(comp)

    # Walk a path inside the SCC until we return to start.
    path = [start]
    current = start
    visited_edges = set()
    while True:
        nxt = next((v for v in adj[current]
                    if v in members and (current, v) not in visited_edges), None)
        if nxt is None:
            # Backtrack.
            path.pop()
            current = path[-1]
            continue
        visited_edges.add((current, nxt))
        if nxt == start:
            path.append(start)
            return path
        path.append(nxt)
        current = nxt
