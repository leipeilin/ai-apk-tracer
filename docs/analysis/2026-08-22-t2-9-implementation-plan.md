# 任务实施方案：T2.9 custom sink 升级闭环

> **任务编号**：T2.9
> **日期**：2026-08-22
> **依据大纲**：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.2（未命中现有 taxonomy 的 sink 标记 custom_sink_proposal，不直接否决，进入 partially_validated 或人工队列）、§2.5（custom sink 升级闭环：人工确认 → 版本化扩展 sink taxonomy → 候选重校验 → validated → 进 golden 集）；实施计划 T2.9 行
> **状态**：已闭合（评审 R-1~R-11 全部采纳，见 `2026-08-22-t2-9-review.md` 处置记录——golden 形状对齐 GoldenCase/匹配器规范化与漂移声明/deep_dive 排除 custom/arity 预留/revalidate reader 重建/集成测试隔离）
> **前置依赖**：T2.6 ✅（三档校验 + custom_sink_proposal 保守 False——D2 边界移交本任务）；T2.7 ✅（归一化 CUSTOM_SINK_PROPOSAL gap 分支预留）
> **移交背景**：T2.6 评审 D2——taxonomy 判定数据源在 rules 侧（`rules/shared/dataflow.py` `classify_operation_taxonomy`），backend → rules 零依赖红线（M2 验收 4.3.5）禁止 import；本任务以**版本化数据文件**为 backend 可读数据源接通判定。

---

## 1. 任务目标与范围

- **目标**：接通 `custom_sink_proposal` 判定（sink taxonomy 版本化文件为数据源）；实现升级闭环工具（人工确认 → taxonomy 版本化扩展 → 候选重校验 → golden 用例生成）。
- **范围（in scope）**：
  1. **sink taxonomy 版本化文件** `rules/sink_taxonomy/versions.yaml`：v1.0.0 种子（从 rules 侧 `classify_operation_taxonomy` 提炼的高置信度 sink 三元组：method + receiver 约束（leaf/prefix/exact 三态匹配器）+ taxonomy）+ 人工扩展条目结构（source=manual + 确认元数据）+ taxonomy_version 版本递增；
  2. **backend 读取与匹配** `backend/app/analysis/sink_taxonomy.py`：加载（容错）+ `sink_matches_taxonomy` 命中判定 + 升级闭环核心逻辑（`promote_custom_sink` / `revalidate_run_candidates` / `generate_golden_case`——纯函数可测）；
  3. **判定接通** `explorer_validation.py`：`custom_sink_proposal` 真实判定；**custom sink 候选状态封顶 partially_validated**（方案 §2.2 原文语义——不否决不进正式 finding，留人工队列）；`validate_explorer_candidates` 加 `taxonomy_entries` 参数（None=禁用，兼容旧行为）；
  4. **CLI 工具** `scripts/promote_custom_sink.py`：薄壳（参数解析 → 调 backend 模块）；
  5. **orchestrator 接线**：`_run_explorer_stage` 载入 taxonomy（配置路径或默认位置）传入校验；
  6. 测试。
- **非范围（out of scope）**：
  - rules 侧 `classify_operation_taxonomy` 的重构/数据化（backend 侧命中判定是**独立保守子集**——同名异义歧义由 receiver 约束缓解，不追求与 rules 判定完全等价）；
  - golden manifest 自动合并（工具生成 case JSON 到指定目录，manifest 登记留人工——golden 集是验收资产，自动化改写风险高）；
  - L1/规则候选的 taxonomy 判定（规则候选的 taxonomy 由 rules 侧运行时自判——backend 不介入）。

## 2. 现状锚点

- **custom_sink_proposal 现状**：`explorer_validation.py:98` 恒 False（T2.6 D2 边界）；`validation` schema（T0.1 冻结）已有该字段；T0.6 映射表 §4 已有 `custom_sink_proposal → CUSTOM_SINK_PROPOSAL` gap 分支（归一化防御保留）。
- **sink 知识提炼（已完成）**：`classify_operation_taxonomy`（rules/shared/dataflow.py:2748-3173）约 90% 敏感分支可数据化为 `(method, receiver 约束, taxonomy)`；taxonomy 值全集 9 敏感 + unknown_effect；**所有 sink 均有 receiver 约束**（无裸方法名 sink）；同名异义（query/write/put*/open/insert）必须 receiver 消歧；不可数据化分支（resolved_target 早退/provider_crud_entry/构造器/参数字面量改判等）v1 不收。
- **判定输入（探索候选侧）**：`chain_proposal.sink`（模型文本，不可靠）+ `hops[-1].to_method_id`（`path#Class.method:line`——回查通过才可信）+ 调用点 `receiver_type`（`call_sites` 表列，`(from_id, line)` 行可查）。
- **golden 格式**：`evaluation/golden/v1/cases/*.json`（id/category/label/rule/component/entry/operation/expected/sources/sinks/tags/provenance）+ manifest（手工管理）。
- **脚本先例**：`scripts/sync-ai-protocol.py`（backend/.venv/bin/python 直调 + 可测函数在 backend 内）。
- **配置先例**：`ExplorerSettings`（config.py:174-194）；yaml 依赖已有（PyYAML 6.0.2）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `rules/sink_taxonomy/versions.yaml` | 新增 | v1.0.0 种子（~40 条高置信度三元组）+ manual 扩展结构 |
| `backend/app/analysis/sink_taxonomy.py` | 新增 | 加载/匹配/升级闭环核心（promote/revalidate/golden 生成——纯函数） |
| `backend/app/analysis/explorer_validation.py` | 修改 | custom_sink_proposal 判定接通 + custom 封顶 partial |
| `backend/app/analysis/orchestrator.py` | 修改 | `_run_explorer_stage` 载入 taxonomy 传入校验 |
| `backend/app/config.py` | 修改 | `ExplorerSettings.custom_sink_taxonomy_path: Path | None = None` |
| `scripts/promote_custom_sink.py` | 新增 | CLI 薄壳 |
| `backend/tests/test_sink_taxonomy.py` | 新增 | 加载/匹配/判定/闭环测试 |

### 3.2 数据结构设计

**versions.yaml**：

```yaml
schema_version: "1.0"
taxonomy_version: "1.0.0"     # manual 扩展时递增（1.0.1 → 1.1.0 由操作者定）
description: >-
  sink taxonomy 版本化文件（T2.9）：backend 侧 custom_sink_proposal 判定的
  独立数据源（零依赖红线——backend 不 import rules）。种子（source=base）
  自 rules/shared/dataflow.py classify_operation_taxonomy 提炼（高置信度
  子集，同名异义靠 receiver 消歧）；人工确认扩展（source=manual）经
  scripts/promote_custom_sink.py 追加。规则侧迭代时 base 条目需人工同步。
entries:
  # ---- 种子（base）：connection_session_control ----
  - method: startService
    receiver_leaves: [Context, ContextWrapper, Activity, Service]
    taxonomy: connection_session_control
    source: base
  - method: bindService
    receiver_leaves: [Context, ContextWrapper, Activity, Service]
    taxonomy: connection_session_control
    source: base
  # ---- receiver 约束三态：leaves（裸类名）/ prefixes（包前缀）/ exact（FQCN）----
  - method: execSQL
    receiver_prefixes: ["android.database.sqlite.", "androidx.sqlite.db."]
    taxonomy: database_mutation
    source: base
  # ---- manual 扩展（升级闭环追加）----
  - method: writeSettings
    receiver_leaves: [SportConfig]
    taxonomy: persistent_state_write
    severity: high
    source: manual
    confirmed_at: "2026-08-22"
    confirmed_by: "analyst"
    provenance: {run_id: "...", candidate_id: "expl_..."}
```

**匹配语义**（保守方向=少命中→多标 custom→多压档，不进正式 finding）：

```python
@dataclass(frozen=True)
class SinkTaxonomyEntry:
    method: str
    taxonomy: str
    receiver_leaves: frozenset[str]      # 裸类名（不含点）
    receiver_prefixes: tuple[str, ...]   # 包前缀
    receiver_exact: frozenset[str]       # FQCN
    source: str                          # base | manual
    severity: str | None = None
    meta: dict = field(default_factory=dict)  # manual 确认元数据

def load_sink_taxonomy(path: Path) -> list[SinkTaxonomyEntry]:
    """容错加载：缺失/损坏/结构异常 → []（判定禁用=保守 False，兼容旧行为）。
    base 与 manual 条目同名冲突时 manual 优先（人工覆盖种子）。"""

def sink_matches_taxonomy(
    method_name: str, receiver_type: str | None,
    entries: Sequence[SinkTaxonomyEntry],
) -> SinkTaxonomyEntry | None:
    """命中判定：method 精确匹配 + receiver 约束三态任一满足。
    receiver_type 为 None/空（调用点无接收者证据）→ 宽松命中
    （约束只在有证据时执行——缺失不算失配）。"""
```

### 3.3 判定接通（explorer_validation.py）

```python
def validate_explorer_candidates(
    candidates, reader, index_path, manifest_facts,
    taxonomy_entries: Sequence[Any] | None = None,   # None=禁用（保守 False）
) -> dict[str, int]:
```

`_validate_one` 内（跳回查后、组装 validation 前）：

```python
custom_sink = False
if taxonomy_entries:
    method_name, receiver_type = _sink_anchor(hops[-1], reader)
    custom_sink = method_name is not None and sink_matches_taxonomy(
        method_name, receiver_type, taxonomy_entries) is None
# 档位规则（方案 §2.2）：custom sink 不否决、封顶 partial（人工队列）
if custom_sink and status == "validated":
    status = "partially_validated"
    notes += "；sink 未命中 taxonomy（custom sink 待人工确认，封顶 partial）"
```

`_sink_anchor(last_hop, reader)`：`to_method_id` 解析方法名（`#` 后 `Class.method:line` 的方法段）；receiver 从 `(from_id, call_site_line)` 的 call_sites 行 `receiver_type` 提取（查不到 → None）；`to_method_id` 无 `#`（畸形）→ (None, None)（判定跳过=不标记 custom——畸形输入不加重）。

**归一化联动**：custom 封顶后 validated 候选必 custom=False——`CUSTOM_SINK_PROPOSAL` gap 分支保留为防御（畸形组合）；`validation_counts` 三档计数含压档结果（custom 压档后 validated 计数如实反映）。

### 3.4 升级闭环核心（sink_taxonomy.py）

```python
def promote_custom_sink(
    taxonomy_path: Path, *, method: str, taxonomy: str,
    receiver_leaves: list[str] | None = None,
    receiver_prefixes: list[str] | None = None,
    receiver_exact: list[str] | None = None,
    severity: str | None = None, operator: str,
    provenance: dict | None = None,
) -> dict:
    """人工确认 → taxonomy 版本化扩展（追加 manual 条目 + 版本递增）。

    幂等保护：同 (method, taxonomy, receiver 约束) 已存在 manual 条目 →
    返回 skipped（不重复追加）；base 条目同名同约束 → 升级为 manual
    （source 改写 + 确认元数据）。返回 {status: appended|upgraded|skipped,
    taxonomy_version, entry}。"""

def revalidate_run_candidates(
    run_dir: Path, taxonomy_path: Path,
) -> dict:
    """候选重校验：读 explorer/candidates.json → 新 taxonomy 重跑
    validate_explorer_candidates（副本，不落盘）→ 升档对比报告。

    返回 {total, status_changes: [{candidate_id, before, after,
    custom_before, custom_after}], counts}。"""

def generate_golden_case(
    candidate: Mapping, entry: SinkTaxonomyEntry, *,
    case_id: str, operator: str,
) -> dict:
    """golden 用例生成（对齐 evaluation/golden/v1/cases 格式）。

    label=positive、rule=EXPLORER_AGENT、sinks 从链尾投影、
    provenance 记录 {kind: explorer-promotion, run_id, candidate_id,
    taxonomy_version, confirmed_by}。manifest 合并留人工（保守）。"""
```

**CLI**（scripts/promote_custom_sink.py）：

```text
用法 A（从 run 候选确认）：
  backend/.venv/bin/python scripts/promote_custom_sink.py \
    --run-dir <dir> --candidate-id <expl_id> --taxonomy <t> \
    [--severity high] --operator <name> [--golden-out <dir>]
  流程：定位候选 → 提取 sink 锚点（回查 run 索引 receiver）→ promote →
  revalidate（升档对比打印）→ 可选 golden 生成。

用法 B（直接确认方法名）：
  ... --method <name> [--receiver-leaf <leaf>]... --taxonomy <t> ...
```

### 3.5 orchestrator 接线

`_run_explorer_stage`：载入一次（`load_sink_taxonomy(配置路径 or WORKSPACE_ROOT/rules/sink_taxonomy/versions.yaml)`——缺失→[] 禁用）→ `validate_explorer_candidates(..., taxonomy_entries=entries)`；stage summary 不变（validation_counts 已含压档计数；custom 计数可从 notes 推导，不扩字段）。

### 3.6 与大纲一致性对照

| 大纲条目（引用） | 本方案实现方式 | 一致性说明 |
|---|---|---|
| §2.2 未命中 taxonomy 标记 custom_sink_proposal，不直接否决 | 真实判定 + notes 标记（不否决候选本身） | 不变 |
| §2.2 进入 partially_validated 或人工队列 | **封顶 partial**（hops 全通过也压 partial——留人工队列走升级闭环） | 不变（本任务明确"封顶"语义——M2 验收 4.3.2"未通过校验 0 条进正式 finding"的扩展） |
| §2.5 人工确认 → 版本化扩展 → 重校验 → validated → golden | promote/revalidate/generate_golden 三函数 + CLI | 不变 |
| §2.3 任何新能力默认关闭（配置显式开启） | 文件缺失即禁用（保守 False 兼容）；配置路径可指空 | 形态适配（数据文件存在性=开关，无独立 enabled 布尔——避免双开关歧义） |
| 零依赖红线（M2 验收 4.3.5） | backend 读数据文件不 import rules；种子一次性提炼（文档注明同步纪律） | 不变 |

### 3.7 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| R-1 种子不全（高置信子集）→ 合法 sink 被误标 custom 压档 | 探索召回下降 | 保守方向=少命中（receiver 证据缺失时宽松命中）；notes 明示原因；升级闭环即修复通道；M2 验收 prec 指标实测 | 删除/清空 versions.yaml → 判定禁用（回到 T2.6 行为） |
| R-2 种子与 rules 判定漂移（rules 迭代不同步） | 判定口径分叉 | 文件头注明同步纪律；base 条目人工同步（规则侧重大迭代时 review） | 同 R-1 |
| R-3 同名异义误命中（receiver 消歧失败） | custom 漏标（候选按普通 sink 对待） | 漏标方向=不压档（候选仍走跳回查+L2 独立裁决——非放行通道，仅少一层保守） | 条目收紧（补 receiver 约束） |
| R-4 promote 误操作污染 taxonomy | 判定口径被污染 | 幂等保护 + manual 条目带完整溯源（operator/run/candidate）；文件 git 版本化可审计回滚 | 删除条目 + 版本回退 |
| R-5 重校验不落盘（报告模式） | 升档结果未持久化 | 设计意图（重校验是决策辅助——人工 review 升档报告后再决定是否重跑 run）；文档明确 | — |

### 3.8 边界决策记录

| 编号 | 决策 | 理由 | 状态 |
|---|---|---|---|
| D1 | custom sink **封顶 partially_validated**（非仅标记） | 方案 §2.2 原文"进入 partially_validated 或人工队列"；不压档则 custom 候选带 gap 进正式 finding——违反升级闭环的"人工确认前置"语义 | 待评审确认 |
| D2 | receiver 证据缺失时**宽松命中**（约束只在有证据时执行） | receiver_type 缺失常见（索引不完整）；严格失配会把大量正常 sink 误标 custom 压档（伤召回）；缺失≠失配 | 待评审确认 |
| D3 | 种子=**高置信度子集**（排除厂商/构造器/参数字面量分支） | 不可数据化分支强行数据化会引入误命中（R-3 方向）；保守子集的漏标风险由 L2 独立裁决兜底 | 按方案执行 |
| D4 | 重校验**报告不落盘** | 升档是 run 产物级决策（应整 run 重跑固化）；工具只输出对比报告 | 待评审确认 |
| D5 | golden manifest 合并留人工 | golden 集是 M4 验收资产；工具自动改 manifest 的风险（错格式/误登记）高于收益 | 按方案执行 |

## 4. 依赖

- 前置：T2.6（校验框架）、T2.7（归一化 gap 分支）、evaluation/golden/v1 格式
- 交接：T4.1（golden 扩展消费 promote 生成的 case）；M2 验收（custom 压档对 prec 的影响实测）
