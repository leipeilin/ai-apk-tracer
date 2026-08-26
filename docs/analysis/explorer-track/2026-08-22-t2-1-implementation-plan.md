# 任务实施方案：T2.1（规则产物导出）

> **任务编号**：T2.1
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §4.11（决断 2：规则运行时输出产物 JSON，api_surface 读产物——**backend 不 import 规则侧代码**红线）
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T2.1
> - T0.4 交付：`schemas/{binder_bindings,receiver_registrations,webview_js_bridges}.schema.json` + `tests/test_rule_artifacts.py`（sample 驱动校验，字段名对齐规则侧实际产出）
> **状态**：起草
> **前置依赖**：T0.4（已完成）

---

## 1. 任务目标与范围

- **目标**：规则运行时将三样中间数据落盘为 run 目录 JSON 产物——Binder 绑定（`index_reader.binder_components` 的 transactions）、动态 Receiver（`receiver_registration.parse_receiver_registrations` 全量）、WebView bridge（`detector._webview_crypto_match` 调用点）——并注册进 `run_manifest.artifacts`。
- **范围**：
  - 规则侧 `rules/shared/detector.py`：`execute()` 三分支收集 `result["artifacts"]`（stdout 协议 v1.0.0 可选字段，向后兼容）；
  - backend `rule_runner.py`：`_validate_output` 认可 artifacts 白名单键；汇总侧提取 → jsonschema 校验（T0.4 schema）→ 写 `rule-results/{name}.json` → `last_artifacts` 实例属性（对齐 `last_coverage_gaps` 先例）；
  - backend `orchestrator.py`：rule_prescan 后读 `last_artifacts` 注册 manifest artifacts（对齐 decompile artifact 先例）；
  - 测试：detector 层（legacy 路径真实执行 + helper 单元）+ rule_runner 层（`_export_rule_artifacts` 单测）。
- **非范围**：`api_entry_table`（T2.2 读取侧）、`call_tree`（T2.3）、attack_surface（T2.4）——本任务只落"生产侧"；产物消费（api_surface）不在本任务。

## 2. 现状锚点（2026-08-22 复核）

- **规则子进程协议**（detector.execute L503-512）：stdout JSON `{protocol_version, rule_id, status, candidates, [component_diagnostics], [duration_ms]}`；detect.py 薄壳统一 `execute(rule_id, payload)`。
- **三数据源在子进程内部**（backend 不可见）：
  - Binder：execute L393-445——`reader.binder_components(services)` → `binder_batch{name: facts}`，facts 含 `service_class/transactions(完整绑定记录)/gaps`（index_reader L743-755）；
  - Receiver：execute L385-392——`dynamic_scope=reader.dynamic_receiver_scope()`（index 路径）或 legacy files；`parse_receiver_registrations(file, manifest)`（receiver_registration L19）返回完整注册记录（schema 字段齐含 reportable）——现有消费仅取 reportable 子集（`_dynamic_receiver_exposures` L2001-2011）；
  - WebView：`_global_code_rule` L2825-2838 → `_webview_crypto_match`（L2842-2873）命中返回 `{line, text, description, sink_kind}`——**bridge_name（正则 group(1)）现未外带**（T0.4 注释：由 T2.1 导出层推导）。
- **stdout 预算**：`_run_one` 落盘监控 stdout_max_mb（默认 10 MiB，超限 RULE_OUTPUT_LIMIT kill）——artifacts 增大输出必须预算自控。
- **backend 汇总侧**（rule_runner.run_all L101-124）：逐规则写 `rule-results/{rule_id}.json`（完整 result）→ 汇总 candidates；`last_coverage_gaps` 实例属性先例（orchestrator L175 消费）。
- **manifest artifacts 注册先例**：decompile artifact（orchestrator L119-132：type/path/status/诊断字段）。
- **jsonschema 4.23.0** 已可用（T0.4 引入）；测试 import rules 先例（test_rule_output_budget：`sys.path.insert(0, WORKSPACE_ROOT/rules)`）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `rules/shared/detector.py` | 修改 | execute 三分支收集 artifacts + `_webview_crypto_match` 补 bridge_name + 预算截断 helper |
| `backend/app/analysis/rule_runner.py` | 修改 | `_validate_output` 白名单 + `_export_rule_artifacts`（校验/写盘/gap）+ `last_artifacts` |
| `backend/app/analysis/orchestrator.py` | 修改 | rule_prescan 后注册 manifest artifacts（~6 行，对齐既有模式） |
| `backend/tests/test_rule_artifacts.py` | 修改 | 追加 detector 层真实执行测试（legacy 路径）+ helper 单测 |
| `backend/tests/test_rule_runner_artifacts.py`（或并入上文件） | 新增 | `_export_rule_artifacts` 单测 |

### 3.2 规则侧设计（detector.py）

**协议扩展（向后兼容）**：`result["artifacts"]` 可选字典，键白名单 `binder_bindings/receiver_registrations/webview_js_bridges`，值为记录数组。旧 backend 忽略未知字段；新 backend 对无 artifacts 的旧规则输出 `get("artifacts")` 为空跳过。

**三分支收集**（execute 内，各 ~10 行）：
1. **Binder**（L393-445 分支）：`binder_batch` 组装后，经 `_binder_bindings_artifact(binder_batch)` 组装（评审 R-1/R-2 修订）：
   - **service_class 注入**：`(facts.get("service_class") or {}).get("qualified_name") or name`——facts["service_class"] 是 **class 记录 dict**（含 qualified_name/kind 等，index_reader L746/L564-567），非字符串；name（manifest 组件名）兜底（索引 qualified_name 与 manifest 名可能不一致时以索引为准——索引名与代码事实同源）；
   - **resolve_status 推导**（transaction 记录无该字段——index_reader L1164-1192 实证）：
     ```python
     def _binder_resolve_status(transaction: dict) -> str:
         # 实现已唯一绑定 → bound（对齐 v2026-08-16 降级逻辑：dispatch 歧义
         # 不再一票否决）；否则按 implementation gap 判 ambiguous/unresolved。
         if transaction.get("implementation_method_id"):
             return "bound"
         codes = {gap.get("code") for gap in transaction.get("gaps", [])}
         if "BINDER_IMPLEMENTATION_AMBIGUOUS" in codes:
             return "ambiguous"
         return "unresolved"
     ```
2. **Receiver**（L385-392 分支，仅 `DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION`）：对 `dynamic_scope["files"]`（index 路径）或 legacy `files` 逐个调 `parse_receiver_registrations(file, manifest)` **全量**收集（不按 reportable 过滤——非 reportable 记录含 `reportable: false` 字段，审计完整）。
3. **WebView**（L455-458 分支，仅 `WEBVIEW_JS_BRIDGE_EXPOSED`）：**独立 helper `_webview_bridge_artifact_records(code, file)`**（评审 R-7：`finditer` 枚举同文件**全部**桥命中——`_webview_crypto_match` 的单 match 候选行为不动；产物与候选收集解耦）→ `{path, line, text, description, sink_kind, bridge_name}`（bridge_name=正则 group(1)，T0.4 评审 R-5）。

**体积预算自控**（stdout 10 MiB 红线，防 RULE_OUTPUT_LIMIT 击穿规则自身）：
```python
_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024  # 单产物 2 MiB

def _bound_artifact_records(name: str, records: list[dict]) -> tuple[list[dict], list[dict]]:
    """按序估算字节超限截断 + truncation gap（保真实总数供覆盖判断，
    对齐 _summarize_reaching_definitions 的"摘要与输入解耦"先例）。
    字节估算与 detect.py 落盘同口径：json.dumps(..., ensure_ascii=False)
    （评审 R-4：CJK 内容不低估）。"""
```
**截断 gap 承载**（评审 R-4）：`result["artifact_gaps"]`（可选 list，纳入 §3.3 白名单校验）——stdout result 无顶层 gaps 字段（component_diagnostics 仅 Binder 规则），新顶层键必须入协议白名单。

### 3.3 backend 汇总侧设计（rule_runner.py）

```python
RULE_ARTIFACT_KEYS = ("binder_bindings", "receiver_registrations", "webview_js_bridges")
# 键 → 产物文件内的记录键名（schema 顶层键）
_RULE_ARTIFACT_ENTRY_KEY = {"binder_bindings": "bindings", "receiver_registrations": "registrations", "webview_js_bridges": "bridges"}

def _export_rule_artifacts(self, run_dir: Path, result: dict) -> None:
    """提取规则产物：jsonschema 校验（T0.4 schema）→ 写 rule-results/{name}.json
    （{schema_version: "1.0.0", entry_key: records}）→ 记录 last_artifacts。

    校验失败：**per-record 剔除 + per-record gap**（评审 R-3：单条坏记录不毒
    化整产物——`RULE_ARTIFACT_RECORD_INVALID` gap 携带记录索引与错误摘要）；
    全空/结构错误（非 list）才整产物降级 `RULE_ARTIFACT_SCHEMA_INVALID`。
    """

# run_all 循环内（L103 写 rule-results/{rule_id}.json 之后）：
self._export_rule_artifacts(run_dir, result)
# __init__/run_all 开头：self.last_artifacts: list[dict] = []（对齐 last_coverage_gaps）
```

- **schemas 路径**：`rules_root.parent / "schemas"`（WORKSPACE_ROOT/schemas——RuleRunner 已有 rules_root，无新构造参数）；
- **schema 缓存**：模块级懒加载（避免每规则重复读盘）。

**schema 修订子项（评审 R-3，显式走 T0.4 回归流程）**：`receiver_registrations.schema.json` 的 `receiver_class` 改 `["string", "null"]`（`receiver_class_name` 返回 `str | None`——opaque 注册点可空，schema required 保持但允许 null）；同步更新 T0.4 的 sample 校验测试（补可空用例）+ 在 T0.4 文档补记修订。**不在 T2.1 静默改 schema**——评审文档记录修订理由与回归。

**`_validate_output` 扩展**：`artifacts`（键 ⊆ 白名单 且值为 list）与 `artifact_gaps`（值为 list）宽松校验——深度校验由汇总侧 jsonschema 做；协议错误仍走 RULE_PROTOCOL_ERROR。

### 3.4 orchestrator 注册（抽方法可测，评审 R-6）

rule_prescan 段（L175 读 last_coverage_gaps 处）调用 `self._register_rule_artifacts(run_id)`：
```python
def _register_rule_artifacts(self, run_id: str) -> None:
    """规则产物注册进 run_manifest.artifacts（对齐 decompile artifact 先例）。"""
    if not self.rule_runner.last_artifacts:
        return
    run_manifest = self.storage.read_manifest(run_id)
    run_manifest.setdefault("artifacts", []).extend(self.rule_runner.last_artifacts)
    self.storage.write_manifest(run_id, run_manifest)
```
artifact 记录形态：`{"type": "binder_bindings", "path": "rule-results/binder_bindings.json", "record_count": N, "truncated": bool}`。

### 3.5 关键设计决策

**D1：stdout 协议内嵌 artifacts（而非旁路文件/独立进程）**
- 备选 A'（规则子进程直写 rule-results/）：规则进程从 input.json 路径推 run_dir（隐式 `../..`）——路径耦合脆弱，且绕过 backend 的统一写盘/校验/原子性（_write_result 的 tmp+replace 模式）；
- 备选 C（backend 重跑解析）：import rules.shared 违背 §4.11 决断 2 红线；
- **采纳 stdout 内嵌**：协议演进最小（可选字段）、复用既有 stdout 落盘与预算监控、写盘原子性由 backend 统一保证；代价是预算自控（§3.2 截断 helper）。

**D2：Receiver 全量导出（不按 reportable 过滤）**
- T2.2 api_surface 需要完整注册面（非 reportable 记录的 local_broadcast/not_exported 判定正是"可排除"证据）；审计完整性（schema 含 reportable 字段——消费方自行过滤）。

**D3：schema 校验失败降级为 gap（不挂 run）**
- 产物为导出性质（T2.2 消费），schema 失败不应拖垮候选主链；RULE_ARTIFACT_SCHEMA_INVALID gap 可审计。

**D4：体积截断保真实总数（truncation gap 携带 total）**
- 对齐 RULE_OUTPUT_LIMIT 事故的教训（保 total 供覆盖判断）；截断是显式声明而非静默丢失。

**D5：测试分层（index 路径为主，评审 R-5 修订）**
- **A-1 主路径用 build_code_index 构造真实 index**（复用 test_dynamic_receiver_resolution._index 先例：tmp 源码 → `build_code_index` → execute——索引构建秒级，分钟级的是反编译而非索引）；生产 payload 只含 manifest/index/config（orchestrator L166-173），**legacy files 路径生产无数据**——仅作补充用例；
- WebView/Binder：同模式 index 构造或 helper 单元（Binder 组装 helper 喂**真实形态** mock binder_batch——transaction 无 resolve_status，验证推导函数，评审 R-2）；
- rule_runner 层单测 `_export_rule_artifacts`（mock result）；orchestrator 注册段抽方法（§3.4）单测（mock storage + SimpleNamespace(last_artifacts)）；
- 真实反编译端到端（source enabled 全链路）为手工验收项（H-1/H-2）。

### 3.6 测试方案

1. **test_detector_receiver_artifact**（**index 路径真实执行**，评审 R-5）：tmp 源码（registerReceiver 调用，复用 test_dynamic_receiver_resolution 的源码构造模式）→ `build_code_index` → `execute("DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION", {manifest, index})` → `result["artifacts"]["receiver_registrations"]` 全量（含非 reportable）+ jsonschema 校验通过；补充 legacy 路径用例（手工 file dict）作回归；
2. **test_detector_webview_artifact**：源码含**同文件两个** `addJavascriptInterface(new A(), "Bridge1"/"Bridge2")` → artifacts 两条（finditer 全枚举，评审 R-7）+ bridge_name 正确 + schema 通过；
3. **test_binder_bindings_artifact_helper**：mock binder_batch 喂**真实形态 transaction**（无 resolve_status 字段；bound=含 implementation_method_id / ambiguous=含 BINDER_IMPLEMENTATION_AMBIGUOUS gap / unresolved=dispatch 未解析）→ 推导函数三态正确 + service_class 注入（qualified_name 取自 class dict）+ schema 通过；
4. **test_artifact_budget_truncation**：>2 MiB 记录集 → 截断 + `artifact_gaps` 含真实 total（ensure_ascii=False 口径——CJK 内容用例）；
5. **test_rule_runner_exports_artifacts**：RuleRunner + mock result（含 artifacts/artifact_gaps）→ 三文件落盘（schema_version + entry_key 结构）+ last_artifacts 记录（record_count/truncated）；
6. **test_rule_runner_per_record_invalid**：mock result 混入一条缺 required 的记录 → 该记录剔除 + `RULE_ARTIFACT_RECORD_INVALID` gap（含索引与摘要）+ 其余记录正常写盘（评审 R-3 粒度）；
7. **test_register_rule_artifacts**（评审 R-6）：mock storage + `SimpleNamespace(last_artifacts=[...])` → 调 `orchestrator._register_rule_artifacts` 断言 manifest append（mock ScanOrchestrator 实例——构造仅注入 storage/rule_runner 属性）；
8. **回归**：既有 test_rule_artifacts.py（T0.4 sample 校验 + **schema 修订后补 receiver_class 可空用例**）；全量回归。

### 3.7 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性 |
|---|---|---|
| 实施计划 T2.1（三样落盘 + artifacts 注册 + 不 import 规则侧） | §3.2-3.4；数据经 stdout 协议传递（backend 零 import rules） | 一致 |
| §4.11 决断 2（规则运行时输出产物，api_surface 读产物） | 规则运行时（子进程）产出 → backend 汇总侧落盘 → T2.2 读取 | 一致 |
| T0.4 schema（1.0.0 三产物） | 汇总侧 jsonschema 校验 + sample 对齐（字段名即规则侧实际产出） | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| artifacts 增大 stdout 击穿 10 MiB（RULE_OUTPUT_LIMIT kill 规则） | 整规则候选丢失（事故先例） | 预算自控（2 MiB/产物截断 + gap 保 total）；测试 4 实证 | 调大 stdout_max_mb（配置已有） |
| 协议校验过严拒合法产物 | 规则失败 | _validate_output 只做白名单+list 类型宽松校验 | 无 |
| schema 演进（规则侧字段新增）| 校验失败 | schema 宽松（T0.4 draft：additionalProperties 默认 true）+ gap 降级不挂主链 | 更新 schema |
| 规则侧改动回归既有规则行为 | 候选变化 | 收集逻辑只读不改既有流（artifact_sink 可选参数默认 None 不影响其他调用点）；全量回归 | 无 |

## 5. 依赖

- 前置：T0.4（schema + 校验测试）；jsonschema 4.23.0（已装）。
