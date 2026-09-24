# Steiner Audit Service（低温探测阵列子网审计）

给定 2–60 个唯一 ASCII 节点、1–220 条标识唯一且带正整数成本的无向边（允许平行边，禁止自环与重复边标识），以及 2–10 个必须连通的校准端点，服务计算**总成本最低且连通全部端点的边集**（合法结果可使用非端点作为中继），并在同成本方案中按**升序边标识列表的字典序**给出唯一规范见证：最低成本、规范边集，以及由该边集导出的连通邻接表。

## 算法

后端从零实现，不枚举边集、不调用通用优化器：

1. **终端子集动态规划**（Dreyfus-Wagner）：`dp[mask][v]` 表示连通终端子集 `mask` 且覆盖节点 `v` 的最优值；
2. **节点汇聚合并**：在每个节点处枚举 `mask` 的二部划分并合并部分解；
3. **多源最短路闭包**：每轮合并后以所有节点当前值为源跑 Dijkstra 闭包。

**规范见证**：边成本为正 ⇒ 同成本的两个可行边集互不包含 ⇒ "升序标识列表字典序最小" 等价于 "按标识排名构成的位向量最大"。将每条边赋加大整数权 `cost·2^m − 2^(m−1−rank)`，DP 一次运行同时优化成本与字典序；组合权对边集是单射，最优边集唯一，重构确定性。`verify` 服务另用暴力枚举在小实例上交叉验证该等价性。

## API

### `POST /api/audit`

```json
{
  "nodes": ["A", "B", "C", "O"],
  "edges": [
    {"id": "e1", "from": "A", "to": "O", "cost": 1},
    {"id": "e2", "from": "B", "to": "O", "cost": 1},
    {"id": "e3", "from": "C", "to": "O", "cost": 1}
  ],
  "terminals": ["A", "B", "C"]
}
```

成功 `200`：

```json
{
  "status": "ok",
  "minCost": 3,
  "edgeIds": ["e1", "e2", "e3"],
  "edgeCount": 3,
  "terminals": ["A", "B", "C"],
  "adjacency": {
    "A": [{"node": "O", "edgeId": "e1", "cost": 1}],
    "B": [{"node": "O", "edgeId": "e2", "cost": 1}],
    "C": [{"node": "O", "edgeId": "e3", "cost": 1}],
    "O": [{"node": "A", "edgeId": "e1", "cost": 1},
          {"node": "B", "edgeId": "e2", "cost": 1},
          {"node": "C", "edgeId": "e3", "cost": 1}]
  }
}
```

### 错误（稳定、可定位；不含部分子网或上次结果）

`{"error": {"code": ..., "message": ..., "field": ...}}`

| HTTP | code | 含义 |
|---|---|---|
| 400 | `MALFORMED_JSON` / `PAYLOAD_NOT_OBJECT` / `MISSING_FIELD` / `INVALID_FIELD_TYPE` | 请求体结构非法 |
| 400 | `NODE_COUNT` / `INVALID_NODE_NAME` / `DUPLICATE_NODE` | 节点数量、命名、唯一性 |
| 400 | `EDGE_COUNT` / `INVALID_EDGE` / `INVALID_EDGE_ID` / `DUPLICATE_EDGE_ID` | 边数量、结构、标识 |
| 400 | `UNKNOWN_NODE` / `SELF_LOOP` / `INVALID_COST` | 悬空端点、自环、非法成本 |
| 400 | `TERMINAL_COUNT` / `INVALID_TERMINAL` / `DUPLICATE_TERMINAL` / `UNKNOWN_TERMINAL` | 端点数量与悬空端点 |
| 422 | `UNCONNECTABLE` | 端点跨连通分量，无法连通 |
| 404 / 405 / 413 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` / `PAYLOAD_TOO_LARGE` | 路由与体量 |

### `GET /healthz` → `200 {"status": "ok"}`

## 运行（Docker Compose）

```bash
# 启动 API（宿主机端口可配置，默认 8080）
API_HOST_PORT=9090 docker compose up --build -d api

# 运行一次性验证套件：求解器测试 + 构建产物检查 + HTTP 冒烟
# （覆盖并列裁决与无解边界），以退出码报告结果
docker compose up --build --exit-code-from verify verify
echo "verify exit code: $?"
```

## 本地开发（仅依赖 Python 标准库）

```bash
python3 -m app.server &                       # PORT=8080 可覆盖
API_URL=http://127.0.0.1:8080 python3 -m verify.verify
```
