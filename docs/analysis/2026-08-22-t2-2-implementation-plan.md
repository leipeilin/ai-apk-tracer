# 任务实施方案：T2.2（api_surface：API 入口表）

> **任务编号**：T2.2
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.1（六类入口来源表 + 时序约束）+ §2.0 接入原则（L175：api_surface 读产物、不 import 规则侧代码）
> - 实施计划：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T2.2（rule_prescan 之后、guard 块后 funnel 前新增阶段）
> - T0.5 设计：`docs/analysis/2026-08-22-t0-5-implementation-plan.md`（schema + 三个对齐决策点：entry_method 依赖 code-index / Binder reliability / 静动态 receiver 区分）
> **状态**：起草
> **前置依赖**：T2.1（规则产物落盘 `rule-results/{binder_bindings,receiver_registrations,webview_js_bridges}.json`，已提交 `e4df08b`）

---

## 1. 任务目标与范围

- **目标**：新增 `backend/app/analysis/api_surface.py`——读规则产物 + manifest + code-index 组装 `run_dir/api-surface/api_entry_table.json`（六类入口：act/svc/rcv/prv manifest 组件 + binder/dynrcv/webview 规则产物），在 orchestrator 新增 `api_surface` 阶段（guard 块后、funnel 前），产物注册 `run_manifest.artifacts`。
- **范围**：
  - `api_surface.py`：`build_api_entry_table(run_dir, manifest, settings, index_reader)` + lifecycle 入口方法解析（T0.5 决策点 ①）；
  - `orchestrator.py`：新阶段（`api_surface.enabled` 门禁，默认 false）；
  - 测试：`tests/test_api_surface.py`。
- **非范围**：attack_surface（T2.3，依赖本产物 api_entry_refs）；call_tree（T2.4）；Explorer 消费（T2.5）。

## 2. 现状锚点（2026-08-22 复核）

- **T2.1 产物**：`rule-results/{name}.json`（`{schema_version, bindings|registrations|bridges}`；receiver_registrations 的 `export_status` 含 `legacy_unspecified`、`receiver_class` 可空——T2.1 评审 §6/§6b 修订）；manifest artifacts 已注册（`rule_runner.last_artifacts` → `_register_rule_artifacts`）。
- **manifest 组件字段**（manifest.py，T2.2 评审 R-2 修正 T0.5 锚点）：`kind/name/exported`（**四值字符串**："true"/"false"/"conditional"/"unknown"，恒存在——`effective_exported` L240-255）/`exported_reason/permission/permission_protection/intent_filters{actions,categories,data}/authorities/...`；顶层 `package`。
- **code-index 读取**（backend 侧 `SQLiteCodeIndexReader`，orchestrator 已 import）：`component_files(component_name)`（**三路匹配**：qualified_name OR 简单名 OR path LIKE——R-3：方法须二次过滤 `qualified_class == 组件 FQCN`）；`get_methods_for_files(file_ids)` → 方法记录含 `name/descriptor/qualified_class`——**descriptor 实际形态为 `(params)->return` 点分**（indexer.py:1068 归一；R-1：T0.5 样例的 JVM 形态为大纲草案理想态，无代码路径产出）——entry_method canonical 格式 **`name(params)->return`**（与 symbol_key `f"{qualified_class}#{name}{descriptor}"` 同构，T2.4 call_tree 匹配友好）。
- **ApiSurfaceSettings**（T0.7 已交付，config.py L206-211）：`enabled`（默认 false）/`include_binder`/`include_webview_jsbridge`——receiver 产物恒含（配置面无此开关，动态注册面核心）。
- **guard 块位置**：orchestrator L193-204（`apply_guard_verification` 在 funnel 前）——api_surface 阶段插在其后、funnel 判定前。
- **backend 零 import rules**：api_surface 只读 `rule-results/*.json` 文件 + 自己的 index（方案 §2.0 L175 红线）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `backend/app/analysis/api_surface.py` | 新增 | `build_api_entry_table` + lifecycle 方法解析 + 六类入口转换 |
| `backend/app/analysis/orchestrator.py` | 修改 | `api_surface` 阶段（guard 后 funnel 前，~15 行） |
| `backend/tests/test_api_surface.py` | 新增 | 转换规则/开关/schema/边界测试 |

### 3.2 `api_surface.py` 设计

```python
# 组件生命周期入口方法集合（外部可触发白名单，T0.5 决策点 ①；评审 R-4 补齐：
# provider.call/openAssetFile（RPC 真实利用面）+ service.onHandleIntent（IntentService））
LIFECYCLE_METHODS: dict[str, set[str]] = {
    "activity": {"onCreate", "onStart", "onResume", "onNewIntent", "onActivityResult", "onRestart"},
    "service": {"onCreate", "onStartCommand", "onBind", "onHandleIntent"},
    "receiver": {"onReceive"},
    "provider": {"query", "insert", "update", "delete", "getType", "openFile", "openAssetFile", "call"},
}
_ENTRY_PREFIX = {"activity": "act", "service": "svc", "receiver": "rcv", "provider": "prv"}


def build_api_entry_table(
    run_dir: Path, manifest: dict[str, Any], settings: ApiSurfaceSettings,
    reader: SQLiteCodeIndexReader | None,
) -> dict[str, Any]:
    """组装 API 入口表（全部确定性生成；产物缺失/空数组/解析不到时字段为空不伪造）。

    1. manifest 四类组件入口：每组件解析 lifecycle 方法（component_files →
       get_methods_for_files → name ∈ LIFECYCLE_METHODS 且
       **qualified_class == 组件 FQCN**（评审 R-3：防同简名异包误匹配）），
       **每方法一条 entry**；无方法时一条（entry_method=null）；
    2. binder 入口（include_binder）：读 rule-results/binder_bindings.json——
       reliability=resolve_status；exported 按 service_class 匹配 manifest 组件
       （匹配不到 null）；
    3. dynrcv 入口：读 rule-results/receiver_registrations.json——
       export_status 映射（legacy_unspecified → unknown）；component_name=
       receiver_class 兜底 path 推导类名（receiver_class 可空，T2.1 修订）；
    4. webview 入口（include_webview_jsbridge）：读 rule-results/webview_js_bridges.json——
       component_name=bridge_path → FQCN（**注册调用类**，非桥对象类——
       产物不含桥类型；条件式剥离 "sources/" 前缀 + .java → .，评审 R-7；
       语义显式标注防 T2.3/Agent1 误读）。
    """
```

**entry_id 规则**（schema pattern `^(act|svc|rcv|prv|binder|dynrcv|webview)_[A-Za-z0-9_]+$`）：
- manifest 组件：`{prefix}_{FQCN 点/$→_}_{method_name}`（无方法时省略尾段）；
- binder：`binder_{service_class 点/$→_}_{interface_method 或 f"code{code}"}`
- dynrcv：`dynrcv_{receiver_class 或 path 类名 点/$→_}_{method_name 或 "register"}`
- webview：`webview_{path 类名}_{bridge_name}`；bridge_name 含非法字符时 sanitize（同上）。
- **去重**（评审 R-6）：entry_id 冲突时追加**双下划线序号** `__2/__3`（`_2` 会与合法方法名 `onCreate_2` 撞车）。

**字段映射**：
- `exported`（评审 R-2 四值域）：`"true"→True / "false"→False / "conditional"|"unknown"→None`；附加透传 `exported_reason`（schema additionalProperties 宽松，合法）；
- `permissions`：`[permission] if permission else []`；
- `entry_method`：**`f"{name}{descriptor}"`——descriptor 实际形态 `(params)->return` 点分**（评审 R-1：canonical 格式与 symbol_key 同构；T0.5 样例 JVM 形态为草案理想态）；解析不到 null（不伪造）；
- `reliability`：binder→resolve_status；其余→`not_applicable`（T0.5 评审 R-6）；
- `export_status`（仅 dynrcv，kind="receiver"）：产物值映射（`legacy_unspecified`→`unknown`）。

### 3.3 orchestrator 集成（guard 块后、funnel 前）

```python
# orchestrator._run：guard_verification 之后
if self.settings.api_surface.enabled:
    self._stage(run_id, "api_surface")
    entry_table = build_api_entry_table(
        run_dir, manifest, self.settings.api_surface,
        self._code_index_reader(code_index),  # source_enabled 时打开，否则 None
    )
    table_path = run_dir / "api-surface" / "api_entry_table.json"
    table_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    table_path.write_text(json.dumps(entry_table, ensure_ascii=False, indent=2), "utf-8")
    run_manifest = self.storage.read_manifest(run_id)
    run_manifest.setdefault("artifacts", []).append({
        "type": "api_entry_table",
        "path": "api-surface/api_entry_table.json",
        "entry_count": len(entry_table["api_entries"]),
        "package": entry_table["package"],
    })
    self.storage.write_manifest(run_id, run_manifest)
    self._record_stage(run_id, "api_surface", "completed", {
        "entry_count": len(entry_table["api_entries"]),
        "by_kind": {...},  # 各 kind 计数
    })
```

- reader 生命周期（评审 R-8 对齐先例）：新 helper `_open_index_reader(code_index)`——**空 code_index（source_enabled=False）无 database_path，直接返回 None 短路**（构造会 ValueError）；打开后 try/finally close（evidence 阶段先例 L280-285）；同步 SQLite 查询经 `asyncio.to_thread`（先例 L163-174）；产物写盘 `chmod 0o600`（先例 L1133）。

### 3.4 关键设计决策

**D1：每 lifecycle 方法一条 entry（组件粒度 vs 方法粒度）**
- attack_surface 的 `api_entry_refs` 为数组（T0.5）——方法粒度天然支持多引用；Agent1 起点=方法（call_tree get_entry_points 消费 entry_method/implementation_method_id）；组件粒度会把"入口"退化为"组件声明"，探索价值低。

**D2：dynrcv 的 entry_method 同样走 lifecycle 解析**
- 动态 receiver 的攻击入口是 `receiver_class.onReceive`——与静态 receiver 一致从 index 解析（`component_files(receiver_class)`）；receiver_class 为 null（opaque）时 entry_method=null（不伪造）。

**D3：source_enabled=False 时产物仍生成（manifest-only）**
- api_surface 的 manifest 入口不依赖反编译（组件清单来自 manifest 解析）；规则产物入口自然为空（无 index 时规则产物空）；entry_method=null——Agent1 输入降级但可用（T0.5 风险表"无 entry_method 仍可用"）。

**D4：产物读取容错（文件缺失/空数组/JSON 损坏，评审 R-5 修订）**
- T2.1 写盘条件是 `if artifacts:`（键存在即写）——**空 records 仍写空数组文件**（manifest-only 模式下三文件"存在且空"）；`_load_artifact` 对三种形态统一容错（缺失/空数组/损坏 JSON）返回空记录 + 日志告警；不因产物问题挂阶段（summary 记 available_artifacts）。

**D5：阶段顺序（guard 后 funnel 前）**
- 实施计划 T2.2 行明确"guard 块后、funnel 前"——api_surface 不依赖 guard 结果（独立确定性产物），但 funnel 前完成保证 Explorer（T2.5）与 funnel 扩展可见；guard 块本身在 rule_prescan 后，时序无环。

### 3.5 测试方案（`test_api_surface.py`）

构造模式复用 T2.1（tmp 源码 → `build_code_index` 真实 index + 手写 rule-results 产物文件）：

1. **test_manifest_entries_with_lifecycle_methods**：真实 index（组件类含 onCreate/onNewIntent）→ act 组件 2 条 entry + `entry_method` **实际格式 `onCreate(android.os.Bundle)->void`**（评审 R-1）+ exported 四值域映射（true/false/conditional→None + exported_reason 透传，评审 R-2）+ permissions/intent_filters 透传 + 全表 schema 校验；
2. **test_manifest_entry_without_methods**：组件类不在 index（或 source 关闭 reader=None）→ 1 条 entry + entry_method=null（不伪造）；**同简名异包类不误匹配**（R-3：com.other.MainActivity 不给 com.example.MainActivity 供方法）；
3. **test_binder_entries_from_artifact**：手写 binder_bindings.json（bound/ambiguous/unresolved 三条）→ reliability 映射 + exported 按 service_class 匹配 manifest + transaction_code/implementation_method_id 透传 + include_binder=false 时不生成；
4. **test_dynrcv_entries_from_artifact**：手写 receiver_registrations.json（exported + legacy_unspecified + receiver_class=None 三条）→ export_status 映射（legacy→unknown）+ component_name 兜底 + kind="receiver"（R-9）+ externally_reachable/actions 透传；
5. **test_webview_entries_from_artifact**：手写 webview_js_bridges.json（path 含与不含 sources/ 前缀两形态，R-7）→ component_name=注册类 FQCN（条件式剥离）+ bridge_name/line/path + include_webview_jsbridge=false 不生成；
6. **test_entry_id_pattern_and_dedup**：内部类（`MainActivity$Inner`）→ `$` 转 `_` 合法 pattern；同 entry_id 冲突去重（`__2` 双下划线后缀，R-6）+ **真实 `onCreate_2` 方法并存不撞车**；
7. **test_missing_and_empty_artifacts_tolerated**（R-5）：三文件全缺 + **空数组文件**（`{"schema_version":..., "bindings": []}`）两形态 → manifest 入口仍生成（阶段不挂）；
8. **test_orchestrator_api_surface_stage**（集成）：TestClient + `ApiSurfaceSettings(enabled=True)` + source=false 的 upload → manifest artifacts 含 api_entry_table 条目 + stages 含 api_surface completed + **manifest-only 下 binder/dynrcv/webview entry 为空**（R-9）；默认配置（enabled=false）零行为（既有测试回归即证）。
9. **test_corrupted_artifact_envelope**（R-9）：信封结构错误（schema_version/entry_key 不符）→ 容错空数组（阶段不挂）。

### 3.6 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性 |
|---|---|---|
| §2.1 六类入口来源表 | §3.2 逐一对应（manifest 四类 + 三产物类） | 一致 |
| §2.0 L175（api_surface 读产物、零 import rules） | 只读 rule-results/*.json + backend 自有 index | 一致 |
| §2.1 时序约束（rule_prescan 后） | guard 后 funnel 前（T2.2 任务行） | 一致 |
| T0.5 决策点 ①（entry_method 依赖 code-index） | D1/D2 lifecycle 解析 + null 不伪造 | 一致 |
| T0.5 决策点 ②（Binder reliability） | resolve_status 直映 | 一致 |
| T0.5 决策点 ③（静动态 receiver 区分） | source 字段（manifest vs rule_artifact:...） | 一致 |
| 方案 L499-502 配置 | ApiSurfaceSettings（T0.7 已交付）三开关 | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| entry_id pattern 违规（特殊字符组件/桥名） | schema 校验失败 | sanitize（点/$/非字母数字→_）+ 去重后缀；测试 6 | - |
| 大 APK 组件方法解析慢 | 阶段耗时 | component_files 精确查询（非全库扫描）；阶段无墙钟但复用既有 run 墙钟 | - |
| 产物缺失（规则失败/旧 run） | 入口表不完整 | D4 容错 + summary 记 available_artifacts | manifest-only |
| entry_method 解析偏差（混淆/反编译噪声） | 入口签名失真 | 只信 index 精确类名匹配；解析不到 null（不伪造） | null 降级 |

## 5. 依赖

- 前置：T2.1（产物）；运行时：manifest/code-index（既有）、jsonschema（schema 校验仅测试侧——产物生成侧不校验，测试断言承载）。
