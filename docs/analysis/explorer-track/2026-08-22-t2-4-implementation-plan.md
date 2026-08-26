# 任务实施方案：T2.4（call_tree on-demand 检索服务）

> **任务编号**：T2.4
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §2.2（七检索能力 + 有界子树 + 可选落盘；服务供 Explorer/核验 Agent/人工，不预生成全量）
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T2.4
> **状态**：起草
> **前置依赖**：无硬前置（T2.2 产物为 `get_entry_points` 的可选增强输入——缺失时降级）

---

## 1. 任务目标与范围

- **目标**：新增 `backend/app/analysis/call_tree.py`——`CallTreeService` 提供七检索能力（`get_entry_points`/`get_method_body`/`get_callees`/`get_callers`/`resolve_invoke_target`/`class_hierarchy`/`search_symbol`）与有界子树构建（`build_bounded_tree`，深度 ≤8/节点 ≤500）+ 可选落盘。
- **范围**：
  - `call_tree.py`：CallTreeService（纯检索服务，无 orchestrator 集成——消费方为 T2.5 explorer / T2.11 核验 agent / 人工）；
  - 测试：`tests/test_call_tree.py`（真实 index 构造调用链）。
- **非范围**：explorer.py 集成（T2.5）；核验 agent（T2.11）；无 orchestrator 阶段（服务非产物）。

## 2. 现状锚点（2026-08-22 复核）

- **index 表结构**（index_store L59-143）：`methods(id TEXT "path#name:line", file_id, name, qualified_class, descriptor, symbol_key, start_line, end_line)`；`classes(id, qualified_name, extends_name, implements_json)`；`call_sites(method_id, resolved_target_id, resolve_status, method_name, method_descriptor, start_line)`；`files(id, path, content)`。
- **method_id 体系统一**：backend indexer 与规则侧同库同 id 格式（`{path}#{name}:{line}`——indexer L327；规则产物 binder `implementation_method_id` 直通）。
- **既有查询能力**（SQLiteCodeIndexReader）：`get_call_relations_for_methods(ids, include_callers, include_callees)`——直接 callers/callees（resolved 边去重排序）+ 歧义 gaps（L585-645）；`component_files`（含 methods）；`get_content(path)`。
- **CallTreeSettings**（T0.7 交付）：`max_depth=8 / max_nodes=500`。
- **api_entry_table**（T2.2）：entry_method `name(params)->return`；binder 条目含 `implementation_method_id`（直通）。
- **guard_verifier 先例**：直接对 reader.db 做 SQL 查询（backend 读自有 index 合法形态）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/call_tree.py` | 新增 | CallTreeService（七能力 + 有界树 + 落盘） |
| `backend/tests/test_call_tree.py` | 新增 | 真实 index 调用链测试 |

### 3.2 `CallTreeService` 设计

```python
class CallTreeService:
    """call_tree on-demand 检索服务（方案 §2.2）。

    复用 analysis.sqlite3 调用边；全部查询有界（LIMIT/预算）；服务供
    Explorer Agent（T2.5）、核验 Agent（T2.11）与人工分析共用——不预生成
    全量调用树。reader 为调用方管理的 SQLiteCodeIndexReader（生命周期
    归调用方）。
    """

    MAX_BODY_LINES = 240       # 方法体行数上限（评审 R-2：对齐 max_lines_per_context
                               # 同"进 prompt 行预算"语义——240 行；截断显式标注）
    MAX_SYMBOL_RESULTS = 50    # 符号解析/检索返回上限

    def __init__(self, run_dir: Path, reader: SQLiteCodeIndexReader,
                 settings: CallTreeSettings) -> None: ...
```

**七能力**（全部返回可序列化 dict/list——Agent prompt 渲染友好）：

1. **`get_entry_points()`**：读 T2.2 `api_entry_table.json`（缺失/损坏容错 → 空列表 + 降级说明）→ 每条目附 `method_id` 解析：
   - manifest/dynrcv 条目：复用 `api_surface.resolve_component_lifecycle_methods`（评审 R-5：提升为公共函数）——组件级入口解析为方法 id 列表；
   - binder 条目：`implementation_method_id` 直通（同 id 体系——**硬约束：method_id 一律 `methods.id` 列值直取、禁止按格式重建**，评审 R-4：前缀/类名形态差异由列值直取消除）；
   - webview 条目：`method_id=None`（注册调用点非方法引用——`bridge_line` 定位）；
   - 返回 `[{entry_id, kind, component_name, source, entry_method, method_id}]`。
2. **`get_method_body(method_id)`**：`methods JOIN files` → `{method_id, name, qualified_class, descriptor, path, start_line, end_line, body, truncated}`——body 为行切片；超 `MAX_BODY_LINES` 截断 + `truncated: true`；方法不存在返回 None。
3. **`get_callees(method_id)` / `get_callers(method_id)`**：`get_call_relations_for_methods`（单方向）→ 目标 method_id 列表 → 批量摘要（name/qualified_class/descriptor/path/line——`methods JOIN files` 单查询）；附歧义 gaps（callees 方向）。
4. **`resolve_invoke_target(expr)`**：`expr` 为方法名（可含 descriptor）→ `methods` 表 `name`（+descriptor）匹配候选列表（LIMIT 50；多候选如实返回——歧义是事实）。
5. **`class_hierarchy(class_name)`**：`classes` 表查询——直接父类（extends_name）+ 实现接口 + 直接子类（`extends_name = class_name` 或简单名匹配）；返回 `{class_name, extends, implements, subclasses}`。
6. **`search_symbol(name)`**：`methods`（name LIKE）+ `classes`（name/qualified_name LIKE）合并结果（各 LIMIT 50）→ `[{kind: "method"|"class", ...摘要}]`。

**有界子树**：

```python
def build_bounded_tree(self, entry_method_id: str,
                       max_depth: int | None = None,
                       max_nodes: int | None = None) -> dict:
    """按入口 BFS 构建有界调用树（方案 §2.2：深度/节点预算）。

    层级批量 get_call_relations（callees 方向）；节点=方法摘要，边=
    {from, to}；**gaps 透传**（评审 R-1：查询已带回的歧义 gap 按节点
    聚合输出——树不"伪完整"，T2.5 缺口清单消费）；达到任一预算即停止
    并记录 truncated{depth_reached, nodes, reason: "depth_limit"|
    "node_limit"}；环安全（visited 集合）；edges 端点恒 ⊆ nodes。
    返回 {entry, nodes: {mid: 摘要}, edges, gaps, truncated}。
    """

def save_tree(self, entry_id: str, tree: dict) -> Path:
    """落盘 run_dir/api-surface/call_tree/{entry_id}.json（chmod 0o600，
    tmp+replace 原子写——对齐 _write_result 模式）。"""
```

### 3.3 关键设计决策

**D1：服务形态（非 orchestrator 阶段、非预生成产物）**
- 方案 §2.2 明确"不固定预生成全量调用树"——服务按需调用（T2.5/T2.11 持有）；`save_tree` 为可选落盘（调用方决定）；无 orchestrator 集成（本任务零 pipeline 改动）。

**D2：method_id 体系统一直通**
- backend/规则侧同库同 id（`path#name:line`）——binder 产物的 `implementation_method_id` 无需转换直接作为检索键（省一层解析与漂移面）。

**D3：查询全部有界**
- body 行数（400）、符号候选（50）、树预算（settings 默认 8/500）——Agent 输入 token 安全；截断显式标注（`truncated`/gaps），不静默。

**D4：歧义如实返回（不择一）**
- `resolve_invoke_target` 多候选全返回；`get_callees` 附歧义 gaps（`SYMBOL_TARGET_AMBIGUOUS`）——Agent1 可见不确定性（低信任输入原则）。

**D5：`get_entry_points` 容错降级**
- api_entry_table 缺失（api_surface.enabled=false 或旧 run）→ 空列表 + `{"degraded": "api_entry_table_missing", "hint": "入口表缺失：请用 search_symbol 定位组件类后以 lifecycle 方法为起点"}`（评审 R-7）——服务仍可用（其余六能力不依赖入口表）。

### 3.4 测试方案（`test_call_tree.py`）

真实 `build_code_index`（源码：`A.entry() → B.helper() → C.sink()` 调用链 + 继承 `Sub extends Base` + 多候选同名方法 `log()`）：

1. **test_get_callees_callers**：A 的 callees 含 B.helper；B 的 callers 含 A.entry（resolved 边双向）+ 摘要字段齐（path/line/descriptor）；
2. **test_get_method_body**：返回方法体文本 + 行号定位 + 非 lifecycle 方法可查；
3. **test_get_method_body_truncation**：超 400 行方法 → body 截断 + truncated=true（构造长方法源码）；
4. **test_resolve_invoke_target**：`log()` 同名多类 → 多候选全返回（歧义如实，D4）；
5. **test_class_hierarchy**：Sub 的 extends=Base；Base 的 subclasses 含 Sub；
6. **test_search_symbol**：按名搜方法与类；
7. **test_build_bounded_tree**：全链树（A→B→C）节点/边正确 + truncated=false；
8. **test_bounded_tree_limits**：`max_nodes=2` → 截断 + reason=node_limit + **edges 端点 ⊆ nodes 断言**（R-6）；深链（构造 5 层）`max_depth=3` → reason=depth_limit；
8b. **test_bounded_tree_cycle**（R-3）：A→B→A 环构造 → 不死循环（visited 实证）+ 树有限输出；
9. **test_save_tree**：落盘文件存在 + 内容回读一致；
10. **test_get_entry_points_with_table**：手写 api_entry_table（binder 条目 implementation_method_id=真实方法 id）→ method_id 直通（D2）+ manifest 条目 lifecycle 解析；
11. **test_get_entry_points_degraded**：无 api_entry_table → 空列表 + degraded 标注（D5）。

### 3.5 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性 |
|---|---|---|
| 方案 §2.2 七能力清单 | §3.2 逐一对应 | 一致 |
| 有界子树（深度/节点/token 预算） | build_bounded_tree（settings 8/500）+ body/符号上限（D3） | 一致 |
| 可选落盘 `api-surface/call_tree/{entry_id}.json` | save_tree（原子写） | 一致 |
| 不预生成全量（服务供三方） | D1（零 pipeline 集成） | 一致 |
| 实施计划 T2.4（复用 analysis.sqlite3 调用边） | get_call_relations_for_methods 复用 | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 大 APK 查询慢 | Agent 循环延迟 | 全部 LIMIT/批量分片（复用既有分片查询）；树预算硬顶 | - |
| extends_name 简单名/FQCN 不一致 | hierarchy 不全 | 返回按实际列值；子类查询双匹配（FQCN OR 简单名） | - |
| body 切片行号偏差（反编译噪声） | 方法体不完整 | start/end_line 直用 index 事实 + 截断标注 | - |
| service 生命周期（reader 归调用方） | 句柄泄漏 | 调用方 try/finally（T2.5 集成模式同 orchestrator） | - |

## 5. 依赖

- 前置：无硬依赖；运行时：index（既有）+ api_entry_table（T2.2 可选输入）。
