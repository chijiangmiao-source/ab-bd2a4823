"""Minimum-cost Steiner subnet solver with a canonical tie-break rule.

Everything is implemented from scratch -- no edge-set enumeration and no
external optimizers:

* terminal-subset dynamic programming (Dreyfus-Wagner),
* node merge/combine of partial terminal subsets at every vertex,
* multi-source shortest-path closure (Dijkstra) after each merge round.

Canonical witness
-----------------
Among all minimum-cost feasible edge sets, the canonical witness is the
one whose ascending edge-id list is lexicographically smallest.  Edge
costs are positive, so two distinct minimum-cost sets can never be
nested; for non-nested sets, comparing ascending id lists
lexicographically is equivalent to comparing bit-vectors in which edge
rank (by ascending id) selects the bit and the set owning the
smaller-ranked bit of the symmetric difference wins.  Folding that
bit-vector into one additive big-integer weight per edge,
``cost * 2**m - 2**(m - 1 - rank)``, lets the DP optimise total cost and
canonical order in a single pass.  The combined weight is injective on
edge sets, hence the optimal edge set is unique and reconstruction is
deterministic.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

MIN_NODES = 2
MAX_NODES = 60
MIN_EDGES = 1
MAX_EDGES = 220
MIN_TERMINALS = 2
MAX_TERMINALS = 10
MIN_EDGE_COST = 1
MAX_EDGE_COST = 10 ** 9
NAME_MAX_LEN = 64


class SolveError(Exception):
    """Domain error carrying a stable, machine-readable code."""

    def __init__(self, code, message, field=None, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.http_status = http_status

    def to_dict(self):
        body = {"code": self.code, "message": self.message}
        if self.field is not None:
            body["field"] = self.field
        return body


@dataclass(frozen=True)
class Edge:
    eid: str
    u: str
    v: str
    cost: int


@dataclass
class Instance:
    nodes: list
    edges: list
    terminals: list


def _is_name(value):
    """Visible (non-space) ASCII token of bounded length."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= NAME_MAX_LEN
        and all(0x21 <= ord(ch) <= 0x7E for ch in value)
    )


def validate(payload):
    """Validate a request payload, raising SolveError with a stable code."""
    if not isinstance(payload, dict):
        raise SolveError("PAYLOAD_NOT_OBJECT", "request body must be a JSON object")
    for key in ("nodes", "edges", "terminals"):
        if key not in payload:
            raise SolveError("MISSING_FIELD", "missing required field '%s'" % key, field=key)

    nodes = payload["nodes"]
    if not isinstance(nodes, list):
        raise SolveError("INVALID_FIELD_TYPE", "'nodes' must be an array of names", field="nodes")
    if not MIN_NODES <= len(nodes) <= MAX_NODES:
        raise SolveError(
            "NODE_COUNT",
            "node count must be between %d and %d" % (MIN_NODES, MAX_NODES),
            field="nodes",
        )
    node_set = set()
    for i, name in enumerate(nodes):
        if not _is_name(name):
            raise SolveError(
                "INVALID_NODE_NAME",
                "node name must be 1..%d visible ASCII characters" % NAME_MAX_LEN,
                field="nodes[%d]" % i,
            )
        if name in node_set:
            raise SolveError(
                "DUPLICATE_NODE", "duplicate node name '%s'" % name, field="nodes[%d]" % i
            )
        node_set.add(name)

    raw_edges = payload["edges"]
    if not isinstance(raw_edges, list):
        raise SolveError("INVALID_FIELD_TYPE", "'edges' must be an array", field="edges")
    if not MIN_EDGES <= len(raw_edges) <= MAX_EDGES:
        raise SolveError(
            "EDGE_COUNT",
            "edge count must be between %d and %d" % (MIN_EDGES, MAX_EDGES),
            field="edges",
        )
    edges = []
    seen_ids = set()
    for i, item in enumerate(raw_edges):
        base = "edges[%d]" % i
        if not isinstance(item, dict):
            raise SolveError(
                "INVALID_EDGE", "edge must be an object with id/from/to/cost", field=base
            )
        for key in ("id", "from", "to", "cost"):
            if key not in item:
                raise SolveError("INVALID_EDGE", "edge is missing key '%s'" % key, field=base)
        eid = item["id"]
        if not _is_name(eid):
            raise SolveError(
                "INVALID_EDGE_ID",
                "edge id must be 1..%d visible ASCII characters" % NAME_MAX_LEN,
                field=base + ".id",
            )
        if eid in seen_ids:
            raise SolveError(
                "DUPLICATE_EDGE_ID", "duplicate edge id '%s'" % eid, field=base + ".id"
            )
        seen_ids.add(eid)
        u, v = item["from"], item["to"]
        for endpoint, key in ((u, "from"), (v, "to")):
            if not isinstance(endpoint, str):
                raise SolveError(
                    "INVALID_EDGE",
                    "edge endpoint must be a node name string",
                    field="%s.%s" % (base, key),
                )
            if endpoint not in node_set:
                raise SolveError(
                    "UNKNOWN_NODE",
                    "edge endpoint '%s' is not a declared node" % endpoint,
                    field="%s.%s" % (base, key),
                )
        if u == v:
            raise SolveError("SELF_LOOP", "self-loops are not allowed ('%s')" % eid, field=base)
        cost = item["cost"]
        if (
            isinstance(cost, bool)
            or not isinstance(cost, int)
            or not MIN_EDGE_COST <= cost <= MAX_EDGE_COST
        ):
            raise SolveError(
                "INVALID_COST",
                "edge cost must be an integer between %d and %d" % (MIN_EDGE_COST, MAX_EDGE_COST),
                field=base + ".cost",
            )
        edges.append(Edge(eid, u, v, cost))

    terminals = payload["terminals"]
    if not isinstance(terminals, list):
        raise SolveError(
            "INVALID_FIELD_TYPE", "'terminals' must be an array of node names", field="terminals"
        )
    if not MIN_TERMINALS <= len(terminals) <= MAX_TERMINALS:
        raise SolveError(
            "TERMINAL_COUNT",
            "terminal count must be between %d and %d" % (MIN_TERMINALS, MAX_TERMINALS),
            field="terminals",
        )
    seen_terminals = set()
    for i, t in enumerate(terminals):
        if not isinstance(t, str):
            raise SolveError(
                "INVALID_TERMINAL", "terminal must be a node name string", field="terminals[%d]" % i
            )
        if t in seen_terminals:
            raise SolveError(
                "DUPLICATE_TERMINAL", "duplicate terminal '%s'" % t, field="terminals[%d]" % i
            )
        seen_terminals.add(t)
        if t not in node_set:
            raise SolveError(
                "UNKNOWN_TERMINAL",
                "terminal '%s' is not a declared node (dangling terminal)" % t,
                field="terminals[%d]" % i,
            )

    return Instance(nodes=list(nodes), edges=edges, terminals=list(terminals))


def _require_connectable(inst):
    """All terminals must share one connected component (union-find)."""
    parent = {name: name for name in inst.nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in inst.edges:
        ra, rb = find(e.u), find(e.v)
        if ra != rb:
            parent[ra] = rb
    if len({find(t) for t in inst.terminals}) > 1:
        raise SolveError(
            "UNCONNECTABLE",
            "the terminals span multiple connected components; no subnet connects them all",
            http_status=422,
        )


def solve(inst):
    """Solve one validated instance; return the canonical witness subnet."""
    _require_connectable(inst)
    n = len(inst.nodes)
    index = {name: i for i, name in enumerate(inst.nodes)}
    m = len(inst.edges)

    # Canonical ranking: ascending edge id -> bit position (most significant first).
    rank = {eid: r for r, eid in enumerate(sorted(e.eid for e in inst.edges))}

    adj = [[] for _ in range(n)]
    by_id = {}
    for e in inst.edges:
        # One additive weight per edge encoding (total cost, canonical order):
        # minimising the sum minimises cost first, then the lexicographic
        # order of the ascending edge-id list.
        w = (e.cost << m) - (1 << (m - 1 - rank[e.eid]))
        a, b = index[e.u], index[e.v]
        adj[a].append((b, w, e.eid))
        adj[b].append((a, w, e.eid))
        by_id[e.eid] = e

    terminals = [index[t] for t in inst.terminals]
    k = len(terminals)
    full = (1 << k) - 1

    dp = [[None] * n for _ in range(1 << k)]       # combined-weight optimum per (mask, node)
    choice = [[0] * n for _ in range(1 << k)]      # merge-phase split (submask) per (mask, node)
    parent = [[None] * n for _ in range(1 << k)]   # closure-phase (prev node, edge id)

    def closure(mask):
        """Multi-source shortest-path closure (Dijkstra) of dp[mask]."""
        dist = dp[mask]
        par = parent[mask]
        heap = [(d, v) for v, d in enumerate(dist) if d is not None]
        heapq.heapify(heap)
        done = [False] * n
        while heap:
            d, u = heapq.heappop(heap)
            if done[u]:
                continue
            done[u] = True
            for w, wt, eid in adj[u]:
                if done[w]:
                    continue
                nd = d + wt
                if dist[w] is None or nd < dist[w]:
                    dist[w] = nd
                    par[w] = (u, eid)
                    heapq.heappush(heap, (nd, w))

    # Singleton subsets: zero at their own terminal, closure everywhere else.
    for i, t in enumerate(terminals):
        dp[1 << i][t] = 0
        closure(1 << i)

    # Composite subsets: node merge/combine over bipartitions, then closure.
    for mask in range(3, full + 1):
        if mask & (mask - 1) == 0:
            continue  # singleton, already solved
        dm = dp[mask]
        ch = choice[mask]
        sub = (mask - 1) & mask
        while sub:
            other = mask ^ sub
            if other and sub < other:  # each bipartition considered once
                ds, do = dp[sub], dp[other]
                for v in range(n):
                    a = ds[v]
                    if a is None:
                        continue
                    b = do[v]
                    if b is None:
                        continue
                    cand = a + b
                    if dm[v] is None or cand < dm[v]:
                        dm[v] = cand
                        ch[v] = sub
            sub = (sub - 1) & mask
        closure(mask)

    best_v, best_w = -1, None
    for v in range(n):
        d = dp[full][v]
        if d is not None and (best_w is None or d < best_w):
            best_v, best_w = v, d
    if best_v < 0:  # unreachable in practice: connectability pre-checked
        raise SolveError(
            "UNCONNECTABLE", "no subnet connects all terminals", http_status=422
        )

    # Reconstruct the (unique) canonical edge set by walking the DP choices.
    chosen = set()

    def build(mask, v):
        u = v
        while True:
            step = parent[mask][u]
            if step is None:
                break
            prev, eid = step
            chosen.add(eid)
            u = prev
        if mask & (mask - 1) == 0:
            return  # singleton: root is the terminal itself
        sub = choice[mask][u]
        if not sub:
            raise SolveError("INTERNAL", "reconstruction failed", http_status=500)
        build(sub, u)
        build(mask ^ sub, u)

    build(full, best_v)

    edge_ids = sorted(chosen)
    min_cost = sum(by_id[eid].cost for eid in edge_ids)

    adjacency = {}
    for eid in edge_ids:
        e = by_id[eid]
        adjacency.setdefault(e.u, []).append({"node": e.v, "edgeId": eid, "cost": e.cost})
        adjacency.setdefault(e.v, []).append({"node": e.u, "edgeId": eid, "cost": e.cost})
    for entries in adjacency.values():
        entries.sort(key=lambda item: (item["node"], item["edgeId"]))

    return {
        "minCost": min_cost,
        "edgeIds": edge_ids,
        "edgeCount": len(edge_ids),
        "terminals": list(inst.terminals),
        "adjacency": {name: adjacency[name] for name in sorted(adjacency)},
    }


def solve_payload(payload):
    """Validate a raw payload and solve it (stateless, no caching)."""
    return solve(validate(payload))
