# 任务实施方案：P-4 复合读码工具 get_call_chain（v3 · 审查后修订）

> **任务编号**：P-4
> **日期**：2026-08-30
> **针对问题**：反编译目录过大 → AI 找源文件/读整个方法体 → 上下文压力与注意力丢失；
> 追一个多层子函数调用需读几个到十几个文件，上下文耗尽或分析不深 → 报告质量不高。
> **状态**：v3（2026-08-30 审查后修订）——审查 17 条（关键 2 / 高 4 / 中 7 / 低 4）**全部采纳**
> （R-2 部分采纳：不接受"在 P-4 内增补 getStringExtra 到 taxonomy"，属 T2.9 升级闭环独立任务），
> 逐条处置见 `2026-08-30-p4-call-chain-tool-review.md` 处置记录。

## 0. 版本演进

| 版本 | 核心特征 | 结局 |
|---|---|---|
| v1 | 沿 resolved 边展开多层 | **推翻**：sink 调用边 100% pending，链到不了 sink |
| v2 | 三类边分类处理：resolved 展开 / pending 作 sink 叶子 / ambiguous 标注 | 方向正确，但**价值被高估 + 实现层接线缺失** |
| **v3** | v2 方向 + 命中基线量化 + 实现层接线补齐（配置可达/taxonomy 数据源/解压容错/预算同源）+ 接口契约 | 本版 |

### v3 修订要点（对应审查 R-x）

| 审查项 | 修订 |
|---|---|
| R-1 可达率仅 8.14%，P4-5 不可判定 | §2.3 写入命中基线；验收改**固定样本断言** |
| R-2 `getStringExtra`/`getString` 不在 taxonomy | §2.2/§3.2 移除，改用实测可命中的 `startService`/`bindService`/`execSQL`/`query`/`delete` |
| R-3 宽松命中 20.58% 误标风险 | §3.2 区分 `★SINK` / `★SINK?`；§4 新增 `call_chain_loose_receiver`（默认 False） |
| R-4 配置到不了实现层 | §4 参数改挂 `CallTreeSettings` |
| R-5 taxonomy 数据源未接线 | §5 构造注入 `sink_entries` |
| R-6 解压失败被吞成 not_found | §3.4 逐边容错 + `arguments_unavailable` 标注 |
| R-7 缺接口契约 | §3.4 补齐签名 / BFS 伪代码 / 返回体契约 |
| R-8 预算上限只改一处 | §4.2 收敛为共享函数，四处同源 |
| R-9/R-10/R-11/R-17 挂点与回归锚点 | §5 修正 schema 文件名、registry 步骤、测试锚点、config.schema.json |
| R-12 复用点选错 | §5 改用 `reader.get_call_sites_for_methods`（批量 + 已解压） |
| R-13 预算二次截断 | §4.2 上限留 1.35 余量 |
| R-14 "零成本回退 v1" 不成立 | §6 改两级回退表述 |
| R-15/R-16 量化与 prompt 落点 | §2.4 体积预估；§5 prompt 精确行号 |

## 1. 目标与范围

新增 `ReadRequest.operation = "get_call_chain"`：一次请求展开多层调用链，按边类型分层供给，
单请求字符预算封顶。深链成本 O(深度) 轮 → O(1) 轮，且**链尾可达 sink**（v1 不可达）。

**价值主张（v3 修正——避免高估）**：
- **主要价值**：减少轮次与重复全方法体带来的上下文膨胀（实测方法体 p50=4 行、p90=22，
  压力主要来自**多轮往返与重复读取**而非单体量）；
- **附带价值**：pending 叶子上的 sink 发现——但受 **taxonomy 覆盖面制约**（82 条目/75 方法名，
  pending 边命中率仅 0.79%），**不构成 sink 召回的主渠道**，主渠道仍是既有候选判定链路。

**非范围**：callers 向上展开；调用图索引改动（pending/ambiguous 不修，如实标注）；
**taxonomy 条目扩充**（属 T2.9 升级闭环——不在 P-4 内增补 `getStringExtra` 等方法名）。

## 2. 现状锚点

### 2.1 代码锚点（v3 修正）

| 锚点 | 位置 | 现状与 v3 修正 |
|---|---|---|
| 读码操作枚举 | `ai_models.py:323` | 4 操作；enum 实际落在 **`schemas/ai_explorer_observation.schema.json`**（`$defs.ReadRequest.properties.operation`），**非** input schema（R-9） |
| 读码分发 | `explorer.py:970` `dispatch_read` | 模块级共用（Explorer + Verify）；`:989` 的 `except ValueError` 会吞掉解压异常（R-6） |
| 上下文截断 | `explorer.py:53` 8192；`:731`（深挖）、`:817`（读码）、`verify_agent.py:39/451/482` | **共四处**，需同源收敛（R-8） |
| 方法体行预算 | `call_tree.py:36` `MAX_BODY_LINES = 240` | L0 复用 |
| **边查询 API** | `index_store.py:569-582` `get_call_sites_for_methods` | **改用此批量 API**（含 resolve_status/receiver_type/method_name/**已解压 arguments**，内部分片 10000）——优于 `get_seed_hops` 裸 SQL（R-12） |
| sink 判定 | `sink_taxonomy.py:127` `sink_matches_taxonomy(method_name, receiver_type, entries)` | 入参恰为 pending 边字段；**:112-124 内部已做 smali 规范化**（`L...;`→`a.b.C`、剥泛型）→ **调用侧禁止重复规范化**（认可项 1） |
| 参数解压 | `index_store.py:27-37` `_load_json` | 仅 `except zlib.error`；损坏 JSON 抛 `JSONDecodeError`（ValueError 子类）→ 被上层吞成 not_found（R-6） |
| 配置挂点 | `config.py:199` `CallTreeSettings`（仅 max_depth/max_nodes） | **改挂此处**——`CallTreeService.__init__` 接收 `CallTreeSettings`（`call_tree.py:39`），orchestrator `:809`/`:1190` 传 `settings.explorer.call_tree`（R-4） |
| taxonomy 加载点 | `orchestrator.py:1231-1251` | 仅在归一化前加载；`call_tree.py` 全文无 taxonomy 引用 → **需接线**（R-5） |

### 2.2 索引实测（run `20260829T145430Z_..._868521fd`，384,607 调用点）

**边分布**：pending 274,163（71.3%）/ resolved 106,183（27.6%）/ ambiguous 4,261（1.1%）
**可展开性**：仅 38.7% 方法（31,254/80,831）拥有 ≥1 resolved 边。

**可命中的 sink（v3 修正：移除不在 taxonomy 的方法名）**：

| sink | 调用边 | pending 占比 | taxonomy 命中数 |
|---|---|---|---|
| `startService` | 29 | 100% | 24/29 ✅ |
| `bindService` | 65 | 100% | 56/65 ✅ |
| `execSQL` | 29 | 100% | 29/29 ✅ |
| `query` | 112 | 93.8% | 29/112 ✅ |
| ~~`getStringExtra`~~ | 243 | 100% | **0（不在 taxonomy）** ❌ |
| ~~`getString`~~ | — | — | **0（不在 taxonomy）** ❌ |

> v2 将 `getStringExtra`/`getString` 列为"典型 sink"是错误的（R-2）：二者在
> `rules/sink_taxonomy/versions.yaml`（82 条目/75 方法名）中不存在，正确实现下必然"未命中"。
> **核心论据不受影响**——`startService`/`bindService`/`execSQL` 均在 taxonomy 中且其调用边
> 100% pending，v1 沿 resolved 边确实到不了它们。

### 2.3 命中基线与可达率（v3 新增——防止验收期误判）

| 指标 | 实测值 |
|---|---|
| pending 边 sink 命中率 | **0.79%**（2,157/274,163） |
| 其中 receiver 缺失的宽松命中 | **20.58%**（444/2,157） |
| 全库拥有 ≥1 直接 pending sink 命中的方法 | **1.57%**（1,273/80,831） |
| 组件 lifecycle 入口（430 个）depth=2/width=3 + sink-first 可达率 | **8.14%**（35/430；直接命中 5.35%） |
| 平均展开节点数 | **2.7** |

> 这三个数字是**基线而非缺陷**：taxonomy 覆盖面（82 条目）是瓶颈，不是工具实现问题。
> 验收以固定样本断言正确性，以基线对比衡量提升（见验收 P4-5/P4-14）。

### 2.4 体积预估（v3 新增 R-15）

实测：方法体行数 p50=4 / p90=22 / p99=94（仅 0.15% 超 240 行）；每方法调用边 p50=3 / p90=15 / 均值 7.01。
→ 典型响应 = L0（p50 仅 4 行）+ ~3.4 条 pending 摘要 + ~2.7 个展开节点 ≈ **1–3K 字符**（远低于 14K 预算）。
**结论**：上下文压力主要来自多轮往返与重复全方法体，本工具收益点成立。
`branch_width=3` 对 p90=15 条边的方法会丢弃约 80% 的边 → "另有 N 条未展开"是常态，
prompt 须教会模型：高扇出方法先 `get_call_chain` 定位方向，再对具体分支用 `get_callees`/`get_method_body` 追深。

## 3. 核心设计

### 3.1 三类边分类处理

| 边类型 | 处理 | 输出 |
|---|---|---|
| **resolved** | 继续展开（BFS 下一层） | 节点 + 方法体（按层降级） |
| **pending** | **终端 sink 候选**，不展开 | 摘要行：`method_name(receiver_type)` + 参数 + 行号 + sink 命中标记 |
| **ambiguous** | 不展开 | gaps：`ambiguous（N 候选）` |

### 3.2 响应形态（v3：示例改用真实可命中 sink + 宽松命中区分）

```
━━ get_call_chain: C0912c.mo3672q（depth=2，7 条边：展开 2 / sink 摘要 2 / 未展开 3）━━
[L0 完整] androidx/sqlite/.../C0912c.java#C0912c.mo3672q:173-176
  <完整方法体（≤240 行，超限显式标注）>
  边:
  ├ :174 checkIfOpen()            [resolved]  → 展开↓
  └ :175 execSQL(sql, args)       [pending]   ★SINK database_mutation
                                              (android.database.sqlite.SQLiteDatabase#execSQL)
[L1 节选] ...#checkIfOpen:180-190（从 :174 进入）
  <前 40 行；超限标注"…(截断，N 行)">
  └ :186 throwIfOpen()            [pending]   无 sink 命中
⚠ gaps: :188 ambiguous(3 候选未展开)；:190 pending 未命中 taxonomy（非已知 sink）
```

**sink 标记两态（R-3）**：
- `★SINK <taxonomy>` —— receiver 证据充分；
- `★SINK? <taxonomy>（receiver 缺失·宽松命中）` —— receiver 为空，依赖宽松命中放行；
  gaps 区统计宽松命中条数。默认配置下（`call_chain_loose_receiver=False`）通用方法名
  （`execute/remove/delete/append/clear/write/put*/apply/commit/newInstance/openConnection`）
  的空 receiver 边**不标** ★SINK。

### 3.3 sink 导向剪枝

1. 每层用 `sink_matches_taxonomy` 判定 pending 边的 sink 命中；
2. **命中 sink 的边优先输出**（不论深度）；
3. resolved 边按**一跳前瞻**（查其 pending 边是否命中 sink）排序，优先展开通往 sink 的分支；
4. 预算优先分配 sink 路径；无关分支留计数（`另有 N 条未展开`）。

### 3.4 接口契约（v3 新增，R-7）

**签名**：

```python
def get_call_chain(
    self,
    method_id: str,
    *,
    depth: int,
    max_hops: int,
    branch_width: int,
    char_budget: int,
    sink_first: bool,
    include_pending: bool,
    loose_receiver: bool,
    sink_entries: Sequence[SinkTaxonomyEntry] | None,
) -> dict[str, Any]:
```

**返回体契约**：

```python
{
  "root": {"method_id": str, "path": str, "lines": [int, int], "body": str},
  "nodes": [{"level": int, "method_id": str, "path": str, "lines": [int, int],
             "body": str, "body_truncated": bool, "enter_line": int | None,
             "edges": [{"kind": "resolved|pending|ambiguous", "line": int,
                        "method_name": str, "receiver_type": str | None,
                        "arguments": list, "arguments_unavailable": bool,
                        "sink": {"taxonomy": str, "loose": bool, "method": str} | None}]}],
  "sink_leaves": [...],                      # 汇总，便于模型与测试检索
  "gaps": [{"line": int, "reason": str}],    # ambiguous / 未命中 taxonomy / 预算截断
  "truncated": bool,
  "rendered": str,                            # §3.2 文本块（供模型阅读）
}
```

> `rendered` 供模型阅读，结构化字段供测试断言——二者必须同源生成（避免漂移）。

**BFS 伪代码**：

```
queue = [(root_method_id, level=0, enter_line=None)]; visited = {root}; hops = 0
while queue and hops < max_hops:
    (mid, level, enter_line) = queue.pop(0)
    if level > depth: gaps += "深度上限"; continue
    sites = reader.get_call_sites_for_methods([mid])      # 批量，一层一次
    edges = classify(sites)                                # resolved/pending/ambiguous
    edges = sort_by_sink_priority(edges, sink_first)       # pending sink 命中优先 + 一跳前瞻
    for edge in edges[:branch_width]:
        逐边 try/except (zlib.error, JSONDecodeError, UnicodeDecodeError, TypeError)
          → 失败: edge.arguments = [], arguments_unavailable = True（节点与其余边照常）
        if edge.kind == "resolved" and edge.target not in visited:
            visited.add(target); queue.append((target, level+1, edge.line)); hops += 1
    if rendered_len >= char_budget: truncated = True; gaps += "预算截断"; break
```

**target 解析**：`method_id` **列值直查**（对齐 `call_tree.py:120-131`），查不到返回 `None`
→ `dispatch_read` 映射 `not_found`；**不做格式重建**（沿用 call_tree 顶部硬约束）。

### 3.5 不变项（v1 已正确，审查认可）

visited 环防护；1 次 `read_requests` 计费；`depth`/`max_hops` 不进协议（服务端封顶防滥用）；
版本化隔离。

## 4. 配置

### 4.1 参数（v3：改挂 `CallTreeSettings`，R-4）

```python
# config.py:199 CallTreeSettings（两轨同源，构造签名零改动）
call_chain_depth: int = 2                # 展开深度上限（le=4）
call_chain_max_hops: int = 8             # 总跳数上限
call_chain_branch_width: int = 3         # 每方法取前 N 条边
call_chain_char_budget: int = 14_000     # 单请求字符预算（渲染字符计）
call_chain_sink_first: bool = True       # sink 导向剪枝
call_chain_include_pending: bool = True  # pending 边作为 sink 叶子
call_chain_loose_receiver: bool = False  # receiver 缺失的宽松命中是否标 ★SINK（默认关闭）
```

> 改挂后需核对 `test_config.py:213` 等既有断言（R-4 提示）。

### 4.2 per-operation 上限（v3：四处同源 + 余量，R-8/R-13）

```python
# explorer.py 模块级；verify_agent 导入复用
def read_result_char_limit(operation: str) -> int:
    return 18_900 if operation == "get_call_chain" else 8 * 1024   # 14_000 × 1.35 转义余量
```

统一调用点：`explorer.py:731`、`explorer.py:817`、`verify_agent.py:451`、`verify_agent.py:482`。
旧 4 操作两轨均仍 8192（**零回归**）。

## 5. 实现挂点

| 层 | 位置 | 改动 |
|---|---|---|
| 协议 | `ai_models.py:323` Literal + **`schemas/ai_explorer_observation.schema.json`** enum（R-9） | 5 操作 |
| schema 同步 | 执行 **`scripts/sync-ai-protocol.py`**（`registry.yaml` 的 `template_sha256`/`schema_sha256` 重算，否则加载抛错）（R-9） | 必做步骤 |
| Prompt | `prompts/explorer/1.1.0/` **`system.md:39`**（operation 约束行）+ **`:61-63`**（读码操作段）+ 新增三类边/★SINK 读法/高扇出引导（R-16）；**需同时提供 `user.md`**（R-9） | |
| 服务 | `call_tree.py` 新增 `get_call_chain`（§3.4 契约） | BFS 用 `reader.get_call_sites_for_methods`（R-12）+ 逐边容错（R-6） |
| **taxonomy 接线** | `CallTreeService.__init__` 增可选 `sink_entries=None`（默认禁用，4 处测试构造零改动）；orchestrator `:809`/`:1190` 注入（R-5） | 新增 |
| 分发 | `explorer.py:970` `dispatch_read` 新增分支 | Explorer + Verify 共用 |
| 预算 | 四处统一调 `read_result_char_limit`（R-8） | |
| 配置 | `config.py:199` 7 参数 + `config/default.yaml` + **`schemas/config.schema.json`**（R-17） | |
| 测试锚点 | `test_explorer_protocol.py:142` `test_read_request_four_operations` → 改名 `test_read_request_operations` + 改 docstring（**明示 P-4 解除 R-2 四操作决断**）+ 补 `get_call_chain` 正向用例（R-11） | 必改 |

**1.0.0 兼容性（R-10，采纳"接受共享"）**：observation schema 被 1.0.0/1.1.0 共用，
enum 扩展会同步放大 1.0.0 允许的操作集——**确认可接受**（服务端实现已存在），
P4-12 断言相应改为"1.0.0 prompt 文本与行为零变化；操作集放大已确认"。

## 6. 风险与回退

| 风险 | 缓解 |
|---|---|
| 低命中率被误判为缺陷 | §2.3 基线写入方案；P4-5 用固定样本而非随机入口 |
| 宽松命中误标 | `★SINK?` 区分 + `call_chain_loose_receiver=False` 默认 + 通用名黑名单 |
| 解压失败整链丢失 | 逐边 try/except + `arguments_unavailable`（R-6） |
| 二次截断 | 上限 18_900（1.35 余量）+ 最坏夹具断言（R-13） |
| 预算挤占 | 单次 14K 占跨轮 40K 的 35%；维持现状（典型响应仅 1–3K，实际占用远低于上限） |
| 索引查询成本 | 一层一次批量查询，可忽略 |

**两级回退（v3 修正 R-14）**：
1. **开关级**：`include_pending=False` + `sink_first=False` → 降级为"纯 resolved 宽度优先"输出形态
   （A/B 对照与应急；**非 v1 全貌**——v1 从未落地）；
2. **代码级**：删除 `call_tree.get_call_chain` + `dispatch_read` 分支 → 真正回到 P-4 之前。
协议层 enum 向后兼容；prompt 层 registry 停用 1.1.0（须先跑 sync 脚本）。

## 7. 验收

核心：三类边处理正确性（含 sink 叶子/宽松命中两态/环/截断/sink 导向剪枝）+ 接口契约断言 +
四处预算同源 + **固定样本的真实索引 sink 可达性** + 探针 A/B（轮次下降、sink 到达率对比基线）。
详见 `2026-08-30-p4-call-chain-tool-acceptance-plan.md`。
