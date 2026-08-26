# 任务实施方案：T2.3（attack_surface：四组件攻击面导出）

> **任务编号**：T2.3
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §2.3（四文件 + 字段清单）+ §2.0（复用现有产物、确定性生成）
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T2.3
> - T0.5 设计：`docs/analysis/explorer-track/2026-08-22-t0-5-implementation-plan.md`（schema + 决策点：receiver 合并/source 三值/空 entry_methods 不伪造）
> **状态**：起草
> **前置依赖**：T2.2（api_entry_table 产物——`api_entry_refs` 引用 entry_id）

---

## 1. 任务目标与范围

- **目标**：新增 `backend/app/analysis/attack_surface.py`——产出四组件攻击面文件 `run_dir/attack_surface/{activity,service,provider,receiver}.json`（组件名/导出/权限/入口方法/intent-action-uri/敏感能力/关联 API 入口），receiver 合并静态+动态注册，注册 manifest artifacts。
- **范围**：
  - `attack_surface.py`：`build_attack_surfaces(run_dir, manifest, candidates) -> dict[str, dict]`（四文件 payload）；
  - `orchestrator.py`：在 api_surface 阶段后生成（同 `api_surface.enabled` 门禁块）+ 四文件落盘注册；
  - 测试：`tests/test_attack_surface.py`。
- **非范围**：call_tree（T2.4）；Explorer 消费（T2.5）；webview/密码学独立攻击面文件（方案 §2.3 四组件文件；webview bridge 为代码级入口，由 api_entry_table 承载）。

## 2. 现状锚点（2026-08-22 复核）

- **T2.2 产物**：`api-surface/api_entry_table.json`（六类入口；`entry_id` 稳定 sanitize + `__2` 去重；manifest 组件条目含 `entry_method`）——attack_surface 的 `api_entry_refs`/`entry_methods` 数据源。
- **attack_surface schema**（T0.5 交付）：`components[]` required `kind/name/exported`（**bool 非 nullable**）；`entry_methods` 数组（允许空——"code-index 未解析时允许空数组，确定性原则不伪造"）；`source` 三值（manifest/dynamic/manifest+dynamic，`dynamic` ≡ `rule_artifact:receiver_registrations`——T0.5 评审 R-2 交叉引用）。
- **manifest 组件**：四值域 exported（T2.2 评审 R-2 已核）。
- **规则候选**（rule_prescan 输出）：`rule_id`/`component`（kind）/`component_name`（清单组件名或全局规则 `dynamic:<path>`）——敏感能力聚合键。
- **receiver_registrations 产物**（T2.1）：`receiver_class`（可空）/`export_status`/`actions`/`externally_reachable`。
- **配置**：无独立 attack_surface 段（方案配置样例 L499-502 仅 api_surface）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/attack_surface.py` | 新增 | `build_attack_surfaces`（四文件 payload 组装） |
| `backend/app/analysis/orchestrator.py` | 修改 | api_surface 阶段后生成 + 落盘注册（~20 行） |
| `backend/tests/test_attack_surface.py` | 新增 | 组装/合并/聚合/映射测试 |

### 3.2 `attack_surface.py` 设计

```python
_KIND_FILES = {"activity": "activity", "service": "service",
               "provider": "provider", "receiver": "receiver"}


def build_attack_surfaces(
    run_dir: Path, manifest: dict[str, Any], candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """组装四组件攻击面（全部确定性生成）。

    数据流：
    1. manifest 组件 → 四类条目（activity/service/provider 纯静态；
       receiver 与动态注册合并——receiver_class 匹配静态名 →
       source="manifest+dynamic"，纯动态 → source="dynamic"）；
    2. api_entry_table（读 T2.2 产物文件）→ api_entry_refs（组件全部
       entry_id：manifest/binder/dynrcv 类）+ entry_methods（manifest 条目
       entry_method 非空集合）；
    3. candidates → sensitive_capabilities（component_name 精确匹配组件名
       的 rule_id 集合——全局规则 dynamic:<path> 不入组件能力，不伪造归属）。
    返回 {文件名: payload}（四文件 payload，调用方落盘）。
    """
```

**exported bool 映射**（schema required bool，D2，评审 R-1 修订统一保守方向）：
- manifest 组件：`"true"→True / "false"→False / "conditional"|"unknown"→True`——**攻击面保守高估**：conditional 语义为 targetSdk≥31 默认不导出但旧版本（≤30）导出 → 攻击面覆盖旧设备；unknown 保守视为可达；`exported_reason` 始终透传（追溯真实判定）。
- 动态 receiver：`externally_reachable is True→True / is False→False / None→True`（None=未知——**保守统一高估**，与 manifest 侧同向，评审 R-1）；附加透传 `export_status`/`externally_reachable` 保审计。
- 合并条目（manifest+dynamic）：`exported = 静态 exported OR 动态判定`（任一可达即可达）；`exported_reason` 组合标注 `"static:{静态 reason};dynamic:{动态判定}"`（评审 R-5——防 True 来源为动态时静态 reason 误导回溯）。

**动态 receiver 条目字段**：
- `name=receiver_class`（可空时 path 推导类名兜底，同 T2.2 dynrcv 逻辑）；`actions=registration actions`；`api_entry_refs=该 receiver 的 dynrcv entry_id`。

**entry_methods**（评审 R-3 修订）：manifest + dynrcv 条目的 `entry_method` **去重集合**（纯动态 receiver 的 onReceive 解析结果可见）；binder 的 interface_method/implementation_method_id **不入 methods 仅入 refs**（非 lifecycle、格式异构——显式声明）。

**provider 附加字段**（评审 R-6）：`read_permission/write_permission` 透传（schema additionalProperties 允许——主 permission 表达组件级保护，读写粒度保审计）。

**binder 无挂靠取舍**（评审 R-6 声明）：service_class 不在 manifest 时 binder 入口无攻击面组件可挂——其可见性由 api_entry_table 承载（kind=binder 条目）；attack_surface 为组件视角，不伪造挂靠。

### 3.3 orchestrator 集成（评审 R-7：封装方法 + `_stage` 两段式，对齐 T2.2 先例）

`_generate_api_entry_table` 之后同 enabled 块内调用 `self._generate_attack_surfaces(run_id, run_dir, manifest, candidates)`：

```python
async def _generate_attack_surfaces(self, run_id, run_dir, manifest, candidates) -> None:
    """生成四组件攻击面并注册 manifest artifacts（T2.3，复用 api_surface.enabled 门禁）。"""
    self._stage(run_id, "attack_surface")
    surfaces = await asyncio.to_thread(build_attack_surfaces, run_dir, manifest, candidates)
    # 四文件落盘（chmod 0o600）+ artifacts 逐文件注册
    # {type: "attack_surface", component_kind, path, component_count}
    # _record_stage(run_id, "attack_surface", "completed", {"by_kind": {...}})
```

### 3.4 关键设计决策

**D1：门禁复用 `api_surface.enabled`（不新增配置段）**
- attack_surface 的 `api_entry_refs` 依赖 api_entry_table（无 api_surface 无 refs）；方案配置样例无独立段；两者同属探索轨确定性输入层——同开同关。记录：若后续需要独立开关（如只产入口表不产攻击面），加 `api_surface.include_attack_surface` 开关（Phase 2 无此需求）。

**D2：exported 保守高估（conditional/unknown → True）**
- 攻击面语义=宁可高估（探索轨多查一个组件的代价远小于漏查）；exported_reason + 附加 `export_status`（动态注册透传）保审计可回溯。区别于 api_entry_table 的 nullable exported（入口表=事实记录，攻击面=保守边界）。

**D3：敏感能力精确匹配（全局规则不入组件）**
- `candidate["component_name"]` 精确等于清单组件名才聚合（组件级规则族）；全局规则（`dynamic:<path>`）的能力归属需文件→组件推断（超确定性范围）——不伪造；全局规则事实由 candidates/api_entry_table 承载。
- **勘误声明（评审 R-2，比照 T2.2 评审 §6 先例）**：T0.5 已交付样例中 activity 条目 `sensitive_capabilities` 含 `WEBVIEW_FILE_ACCESS_ENABLED` 为**草案理想态**——该规则属 GLOBAL_CODE_RULES（component_name=dynamic:path），按本决策不进组件能力；全局能力对 Agent1 的可见性缺口移交 T2.5 输入设计（见评审 §6 遗留项）。
- auxiliary 候选（informational 启发式）**含入**聚合（能力=全部规则命中，rule_id 自带语义可辨——评审 R-7 声明）。

**D4：receiver 合并以类名为键**
- 静态 receiver 与动态注册 `receiver_class` 同名 → 合并条目（source=manifest+dynamic，actions/refs 并集）；仅静态 → manifest；仅动态 → dynamic。

**D5：四文件恒生成（含空 components 数组）**
- 无某类组件（如无 provider）→ 对应文件 `components: []`（schema 合法；Agent1 输入面稳定——四文件恒存在）。

### 3.5 测试方案（`test_attack_surface.py`，评审 R-4：entry_table 夹具用真实生成器）

构造模式：手写 rule-results 产物（binder/receiver_registrations）+ **真实 `build_api_entry_table` 生成** entry_table（消手写漂移；entry_method 复用 `name(params)->return` 实际格式）+ manifest fixture + 手工 candidates 列表：

1. **test_activity_surface_fields**：manifest activity（exported=conditional）→ exported=True（保守高估 D2）+ exported_reason 透传 + permission/protection + entry_methods（来自 entry_table 的 manifest 条目）+ api_entry_refs + intent_filters；schema 校验；
2. **test_sensitive_capabilities_aggregation**：candidates 含组件级命中（component_name=组件名 × 2 规则）+ 全局规则（dynamic:path）→ 组件能力=2 个 rule_id（全局不入，D3）；
3. **test_receiver_merge_manifest_and_dynamic**：静态 receiver + 动态注册同名类 → source=manifest+dynamic + actions 并集 + refs 含静态与 dynrcv entry + exported OR 合并（静态 false + externally_reachable True → True）+ **reason 组合标注**（R-5）+ 动态 reachable=None 分支（**静态 false + None → True**，R-1）；
4. **test_dynamic_only_receiver**：仅动态注册 → source=dynamic + exported 三分支（True/False/**None→True**，R-1）+ 无静态条目；
5. **test_provider_and_service_surfaces**：provider（authorities + **read/write_permission 透传** R-6）/service（权限）字段透传；
6. **test_empty_kind_file**：无 provider 组件 → provider.json components=[] 合法（D5）；
7. **test_missing_entry_table_tolerated**：api_entry_table 缺失 → refs/entry_methods 空（容错）；
8. **test_orchestrator_attack_surface_stage**（集成）：`ApiSurfaceSettings(enabled=True)` + upload → attack_surface/ 四文件存在 + artifacts 注册 4 条 + stage completed；默认关闭零行为。

### 3.6 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性 |
|---|---|---|
| 方案 §2.3 四文件 + 字段清单 | §3.2 逐字段（组件名/导出/权限/入口方法/intent-action-uri/敏感能力/关联入口） | 一致 |
| 方案 §2.3 receiver 合并静态+动态 | D4（类名键合并，source 三值） | 一致 |
| T0.5 决策点（空 entry_methods 不伪造） | entry_methods 来自 entry_table 精确集合 | 一致 |
| T0.5 评审 R-2（dynamic ≡ rule_artifact:*） | source 值直用 schema 枚举 | 一致 |
| 敏感能力从规则候选聚合（T0.5 §3.4） | D3 精确匹配 | 一致（全局规则不伪造归属） |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| exported 保守高估误导 | 攻击面虚大 | exported_reason 透传可回溯真值 | 改 nullable（需 schema 修订） |
| 组件级规则能力归属歧义 | 能力缺失 | 全局规则事实在 candidates/api_entry_table 可查 | 文件→组件推断（后续确定性增强） |
| entry_table 读取失败 | refs 空 | 容错 + 阶段仍 completed | - |

## 5. 依赖

- 前置：T2.2（api_entry_table 产物文件）；运行时：manifest + candidates（orchestrator 流程内变量）。
