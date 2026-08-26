# 任务实施方案：T0.4（规则产物 Schema：binder_bindings / receiver_registrations / webview_js_bridges）

> **任务编号**：T0.4
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan.md` §2.1（规则产物传递）、§2.0（backend→rules 零依赖）
> - 评审：`docs/analysis/project-optimization/2026-08-18-project-optimization-plan-review.md` §7.2（决断：方案 B 规则产物 JSON 传递）
> - 实施计划：`docs/analysis/explorer-track/2026-08-21-explorer-track-implementation-plan.md` T0.4
> **状态**：起草
> **前置依赖**：无（字段设计基于 2026-08-22 对规则侧三模块输出形态的调研）

---

## 1. 任务目标与范围

- **目标**：定义三个**规则产物确定性 JSON Schema**（`binder_bindings` / `receiver_registrations` / `webview_js_bridges`），供 T2.1 规则侧导出、T2.2 `api_surface` 读取，落实评审 §7.2 方案 B（backend 不 import 规则侧代码，只读产物）。
- **范围**：
  - 三个手写宽松 schema（`schemas/` 下，风格对齐 `candidate.schema.json`：draft/2020-12、`additionalProperties: true`）；
  - 新测试文件 `backend/tests/test_rule_artifacts.py`（jsonschema 校验样例/反例）；
  - `jsonschema` 加入 `pyproject.toml` test 依赖（当前 venv 未安装）。
- **非范围**：
  - 规则侧导出实现（T2.1，规则运行时序列化）；
  - `api_surface.py` 读取消费（T2.2）；
  - `resolve_status` 的推导逻辑实现（T2.1 导出时由 Binder gap 推导，本任务仅定义枚举语义）。

## 2. 现状锚点（2026-08-22 调研结论）

- `rules/shared/index_reader.py`：`binder_components` L536 → `_bind_binder_transactions` L758 → `_binder_transactions` L1065；transaction dict 含 `code/case_token/interface_method/descriptor/implementation_class/implementation_method_id/implementation_line/implementation_path/gaps`（gap code：`BINDER_IMPLEMENTATION_AMBIGUOUS/UNRESOLVED`、`BINDER_DISPATCH_TARGET_*`、`BINDER_RETURN_TYPE_*`，带 `critical`）。
- `rules/shared/receiver_registration.py`：`parse_receiver_registrations` L19 → `_parse_call` L180；记录字段（L353-389）：`receiver_class/actions/unresolved_action_expressions/filter_expression/flag_value/flag_status/export_status/externally_reachable(可 null)/permission/permission_status/local_broadcast/platform_branch/reportable/call/path/line/method_id/coverage_gaps`。
- `rules/shared/detector.py`：`_webview_crypto_match` L2842 `WEBVIEW_JS_BRIDGE_EXPOSED` 分支 L2860-2873 产出 `{line, text(前120字符), description, sink_kind}`；**无桥对象类型/注解解析**。
- 风格参考：`schemas/candidate.schema.json`（draft/2020-12、`additionalProperties: true`、required + properties 约束）。
- 依赖现状：venv 无 `jsonschema`；`pyproject.toml` test 组仅 `pytest`。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `schemas/binder_bindings.schema.json` | 新增 | Binder 绑定产物 schema |
| `schemas/receiver_registrations.schema.json` | 新增 | 动态 Receiver 产物 schema |
| `schemas/webview_js_bridges.schema.json` | 新增 | WebView JS bridge 产物 schema |
| `backend/tests/test_rule_artifacts.py` | 新增 | 三产物 schema 校验测试（样例通过/反例失败） |
| `backend/pyproject.toml` | 修改 | test 组加 `jsonschema==4.23.0`（固定版本，评审 R-8） |

### 3.2 Schema 定义（draft/2020-12，全部 `additionalProperties: true` 宽松产物约束）

**`schemas/binder_bindings.schema.json`**（顶层 `{schema_version, bindings[]}`）：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ai-apk-tracer.local/schemas/binder_bindings.schema.json",
  "title": "Binder AIDL bindings artifact",
  "type": "object",
  "required": ["schema_version", "bindings"],
  "properties": {
    "schema_version": {"const": "1.0.0"},
    "bindings": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["service_class", "code", "resolve_status"],
        "properties": {
          "service_class": {"type": "string", "minLength": 1},
          "code": {"type": ["integer", "null"]},
          "case_token": {"type": ["string", "null"]},
          "interface_method": {"type": ["string", "null"]},
          "descriptor": {"type": ["string", "null"]},
          "on_transact_method_id": {"type": ["string", "null"]},
          "on_transact_descriptor": {"type": ["string", "null"]},
          "case_line": {"type": ["integer", "null"]},
          "dispatch_call_site": {"type": ["object", "null"]},
          "dispatch_descriptor": {"type": ["string", "null"]},
          "dispatch_assigned_to": {"type": ["string", "null"]},
          "path": {"type": ["string", "null"]},
          "line": {"type": ["integer", "null"]},
          "implementation_class": {"type": ["string", "null"]},
          "implementation_method_id": {"type": ["string", "null"]},
          "implementation_path": {"type": ["string", "null"]},
          "implementation_line": {"type": ["integer", "null"]},
          "resolve_status": {"enum": ["bound", "ambiguous", "unresolved"]},
          "gaps": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["code"],
              "properties": {
                "code": {"type": "string"},
                "critical": {"type": "boolean"},
                "detail": {"type": ["string", "null"]}
              }
            }
          }
        }
      }
    }
  }
}
```

**`schemas/receiver_registrations.schema.json`**（顶层 `{schema_version, registrations[]}`）：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ai-apk-tracer.local/schemas/receiver_registrations.schema.json",
  "title": "Dynamic receiver registrations artifact",
  "type": "object",
  "required": ["schema_version", "registrations"],
  "properties": {
    "schema_version": {"const": "1.0.0"},
    "registrations": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["receiver_class", "path", "line"],
        "properties": {
          "receiver_class": {"type": "string", "minLength": 1},
          "call": {"type": ["object", "null"]},
          "method_id": {"type": ["string", "null"]},
          "method_name": {"type": ["string", "null"]},
          "path": {"type": ["string", "null"]},
          "line": {"type": "integer"},
          "actions": {"type": "array", "items": {"type": "string"}},
          "unresolved_action_expressions": {"type": "array", "items": {"type": "string"}},
          "filter_expression": {"type": ["string", "null"]},
          "flag_expression": {"type": ["string", "null"]},
          "flag_value": {"type": ["integer", "null"]},
          "flag_status": {"type": ["string", "null"]},
          "export_status": {"enum": ["exported", "not_exported", "unknown"]},
          "externally_reachable": {"type": ["boolean", "null"]},
          "permission_expression": {"type": ["string", "null"]},
          "permission": {"type": ["string", "null"]},
          "permission_status": {"type": ["string", "null"]},
          "permission_policy": {"type": ["object", "null"]},
          "local_broadcast": {"type": "boolean"},
          "platform_branch": {"type": "boolean"},
          "reportable": {"type": "boolean"},
          "coverage_gaps": {"type": "array", "items": {"type": "object"}}
        }
      }
    }
  }
}
```

**`schemas/webview_js_bridges.schema.json`**（顶层 `{schema_version, bridges[]}`）：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ai-apk-tracer.local/schemas/webview_js_bridges.schema.json",
  "title": "WebView JavaScript bridge call sites artifact",
  "type": "object",
  "required": ["schema_version", "bridges"],
  "properties": {
    "schema_version": {"const": "1.0.0"},
    "bridges": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["line", "text"],
        "properties": {
          "line": {"type": "integer"},
          "text": {"type": ["string", "null"]},
          "description": {"type": ["string", "null"]},
          "sink_kind": {"type": ["string", "null"]},
          "path": {"type": ["string", "null"]},
          "bridge_name": {"type": ["string", "null"]}
        }
      }
    }
  }
}
```

### 3.3 设计决策说明

- **宽松产物约束**（`additionalProperties: true`）：与 `candidate.schema.json` 一致——产物由规则侧子进程产出，约束仅保证下游（`api_surface`）可读取的核心字段与类型，不限制规则侧额外字段（如内部调试字段），避免规则演进导致产物校验频繁失败。
- **`resolve_status` 三态语义**（T2.1 导出时推导）：`bound`=存在唯一实现绑定且无关键 gap；`ambiguous`=存在 `BINDER_IMPLEMENTATION_AMBIGUOUS`/`BINDER_DISPATCH_TARGET_AMBIGUOUS` 等歧义 gap；`unresolved`=存在 `*_UNRESOLVED` gap。**歧义/未解析入口在产物中显式保留**（而非丢弃），`api_surface` 据此将其标记为"不可靠入口"。
- **字段注入点（评审 R-2/R-5）**：规则侧 dict 无 `service_class`（binder，需 T2.1 从 `binder_components` 外层 key 注入）与 `path`（webview，需 T2.1 从 `file["path"]` 注入）；`resolve_status`/`bridge_name` 亦由 T2.1 推导/提取注入。**注入点固化在 T2.1 方案**，schema 仅约束注入后的产物形状。
- **webview 产物信息最薄**：仅调用点 + 桥名（调研确认规则侧无桥对象类型解析，评审 §4.7）；桥对象方法枚举由 T2.2 `api_surface` 用 call_tree 补。
- **`schema_version: const "1.0.0"`**：产物版本门禁，规则侧导出与 `api_surface` 消费双侧校验。

### 3.4 测试方案（`backend/tests/test_rule_artifacts.py`）

- 用 `jsonschema` 的 `Draft202012Validator`（或 `validate`）校验：
  - 每个 schema 的**有效样例**通过（样例严格使用调研到的规则侧字段名）；
  - **反例**失败：缺 required、`resolve_status` 非法枚举、`line` 字符串类型、`schema_version` 非 `1.0.0`、`bindings/registrations/bridges` 非数组。
- 样例 JSON 内联在测试中（不落盘临时文件）。

### 3.5 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| 评审 §7.2 方案 B：规则产物 JSON 传递 | 三个产物 schema 定义为唯一传递载体 | 一致 |
| 方案 §2.1 来源表：Binder/Receiver/WebView 入口 | 产物字段投影自规则侧实际结构（调研确认） | 细化字段，语义不变 |
| 评审 §4.11：`BINDER_IMPLEMENTATION_AMBIGUOUS/UNRESOLVED` gap 透传 | `gaps[]` + `resolve_status` 三态保留 | 一致 |

### 3.6 错误处理

- 产物 JSON 不符合 schema 时：T2.1 导出侧校验失败即视为规则产物错误（产出失败记录，不静默）；T2.2 读取侧对不符合的产物按"该入口缺失"降级。
- `jsonschema` 安装失败：安装到 venv 并声明 test 依赖；测试缺失时 A-4 不通过。

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| 字段名与规则侧内部 dict 不一致 | 产物校验失败 | 字段设计严格对齐调研的规则侧字段名（L1065/L353-389/L2865） | 按实测修正 schema 并回归 |
| `resolve_status` 三态推导在 T2.1 与 schema 语义漂移 | api_surface 入口可靠性误判 | schema 仅定义枚举，推导语义在 T2.1 方案中固定 | 修订 T2.1 推导规则 |
| jsonschema 依赖变更 | 环境变化 | 声明进 pyproject test 组 | 无需回退（仅测试依赖） |

## 5. 依赖

- 无前置；依赖 `jsonschema`（测试）。
