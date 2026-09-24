"""One-shot verification suite for the Steiner audit service.

Runs once inside the Compose topology and reports via process exit code:

  1. solver tests          unit tests + brute-force cross-checks of the
                           minimum cost and the canonical edge set
  2. build artifact checks expected files/modules of the built image
  3. HTTP smoke tests      happy path, tie-break verdicts, infeasible and
                           validation boundaries, determinism/statelessness

Exits 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import importlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

APP_ROOT = os.environ.get(
    "APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

from app.solver import SolveError, solve_payload  # noqa: E402

API_URL = os.environ.get("API_URL", "http://127.0.0.1:8080").rstrip("/")

RESULTS = {"passed": 0, "failed": 0}


def check(name, cond, detail=""):
    if cond:
        RESULTS["passed"] += 1
        print("  PASS %s" % name)
    else:
        RESULTS["failed"] += 1
        print("  FAIL %s%s" % (name, (" :: %s" % detail) if detail else ""))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def payload(nodes, edges, terminals):
    return {"nodes": nodes, "edges": edges, "terminals": terminals}


def expect_error(name, pl, code):
    try:
        solve_payload(pl)
    except SolveError as exc:
        check(name, exc.code == code, "got code %s" % exc.code)
        return
    check(name, False, "expected SolveError %s" % code)


def expect_solution(name, pl, min_cost, edge_ids):
    try:
        res = solve_payload(pl)
    except SolveError as exc:
        check(name, False, "unexpected error %s" % exc.code)
        return
    ok = res["minCost"] == min_cost and res["edgeIds"] == edge_ids
    check(name, ok, "got (%s, %s), want (%s, %s)"
          % (res["minCost"], res["edgeIds"], min_cost, edge_ids))


def brute_force(nodes, edges, terminals):
    """Reference: enumerate edge subsets (tiny instances only)."""
    m = len(edges)
    best = None
    for mask in range(1, 1 << m):
        parent = {x: x for x in nodes}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        cost = 0
        ids = []
        for i, e in enumerate(edges):
            if mask & (1 << i):
                cost += e["cost"]
                ids.append(e["id"])
                ra, rb = find(e["from"]), find(e["to"])
                if ra != rb:
                    parent[ra] = rb
        if best is not None and cost > best[0]:
            continue
        if len({find(t) for t in terminals}) != 1:
            continue
        ids.sort()
        cand = (cost, ids)
        if best is None or cand < best:
            best = cand
    return best


# ---------------------------------------------------------------------------
# 1. solver tests
# ---------------------------------------------------------------------------

def solver_tests():
    print("[solver] targeted cases")

    # Simple path: the only feasible subnet.
    expect_solution(
        "simple path",
        payload(["A", "B", "C"],
                [{"id": "e1", "from": "A", "to": "B", "cost": 5},
                 {"id": "e2", "from": "B", "to": "C", "cost": 7}],
                ["A", "C"]),
        12, ["e1", "e2"],
    )

    # Non-terminal relay (Steiner point): star through O beats direct edges.
    expect_solution(
        "relay via steiner point",
        payload(["A", "B", "C", "O"],
                [{"id": "e1", "from": "A", "to": "O", "cost": 1},
                 {"id": "e2", "from": "B", "to": "O", "cost": 1},
                 {"id": "e3", "from": "C", "to": "O", "cost": 1},
                 {"id": "e4", "from": "A", "to": "B", "cost": 10},
                 {"id": "e5", "from": "B", "to": "C", "cost": 10},
                 {"id": "e6", "from": "A", "to": "C", "cost": 10}],
                ["A", "B", "C"]),
        3, ["e1", "e2", "e3"],
    )

    # Tie verdict: {e1,e2} and {e3} both cost 2; ["e1","e2"] < ["e3"].
    expect_solution(
        "tie-break prefers lexicographically smaller list",
        payload(["A", "B", "C"],
                [{"id": "e1", "from": "A", "to": "B", "cost": 1},
                 {"id": "e2", "from": "B", "to": "C", "cost": 1},
                 {"id": "e3", "from": "A", "to": "C", "cost": 2}],
                ["A", "C"]),
        2, ["e1", "e2"],
    )

    # Tie verdict the other way: ["e1"] < ["e2","e3"].
    expect_solution(
        "tie-break single edge can win",
        payload(["A", "B", "C"],
                [{"id": "e1", "from": "A", "to": "C", "cost": 2},
                 {"id": "e2", "from": "A", "to": "B", "cost": 1},
                 {"id": "e3", "from": "B", "to": "C", "cost": 1}],
                ["A", "C"]),
        2, ["e1"],
    )

    # Same-cost lists of different sizes: ["e1","e8"] < ["e9"].
    expect_solution(
        "tie-break across different list sizes",
        payload(["A", "B", "C"],
                [{"id": "e9", "from": "A", "to": "C", "cost": 2},
                 {"id": "e1", "from": "A", "to": "B", "cost": 1},
                 {"id": "e8", "from": "B", "to": "C", "cost": 1}],
                ["A", "C"]),
        2, ["e1", "e8"],
    )

    # Parallel edges: cheapest wins; equal cost -> smallest id wins.
    expect_solution(
        "parallel edges",
        payload(["A", "B"],
                [{"id": "e1", "from": "A", "to": "B", "cost": 5},
                 {"id": "e2", "from": "A", "to": "B", "cost": 3},
                 {"id": "e3", "from": "A", "to": "B", "cost": 3}],
                ["A", "B"]),
        3, ["e2"],
    )

    # Four terminals on a unit square: any 3 edges connect; canonical is
    # the lexicographically smallest 3-id list.
    expect_solution(
        "square four terminals",
        payload(["A", "B", "C", "D"],
                [{"id": "e1", "from": "A", "to": "B", "cost": 1},
                 {"id": "e2", "from": "B", "to": "C", "cost": 1},
                 {"id": "e3", "from": "C", "to": "D", "cost": 1},
                 {"id": "e4", "from": "D", "to": "A", "cost": 1}],
                ["A", "B", "C", "D"]),
        3, ["e1", "e2", "e3"],
    )

    # Relay shows up in the derived adjacency list.
    res = solve_payload(payload(
        ["A", "B", "C", "O"],
        [{"id": "e1", "from": "A", "to": "O", "cost": 1},
         {"id": "e2", "from": "B", "to": "O", "cost": 1},
         {"id": "e3", "from": "C", "to": "O", "cost": 1}],
        ["A", "B", "C"]))
    check("adjacency covers relay node",
          sorted(res["adjacency"].keys()) == ["A", "B", "C", "O"]
          and len(res["adjacency"]["O"]) == 3)

    print("[solver] error boundaries")
    expect_error("infeasible terminals",
                 payload(["A", "B", "C", "D"],
                         [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                         ["A", "C"]),
                 "UNCONNECTABLE")
    expect_error("isolated terminal node",
                 payload(["A", "B", "C"],
                         [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                         ["A", "C"]),
                 "UNCONNECTABLE")
    expect_error("dangling terminal",
                 payload(["A", "B"],
                         [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                         ["A", "ZZ"]),
                 "UNKNOWN_TERMINAL")
    for bad in (0, -3, 1.5, "5", True, 10 ** 9 + 1):
        expect_error("invalid cost %r" % (bad,),
                     payload(["A", "B"],
                             [{"id": "e1", "from": "A", "to": "B", "cost": bad}],
                             ["A", "B"]),
                     "INVALID_COST")
    expect_error("self loop",
                 payload(["A", "B"],
                         [{"id": "e1", "from": "A", "to": "A", "cost": 1}],
                         ["A", "B"]),
                 "SELF_LOOP")
    expect_error("duplicate edge id",
                 payload(["A", "B"],
                         [{"id": "e1", "from": "A", "to": "B", "cost": 1},
                          {"id": "e1", "from": "A", "to": "B", "cost": 2}],
                         ["A", "B"]),
                 "DUPLICATE_EDGE_ID")
    expect_error("duplicate node",
                 payload(["A", "A"],
                         [{"id": "e1", "from": "A", "to": "A", "cost": 1}],
                         ["A"]),
                 "DUPLICATE_NODE")
    expect_error("unknown edge endpoint",
                 payload(["A", "B"],
                         [{"id": "e1", "from": "A", "to": "ZZ", "cost": 1}],
                         ["A", "B"]),
                 "UNKNOWN_NODE")
    expect_error("too few nodes",
                 payload(["A"],
                         [{"id": "e1", "from": "A", "to": "A", "cost": 1}],
                         ["A"]),
                 "NODE_COUNT")
    expect_error("too many nodes",
                 payload(["n%02d" % i for i in range(61)],
                         [{"id": "e1", "from": "n00", "to": "n01", "cost": 1}],
                         ["n00", "n01"]),
                 "NODE_COUNT")
    expect_error("no edges",
                 payload(["A", "B"], [], ["A", "B"]),
                 "EDGE_COUNT")
    expect_error("too many edges",
                 payload(["A", "B"],
                         [{"id": "e%03d" % i, "from": "A", "to": "B", "cost": 1}
                          for i in range(221)],
                         ["A", "B"]),
                 "EDGE_COUNT")
    expect_error("too few terminals",
                 payload(["A", "B"],
                         [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                         ["A"]),
                 "TERMINAL_COUNT")
    expect_error("too many terminals",
                 payload(["n%02d" % i for i in range(11)],
                         [{"id": "e%02d" % i, "from": "n%02d" % i, "to": "n%02d" % (i + 1),
                           "cost": 1} for i in range(10)],
                         ["n%02d" % i for i in range(11)]),
                 "TERMINAL_COUNT")
    expect_error("duplicate terminal",
                 payload(["A", "B"],
                         [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                         ["A", "A"]),
                 "DUPLICATE_TERMINAL")
    expect_error("payload not object", [1, 2, 3], "PAYLOAD_NOT_OBJECT")
    expect_error("missing field",
                 {"nodes": ["A", "B"],
                  "edges": [{"id": "e1", "from": "A", "to": "B", "cost": 1}]},
                 "MISSING_FIELD")

    print("[solver] determinism")
    pl = payload(["A", "B", "C", "D"],
                 [{"id": "e1", "from": "A", "to": "B", "cost": 2},
                  {"id": "e2", "from": "B", "to": "C", "cost": 2},
                  {"id": "e3", "from": "C", "to": "D", "cost": 2},
                  {"id": "e4", "from": "A", "to": "D", "cost": 5}],
                 ["A", "D"])
    check("same input twice -> identical result",
          solve_payload(pl) == solve_payload(json.loads(json.dumps(pl))))

    print("[solver] random cross-check vs brute force")
    rng = random.Random(20260924)
    n_feasible = n_infeasible = 0
    for trial in range(130):
        n = rng.randint(2, 8)
        nodes = ["n%d" % i for i in range(n)]
        m = rng.randint(1, 11)
        tie_heavy = trial % 2 == 0
        edges = []
        for i in range(m):
            u = rng.choice(nodes)
            v = rng.choice(nodes)
            while v == u:
                v = rng.choice(nodes)
            cost = rng.randint(1, 2) if tie_heavy else rng.randint(1, 9)
            # non-padded ids on purpose: string rank != numeric order
            edges.append({"id": "e%d" % i, "from": u, "to": v, "cost": cost})
        k = rng.randint(2, min(5, n))
        terminals = rng.sample(nodes, k)
        pl = payload(nodes, edges, terminals)
        bf = brute_force(nodes, edges, terminals)
        try:
            res = solve_payload(pl)
            err = None
        except SolveError as exc:
            res, err = None, exc
        if bf is None:
            n_infeasible += 1
            check("random#%d infeasible" % trial,
                  err is not None and err.code == "UNCONNECTABLE",
                  "got %s" % (err.code if err else res))
        else:
            n_feasible += 1
            ok = (err is None and res["minCost"] == bf[0] and res["edgeIds"] == bf[1])
            check("random#%d cost+canonical" % trial, ok,
                  "solver=%s brute=%s"
                  % (None if res is None else (res["minCost"], res["edgeIds"]), bf))
    check("random mix covers both outcomes", n_feasible > 0 and n_infeasible > 0,
          "feasible=%d infeasible=%d" % (n_feasible, n_infeasible))


# ---------------------------------------------------------------------------
# 2. build artifact checks
# ---------------------------------------------------------------------------

def artifact_checks():
    print("[artifacts] expected files in built image")
    for rel in ("app/__init__.py", "app/solver.py", "app/server.py",
                "verify/__init__.py", "verify/verify.py",
                "Dockerfile", "docker-compose.yml"):
        check("artifact %s" % rel, os.path.isfile(os.path.join(APP_ROOT, rel)))

    print("[artifacts] orchestration wiring")
    try:
        with open(os.path.join(APP_ROOT, "docker-compose.yml"), encoding="utf-8") as fh:
            compose = fh.read()
        check("compose healthchecks /healthz", "/healthz" in compose)
        check("compose exposes configurable host port",
              "API_HOST_PORT" in compose and ":8080" in compose)
        check("compose runs one-shot verify service",
              "verify" in compose and "service_healthy" in compose)
    except OSError as exc:
        check("compose file readable", False, str(exc))
    try:
        with open(os.path.join(APP_ROOT, "Dockerfile"), encoding="utf-8") as fh:
            dockerfile = fh.read()
        check("Dockerfile defines default command",
              "CMD" in dockerfile and "app.server" in dockerfile)
    except OSError as exc:
        check("Dockerfile readable", False, str(exc))

    print("[artifacts] modules import cleanly")
    for mod in ("app", "app.solver", "app.server"):
        try:
            importlib.import_module(mod)
            check("import %s" % mod, True)
        except Exception as exc:  # noqa: BLE001
            check("import %s" % mod, False, str(exc))


# ---------------------------------------------------------------------------
# 3. HTTP smoke tests
# ---------------------------------------------------------------------------

def http(method, path, pl=None, raw=None):
    data = raw
    if data is None and pl is not None:
        data = json.dumps(pl).encode("utf-8")
    req = urllib.request.Request(API_URL + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except ValueError:
            return exc.code, None


def wait_for_api():
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(API_URL + "/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return False


def expect_http_error(name, status, code, pl=None, raw=None):
    st, body = http("POST", "/api/audit", pl=pl, raw=raw)
    ok = (
        st == status
        and isinstance(body, dict)
        and set(body.keys()) == {"error"}          # no partial/stale subnet
        and body["error"].get("code") == code
        and isinstance(body["error"].get("message"), str)
    )
    check(name, ok, "status=%s body=%s" % (st, body))


def subnet_consistency(name, pl, resp):
    """The returned adjacency must be exactly what the edge set induces."""
    ok, detail = True, ""
    edges_by_id = {e["id"]: e for e in pl["edges"]}
    ids = resp.get("edgeIds")
    adj = resp.get("adjacency")
    cost = resp.get("minCost")
    if not isinstance(ids, list) or ids != sorted(ids) or len(set(ids)) != len(ids):
        ok, detail = False, "edgeIds not sorted/unique"
    elif any(i not in edges_by_id for i in ids):
        ok, detail = False, "unknown edge id in result"
    if ok and sum(edges_by_id[i]["cost"] for i in ids) != cost:
        ok, detail = False, "minCost does not match edge set"
    cover = {}
    if ok:
        for i in ids:
            e = edges_by_id[i]
            cover.setdefault(e["from"], []).append((e["to"], i))
            cover.setdefault(e["to"], []).append((e["from"], i))
        adj_norm = {k: sorted((x["node"], x["edgeId"]) for x in v)
                    for k, v in (adj or {}).items()}
        cov_norm = {k: sorted(v) for k, v in cover.items()}
        if adj_norm != cov_norm:
            ok, detail = False, "adjacency does not match edge set"
    if ok:
        seen, stack = set(), [next(iter(cover))]
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(n for n, _ in cover[x])
        if seen != set(cover):
            ok, detail = False, "subnet not connected"
        elif len(ids) != len(cover) - 1:
            ok, detail = False, "subnet is not a tree"
        elif not all(t in cover for t in pl["terminals"]):
            ok, detail = False, "terminal not covered"
    check(name, ok, detail)


def big_case():
    """Boundary-size valid instance: 60 nodes / 220 edges / 10 terminals."""
    rng = random.Random(99)
    nodes = ["n%02d" % i for i in range(60)]
    edges = [{"id": "p%02d" % i, "from": nodes[i], "to": nodes[i + 1],
              "cost": (i % 7) + 1} for i in range(59)]
    c = 0
    while len(edges) < 220:
        a, b = rng.randrange(60), rng.randrange(60)
        if a == b:
            continue
        edges.append({"id": "x%03d" % c, "from": nodes[a], "to": nodes[b],
                      "cost": rng.randint(1, 20)})
        c += 1
    terminals = ["n%02d" % (6 * i) for i in range(10)]
    return payload(nodes, edges, terminals)


def http_smoke():
    print("[http] waiting for api at %s" % API_URL)
    check("api becomes healthy", wait_for_api())

    st, body = http("GET", "/healthz")
    check("GET /healthz", st == 200 and body == {"status": "ok"},
          "status=%s body=%s" % (st, body))

    print("[http] happy path")
    happy = payload(
        ["A", "B", "C", "O"],
        [{"id": "e1", "from": "A", "to": "O", "cost": 1},
         {"id": "e2", "from": "B", "to": "O", "cost": 1},
         {"id": "e3", "from": "C", "to": "O", "cost": 1},
         {"id": "e4", "from": "A", "to": "B", "cost": 10},
         {"id": "e5", "from": "B", "to": "C", "cost": 10},
         {"id": "e6", "from": "A", "to": "C", "cost": 10}],
        ["A", "B", "C"])
    st, body = http("POST", "/api/audit", pl=happy)
    check("happy path status+shape",
          st == 200 and isinstance(body, dict)
          and set(body.keys()) == {"status", "minCost", "edgeIds", "edgeCount",
                                   "terminals", "adjacency"}
          and body["minCost"] == 3 and body["edgeIds"] == ["e1", "e2", "e3"],
          "status=%s body=%s" % (st, body))
    if st == 200:
        subnet_consistency("happy path subnet consistency", happy, body)

    print("[http] tie-break verdict over HTTP")
    tie = payload(["A", "B", "C"],
                  [{"id": "e1", "from": "A", "to": "B", "cost": 1},
                   {"id": "e2", "from": "B", "to": "C", "cost": 1},
                   {"id": "e3", "from": "A", "to": "C", "cost": 2}],
                  ["A", "C"])
    st, body = http("POST", "/api/audit", pl=tie)
    check("tie verdict canonical edge set",
          st == 200 and body.get("edgeIds") == ["e1", "e2"]
          and body.get("minCost") == 2,
          "status=%s body=%s" % (st, body))

    print("[http] infeasible boundary over HTTP")
    expect_http_error("unconnectable -> 422 UNCONNECTABLE", 422, "UNCONNECTABLE",
                      pl=payload(["A", "B", "C", "D"],
                                 [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                                 ["A", "C"]))
    expect_http_error("isolated terminal -> 422", 422, "UNCONNECTABLE",
                      pl=payload(["A", "B", "C"],
                                 [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                                 ["A", "C"]))

    print("[http] validation boundaries over HTTP")
    expect_http_error("dangling terminal -> 400", 400, "UNKNOWN_TERMINAL",
                      pl=payload(["A", "B"],
                                 [{"id": "e1", "from": "A", "to": "B", "cost": 1}],
                                 ["A", "ZZ"]))
    expect_http_error("invalid cost -> 400", 400, "INVALID_COST",
                      pl=payload(["A", "B"],
                                 [{"id": "e1", "from": "A", "to": "B", "cost": 0}],
                                 ["A", "B"]))
    expect_http_error("self loop -> 400", 400, "SELF_LOOP",
                      pl=payload(["A", "B"],
                                 [{"id": "e1", "from": "A", "to": "A", "cost": 1}],
                                 ["A", "B"]))
    expect_http_error("duplicate edge id -> 400", 400, "DUPLICATE_EDGE_ID",
                      pl=payload(["A", "B"],
                                 [{"id": "e1", "from": "A", "to": "B", "cost": 1},
                                  {"id": "e1", "from": "A", "to": "B", "cost": 2}],
                                 ["A", "B"]))
    expect_http_error("61 nodes -> 400", 400, "NODE_COUNT",
                      pl=payload(["n%02d" % i for i in range(61)],
                                 [{"id": "e1", "from": "n00", "to": "n01", "cost": 1}],
                                 ["n00", "n01"]))
    expect_http_error("221 edges -> 400", 400, "EDGE_COUNT",
                      pl=payload(["A", "B"],
                                 [{"id": "e%03d" % i, "from": "A", "to": "B", "cost": 1}
                                  for i in range(221)],
                                 ["A", "B"]))
    expect_http_error("11 terminals -> 400", 400, "TERMINAL_COUNT",
                      pl=payload(["n%02d" % i for i in range(11)],
                                 [{"id": "e%02d" % i, "from": "n%02d" % i,
                                   "to": "n%02d" % (i + 1), "cost": 1}
                                  for i in range(10)],
                                 ["n%02d" % i for i in range(11)]))
    expect_http_error("malformed json -> 400", 400, "MALFORMED_JSON",
                      raw=b"{not json")
    expect_http_error("payload not object -> 400", 400, "PAYLOAD_NOT_OBJECT",
                      pl=[1, 2, 3])
    expect_http_error("missing field -> 400", 400, "MISSING_FIELD",
                      pl={"nodes": ["A", "B"]})
    expect_http_error("oversized body -> 413", 413, "PAYLOAD_TOO_LARGE",
                      raw=b" " * ((1 << 20) + 1))

    print("[http] routing")
    st, body = http("GET", "/api/audit")
    check("GET /api/audit -> 405", st == 405
          and body.get("error", {}).get("code") == "METHOD_NOT_ALLOWED",
          "status=%s" % st)
    st, body = http("GET", "/nope")
    check("unknown path -> 404", st == 404
          and body.get("error", {}).get("code") == "NOT_FOUND",
          "status=%s" % st)

    print("[http] boundary-size valid instance (60/220/10)")
    big = big_case()
    st, body = http("POST", "/api/audit", pl=big)
    path_cost = sum(e["cost"] for e in big["edges"] if e["id"].startswith("p"))
    check("big instance solved",
          st == 200 and body.get("minCost", 10 ** 18) <= path_cost,
          "status=%s minCost=%s path_cost=%s"
          % (st, body.get("minCost") if isinstance(body, dict) else None, path_cost))
    if st == 200:
        subnet_consistency("big instance subnet consistency", big, body)

    print("[http] determinism / no stale or partial results")
    st1, body1 = http("POST", "/api/audit", pl=tie)
    st2, body2 = http("POST", "/api/audit", pl=tie)
    check("same request twice -> identical response",
          st1 == st2 == 200 and body1 == body2)
    st3, err_body = http("POST", "/api/audit",
                         pl=payload(["A", "B"],
                                    [{"id": "e1", "from": "A", "to": "B", "cost": 0}],
                                    ["A", "B"]))
    check("error after success carries no stale subnet",
          st3 == 400 and isinstance(err_body, dict)
          and set(err_body.keys()) == {"error"})
    st4, body4 = http("POST", "/api/audit", pl=tie)
    check("service stateless after error", st4 == 200 and body4 == body1)


# ---------------------------------------------------------------------------

def main():
    solver_tests()
    artifact_checks()
    http_smoke()
    total = RESULTS["passed"] + RESULTS["failed"]
    print("verify: %d/%d checks passed" % (RESULTS["passed"], total))
    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
