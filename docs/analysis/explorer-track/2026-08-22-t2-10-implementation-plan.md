# 任务实施方案：T2.10 探索产物注册与人工队列审计视图

> **任务编号**：T2.10
> **日期**：2026-08-22
> **依据大纲**：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §2.0/§5.4（unverified/partial → 人工队列，按置信度/引用完整度排序；partial 深挖或人工高优）、§6 风险对策（`unverified` 候选被埋没 → 人工队列排序展示）；M2 验收 §4.3.3（人工队列）
> **状态**：已闭合（评审 R-1~R-9 全部采纳，见 `2026-08-22-t2-10-review.md`——排序改置信度主键+deep_dive 证据次键/deep_dive 投影字段修正/门禁如实 tsc+build/usePolling 轮询/call_tree 落盘按实情排除/键名 entries 统一）
> **前置依赖**：T2.5b~T2.9 ✅（explorer/candidates.json 含全三档 + validation + deep_dive；`explorer_candidates` artifact 已注册（orchestrator.py:1192-1196））

---

## 1. 任务目标与范围

- **目标**：探索产物完整注册（observations 补注册）+ 人工队列 API 与前端审计视图（partial/unverified 按 deep_dive 证据排序展示，含三档校验与 deep_dive 结果）。
- **范围（in scope）**：
  1. **队列构建纯函数** `backend/app/analysis/explorer_queue.py`：`build_explorer_queue(candidates) -> {queue, counts}`——投影（脱全量防响应膨胀）+ 排序（deep_dive 证据数 ↓ → 置信度 ↓ → 跳回查完整度 ↓）+ 汇总计数；
  2. **API 端点** `GET /api/runs/{run_id}/explorer/candidates`（routes.py）：run 校验 404 + 读 candidates.json（容错空态）+ build_explorer_queue；
  3. **observations artifact 补注册**（orchestrator `_run_explorer_stage`）：type=`explorer_observations`（文件存在时）；
  4. **前端**：types.ts/api.ts 扩展 + `ExplorerQueuePanel.tsx`（三档徽章/链摘要/回查进度/deep_dive 证据计数/notes；空态）+ RunDetailPage 挂载；
  5. 测试：backend 单测（队列构建/端点）；前端 tsc+build+eslint 门禁（无单测基建——验收以类型与构建为准）。
- **非范围（out of scope）**：
  - 人工处置动作（确认/驳回/promote 触发——promote 已有 CLI（T2.9）；前端动作按钮属后续任务）；
  - validated 候选的对照视图（validated 已进主链 findings——队列视图以 partial/unverified 为主体，validated 仅计数展示）；
  - 分页/过滤参数（候选量级 ≤ max_candidates_per_run=50——全量返回）。

## 2. 现状锚点

- **数据源**：`run_dir/explorer/candidates.json`（list[ExplorerCandidate]——含 validation（三档/failed_hop_indices/notes）与 deep_dive（status/evidence_refs/resolved_facts/remaining_gaps/rounds）字段）。
- **artifact 现状**：`explorer_candidates` 已注册（T2.5b）；`explorer/observations.json`（轮审计）**未注册**。
- **API 先例**：routes.py `GET /api/runs/{run_id}/findings`（157-162）——request.app.state.storage 模式；404 语义（NotFoundError → handler）。
- **前端先例**：api.ts 类型化 request 客户端；FindingsPanel/StageTimeline 面板模式（Badge/StateView 组件）；RunDetailPage 挂载点（FindingsPanel 之后）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/explorer_queue.py` | 新增 | `build_explorer_queue`（投影+排序+计数纯函数） |
| `backend/app/api/routes.py` | 修改 | `GET /api/runs/{run_id}/explorer/candidates` |
| `backend/app/analysis/orchestrator.py` | 修改 | observations artifact 补注册 |
| `frontend/src/lib/types.ts` | 修改 | ExplorerQueueCandidate/ExplorerQueueResponse |
| `frontend/src/lib/api.ts` | 修改 | `getExplorerCandidates` |
| `frontend/src/features/runs/ExplorerQueuePanel.tsx` | 新增 | 人工队列面板 |
| `frontend/src/features/runs/RunDetailPage.tsx` | 修改 | 挂载面板 |
| `backend/tests/test_explorer_queue.py` | 新增 | 队列构建 + API 端点测试 |

### 3.2 队列条目形状与排序

```python
# explorer_queue.py
def build_explorer_queue(candidates: Sequence[Mapping]) -> dict[str, Any]:
    """探索候选 → 人工队列（投影脱全量 + 服务端预排序 + 计数）。

    排序（方案 §2.0"按置信度/引用完整度"+ 任务要求"按 deep_dive 证据"）：
    ① deep_dive 证据数（回查通过的 evidence_refs 数）降序；
    ② 置信度（high=3 > medium=2 > low=1）降序；
    ③ 跳回查完整度（verified_hop_count / len(hops)）降序；
    ④ candidate_id 稳定序。
    """

    条目 = {
        "candidate_id", "component": {kind, name, entry_method},
        "chain": {source, sink, hop_count},
        "validation": {status, verified_hop_count, hop_count_total,
                       failed_hop_indices, blocked_by_guard,
                       custom_sink_proposal, notes},
        "deep_dive": None | {status, evidence_count, confirmed_fact_count,
                             remaining_gap_count, consistency_downgraded,
                             requests_used},
        "confidence": chain_proposal.confidence,
        "sort_keys": {deep_dive_evidence, confidence_rank, hop_ratio},
    }
    counts = {validated, partially_validated, unverified, pending, total,
              deep_dive_completed}
```

### 3.3 API 端点

```python
@router.get("/api/runs/{run_id}/explorer/candidates")
def explorer_candidates(run_id: str, request: Request) -> dict:
    # run 存在校验（404）→ storage.run_dir 读 explorer/candidates.json
    # （缺失/损坏 → build_explorer_queue([]) 空态）→ build_explorer_queue
```

### 3.4 前端面板

- `ExplorerQueuePanel`：props `runId`；拉取 + 三档计数徽章 + 行列表（状态徽章色：partial=amber/unverified=gray/validated=green 折叠计数）；行内容：组件名、`source → sink` 链摘要、跳回查 `x/y`、deep_dive 徽章（`证据 n`/未深挖）、custom_sink 标记、notes（title tooltip）；服务端序展示；空态文案"探索轨未启用或无候选"。
- 挂载 RunDetailPage（FindingsPanel 后）。

### 3.5 与大纲一致性对照

| 大纲条目（引用） | 本方案实现方式 | 一致性说明 |
|---|---|---|
| §2.0/§5.4 unverified/partial 人工队列（按置信度/引用完整度排序） | 队列排序键（置信度 rank + hop_ratio） | 不变 |
| 任务要求"按 deep_dive 证据排序" | 排序第一键（deep_dive 证据数——深挖产出可回查证据越多越靠前） | 组合排序（deep_dive 证据 → 置信度 → 完整度） |
| §6 unverified 被埋没对策 | 队列视图 + 徽章计数 | 不变 |
| 任务行"产物注册 artifacts" | explorer_candidates 已有；observations 补注册 | 补齐 |
| validated 不进队列主列表（§5.4——已进主链） | 计数徽章展示（对照），列表主体 partial/unverified/pending | 不变 |

### 3.6 风险与边界决策

| 编号 | 决策/风险 | 说明 |
|---|---|---|
| D1 | 列表主体=partial/unverified/pending（validated 仅计数） | validated 已进主链 findings——队列重复展示徒增噪音；计数保留对照 |
| D2 | 服务端预排序（非前端排） | 排序规则是方案语义（可审计可测）；前端只渲染 |
| R-1 响应体积 | 全量候选（≤50）投影脱全量（不含 hops 全文/rounds 审计——审计走文件下载） | 可控 |
| R-2 run 产物缺失 | 空态 200（非 404）——探索轨未开启的 run 是常态 | 容错 |
| R-3 前端无单测 | tsc+build+eslint 门禁（项目现状） | 验收以门禁为准 |

## 4. 依赖

- 前置：T2.5b~T2.9（candidates.json 数据形状）；`check-all.sh` frontend 门禁
