# 任务实施方案：T0.7（配置模型扩展）

> **任务编号**：T0.7
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/2026-08-18-project-optimization-plan.md` §5.5（配置草案）
> - 实施计划：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T0.7
> **状态**：起草
> **前置依赖**：T0.3（`explorer-deep-dive` 协议版本已定）、T0.5（配置语义对齐）

---

## 1. 任务目标与范围

- **目标**：扩展配置模型，新增 `explorer` / `verify` / `api_surface` / `assets` / `batch` / `report` 六个配置段（全部默认关闭或保守默认），与 `default.yaml`、`config.schema.json` 三方同步，并补测试。
- **范围**：
  - `backend/app/config.py`：6 个段类 + `Settings` 注册；
  - `config/default.yaml`：6 段默认值（含注释）；
  - `schemas/config.schema.json`：6 段 schema（含字段描述）；
  - `backend/tests/test_config.py`：默认值/环境变量覆盖/YAML 加载/schema 描述测试。
- **非范围**：各段对应的功能实现（explorer 循环 T2.5、verify T2.11、api_surface T2.2、assets/batch M1、report M3）；`batch.max_ai_calls` 的降级逻辑（M1）。

## 2. 现状锚点

- `config.py`：`Settings(BaseSettings)`（env_prefix=`AI_APK_TRACER_`、嵌套 `__` 分隔、`extra="ignore"`），现有段类 `SourceAnalysis/RuleRuntime/Storage/AI/Funnel/ContextBudget`（L23-198）；`get_settings()`（L231）从 `config/default.yaml` 加载。
- `default.yaml`：L157 结束（现有 6 段，无 report/explorer 等）。
- `config.schema.json`：draft/2020-12、顶层 `required` 列段、每段 object + `additionalProperties: true`。
- `test_config.py`：环境变量嵌套覆盖、legacy 回退、schema 描述校验。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/config.py` | 修改 | 新增 6 段类 + `Settings` 注册 |
| `config/default.yaml` | 修改 | 追加 6 段默认值（含注释） |
| `schemas/config.schema.json` | 修改 | 追加 6 段 schema + 顶层 required 更新 |
| `backend/tests/test_config.py` | 修改 | 追加默认值/环境变量/YAML/schema 测试 |

### 3.2 段类字段设计（`config.py`）

```python
class CallTreeSettings(BaseModel):
    """call_tree on-demand 有界构建预算（方案 §2.2）。"""

    max_depth: int = Field(default=8, ge=1, description="按入口构建调用树的最大深度")
    max_nodes: int = Field(default=500, ge=1, description="按入口构建调用树的最大节点数")


class ExplorerSettings(BaseModel):
    """探索轨（Agent1）开关与循环预算（方案 §2.4/§5.5）。"""

    enabled: bool = Field(default=False, description="是否启用探索轨；默认关闭，开启前须过 M2 三加一验收")
    max_candidates_per_run: int = Field(default=50, ge=1, description="单次扫描最多纳入 funnel 的探索候选数")
    auto_promote: bool = Field(default=False, description="validated 探索候选是否自动升入正式候选池；默认 false（走 L2 复核）")
    allow_external_code: bool = Field(default=True, description="是否允许向模型发送探索检索读回的代码片段")
    prompt_version: str = Field(default="explorer/1.0.0", description="探索协议版本（registry 精确匹配）")
    max_rounds_per_entry: int = Field(default=4, ge=1, description="单入口检索循环轮数上限（评审 §4.3）")
    max_requests_per_entry: int = Field(default=20, ge=1, description="单入口读码请求总数上限（评审 §4.3）")
    max_requests_per_candidate: int = Field(default=4, ge=1, description="单探索候选的 AI 请求上限")
    deep_dive_prompt_version: str = Field(default="explorer-deep-dive/1.0.0", description="partial 候选深挖协议版本（T0.3）")
    call_tree: CallTreeSettings = CallTreeSettings()


class VerifySettings(BaseModel):
    """核验 agent（L2 agent 化演进）开关与预算（方案 §2.7）。"""

    enabled: bool = Field(default=False, description="是否启用核验 agent 替代单轮 L2；默认关闭")
    prompt_version: str = Field(default="verify/1.0.0", description="核验协议版本（registry 精确匹配）")
    max_rounds_per_candidate: int = Field(default=4, ge=1, description="单候选取证循环轮数上限")
    max_requests_per_candidate: int = Field(default=12, ge=1, description="单候选读码请求总数上限")
    fallback_to_single_turn_l2: bool = Field(default=True, description="agent 失败/预算耗尽时回退现有单轮 L2（主链不阻塞）")


class ApiSurfaceSettings(BaseModel):
    """API 入口表生成开关（方案 §2.1）。"""

    enabled: bool = Field(default=False, description="是否生成 api_entry_table 产物；默认关闭")
    include_binder: bool = Field(default=True, description="是否包含 Binder 入口（读 binder_bindings 产物）")
    include_webview_jsbridge: bool = Field(default=True, description="是否包含 WebView bridge 入口（读 webview_js_bridges 产物）")


class AssetsSettings(BaseModel):
    """资产注册与批量扫描（Phase 1）。"""

    enabled: bool = Field(default=False, description="是否启用资产/批量扫描；默认关闭")
    max_concurrent_runs: int = Field(default=2, ge=1, description="资产扫描层并发 run 上限（资产级，区别于 batch 段批内并发；评审 R-4）")
    data_root: Path = Field(default=Path(".ai-apk-tracer/assets"), description="资产元数据与 APK 副本根目录（相对路径以工作区为基准，经 resolved_assets_data_root 解析）")


class BatchSettings(BaseModel):
    """batch 级预算帽（评审 §4.12：Phase 1 即生效）。"""

    max_concurrent_runs: int = Field(default=2, ge=1, description="批内 run 并发上限（batch 级，区别于 assets 段资产扫描并发；评审 R-4）")
    max_ai_calls: int = Field(default=0, ge=0, description="batch 总 AI 预算帽；0=沿用 run 级（默认）；>0 超限降级为仅确定性主链")
    max_wall_seconds: int = Field(default=0, ge=0, description="batch 墙钟上限；0=不限（默认）")


class ReportSettings(BaseModel):
    """漏洞报告与 PoC 骨架（Phase 3）。"""

    allow_executable_poc: bool = Field(default=False, description="是否允许生成可执行 PoC；默认禁止（仅骨架）")
    require_confirmed_finding: bool = Field(default=True, description="仅已确认 finding 可触发报告生成")
```

**Settings 注册**（追加到 L198 后）：`explorer: ExplorerSettings = ExplorerSettings()`、`verify`、`api_surface`、`assets`、`batch`、`report` 同式。

**工作区路径解析（评审 R-2）**：新增 `Settings.resolved_assets_data_root()`，复用 `_resolve_workspace_path`（对齐 `resolved_data_root`），供 T1.x 资产实现使用。

### 3.3 `default.yaml` 追加段（风格对齐现有注释）

```yaml
# 探索轨（Agent1）。全部默认关闭：开启前须通过 M2 三加一验收（2026-08-21-explorer-track-implementation-plan.md §4.3）。
explorer:
  enabled: false
  max_candidates_per_run: 50
  auto_promote: false
  allow_external_code: true
  prompt_version: explorer/1.0.0
  # 检索循环预算（评审 §4.3）：轮数与读码请求上限，跑满产出"部分链+缺口清单"。
  max_rounds_per_entry: 4
  max_requests_per_entry: 20
  max_requests_per_candidate: 4
  # partial 候选深挖协议（评审 §7.1 决断，T0.3）。
  deep_dive_prompt_version: explorer-deep-dive/1.0.0
  call_tree:
    max_depth: 8
    max_nodes: 500

# 核验 agent（L2 agent 化演进，方案 §2.7）。默认关闭；失败/预算耗尽回退单轮 L2。
verify:
  enabled: false
  prompt_version: verify/1.0.0
  max_rounds_per_candidate: 4
  max_requests_per_candidate: 12
  fallback_to_single_turn_l2: true

# API 入口表（方案 §2.1）。默认关闭；生成须在 rule_prescan 之后。
api_surface:
  enabled: false
  include_binder: true
  include_webview_jsbridge: true

# 资产注册与批量扫描（Phase 1）。默认关闭。
assets:
  enabled: false
  max_concurrent_runs: 2
  data_root: .ai-apk-tracer/assets

# batch 级预算帽（评审 §4.12，Phase 1 即生效）。
batch:
  max_concurrent_runs: 2
  # 0=沿用 run 级预算；>0 时超限 run 降级为仅确定性主链并标记 ai_skipped_by_batch_budget。
  max_ai_calls: 0
  # 0=不限。
  max_wall_seconds: 0

# 漏洞报告与 PoC 骨架（Phase 3）。
report:
  # 禁止生成可执行 PoC，仅产出不可执行骨架。
  allow_executable_poc: false
  # 仅已确认 finding 可触发报告生成。
  require_confirmed_finding: true
```

### 3.4 `config.schema.json` 追加段

- 顶层 `required` 追加 6 段名；
- 每段 object + `required`（核心字段）+ `properties`（全字段含 `description`，风格对齐现有段）+ `additionalProperties: true`；
- **Path 类型表达（评审 R-3）**：`assets.data_root` 复用现有 `storage.data_root` 的 `{"type":"string","minLength":1}` 风格（不做 object）。

### 3.5 测试方案（`test_config.py` 追加）

1. **test_explorer_and_related_sections_defaults**：6 段默认值断言（explorer.enabled=False、max_rounds_per_entry=4、call_tree.max_depth=8、verify.fallback_to_single_turn_l2=True、batch.max_ai_calls=0、report.allow_executable_poc=False、api_surface.enabled=False、assets.enabled=False）；
2. **test_nested_env_override_explorer**：`AI_APK_TRACER_EXPLORER__MAX_ROUNDS_PER_ENTRY=6` + `AI_APK_TRACER_VERIFY__ENABLED=true` 覆盖生效；
3. **test_default_yaml_loads_with_new_sections**：`get_settings()` 加载后 6 段存在且默认值正确（回归：现有段值不变）；
4. **test_config_schema_describes_new_sections**：`config.schema.json` 顶层含 6 段名 + 关键字段（explorer.max_rounds_per_entry、verify.fallback_to_single_turn_l2、batch.max_ai_calls 等）描述存在；
5. **test_batch_zero_semantics**：`BatchSettings(max_ai_calls=0, max_wall_seconds=0)` 通过（0=沿用/不限语义）；
6. **test_resolved_assets_data_root**：相对 `data_root` 经 `resolved_assets_data_root()` 解析到工作区（评审 R-2）；
7. **test_config_schema_new_sections_types**：`config.schema.json` 中 `assets.data_root` 为 string 类型（评审 R-3）、顶层 `required` 含 6 段名（评审 R-5）；
8. **test_prompt_version_declared_matches_registry**：`explorer-deep-dive` 默认值匹配 `prompts/registry.yaml` 已注册版本；`explorer/1.0.0` 与 `verify/1.0.0` 未注册属先声明后注册（T2.5/T0.9），**注册前不得运行时解析**（评审 R-1 守卫）。

### 3.6 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| 方案 §5.5 配置草案（explorer/verify/api_surface/assets/batch/report 段） | 字段逐一对应（含 deep_dive_prompt_version、verify.fallback_to_single_turn_l2、batch.max_ai_calls） | 一致 |
| 实施计划 T0.7 参数清单 | `max_rounds_per_entry`/`max_requests_per_entry`/`deep_dive_prompt_version`/`verify.max_rounds_per_candidate`/`verify.fallback_to_single_turn_l2`/`batch.max_ai_calls` 全部覆盖 | 一致 |
| 评审 §4.12（batch 预算帽 Phase 1 生效） | `batch.max_ai_calls` 默认 0=沿用 run 级，语义注释 | 一致 |
| 评审 §4.3（循环预算） | explorer 段含 `max_rounds_per_entry`/`max_requests_per_entry` | 一致 |
| 方案 §2.4/§2.7（协议版本） | `explorer.prompt_version="explorer/1.0.0"` 属**先声明后注册**（prompt 在 T2.5 新增，与 `verify` 归 T0.9 同理）；registry 注册前不得运行时解析（评审 R-1） | 一致性说明 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| config.schema.json 与 default.yaml 漂移 | 配置校验不一致 | 三方（py/schema/yaml）同步修改 + test_config 校验 schema 描述 | 单独回退 schema 段 |
| 环境变量前缀冲突 | 新段覆盖错误 | 沿用既有 `AI_APK_TRACER_<SEGMENT>__<FIELD>` 约定 | 无（既有机制） |
| 默认开启误伤 | 探索轨意外启用 | 全部默认关闭（enabled=False） | 配置回退 |

## 5. 依赖

- 前置：T0.3（deep_dive 协议版本）、T0.5（api_surface 语义）；无功能依赖（仅配置）。
