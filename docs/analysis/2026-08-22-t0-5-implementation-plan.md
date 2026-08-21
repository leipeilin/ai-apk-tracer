# 任务实施方案：T0.5（api_entry_table / attack_surface Schema）

> **任务编号**：T0.5
> **日期**：2026-08-22
> **依据大纲**：
> - 方案：`docs/analysis/2026-08-18-project-optimization-plan.md` §2.1（API 入口表来源表）、§2.3（攻击面导出）、§5.1（入口表 Schema 草案）
> - 实施计划：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` T0.5
> - 对齐输入：2026-08-22 对 §2.1 来源表 × manifest（`manifest.py:130-203`）× T0.4 三规则产物 × code-index 的对齐分析（含 3 个新决策点）
> **状态**：起草
> **前置依赖**：T0.4（三规则产物 schema，已提交 `0b2915a`）

---

## 1. 任务目标与范围

- **目标**：定义探索轨 Agent1 的两个核心确定性输入 schema——`api_entry_table.schema.json`（API 入口表）与 `attack_surface.schema.json`（四组件攻击面）。
- **范围**：
  - 两个宽松确定性产物 schema（draft/2020-12、`additionalProperties: true`，风格同 T0.4）；
  - `backend/tests/test_rule_artifacts.py` 追加校验用例（样例/反例）。
- **非范围**：
  - `api_surface.py` / `attack_surface.py` 生成实现（T2.2/T2.3）；
  - entry_method 的 code-index 解析逻辑（T2.2/T2.3）；
  - sensitive_capabilities 的规则候选聚合逻辑（T2.3）。

## 2. 现状锚点（2026-08-22 对齐结果）

- **manifest**（`backend/app/analysis/manifest.py`）：`parse_manifest` 输出 `{package, min_sdk, target_sdk, debuggable, uses_permissions, authority_conflicts, components[]}`；组件字段：`kind/name/exported/exported_reason/permission/permission_protection/intent_filters{actions,categories,data}/authorities/authority_tokens/read_permission/write_permission/path_permissions/grant_uri_patterns/provider_paths/broadcast_action_authorization`。
- **规则产物**（T0.4）：`binder_bindings`（resolve_status 三态 + gaps）、`receiver_registrations`（export_status/externally_reachable/actions）、`webview_js_bridges`（line/text/description/sink_kind/path/bridge_name）。
- **§5.1 草案**：已有 `act_`（manifest）与 `binder_`（rule_artifact:binder_bindings）条目示例，字段 `entry_id/kind/component_name/exported/permissions/entry_method/intent_filters/source` 与 `interface_method/transaction_code/implementation_method_id`。
- **缺口（对齐决策点）**：① manifest 组件无 `entry_method`（需 code-index 解析生命周期方法）；② Binder 入口需 `reliability`（bound/ambiguous/unresolved）标记不可靠入口；③ 静态（manifest）与动态（规则产物）Receiver 需 `source` 区分。
- **风格**：T0.4 三产物 schema（宽松、required+properties）。

## 3. 详细实现方案

### 3.1 文件变更清单

| 文件 | 变更类型 | 变更内容摘要 |
|---|---|---|
| `schemas/api_entry_table.schema.json` | 新增 | API 入口表 schema（六类入口统一条目） |
| `schemas/attack_surface.schema.json` | 新增 | 攻击面 schema（四组件共享结构） |
| `backend/tests/test_rule_artifacts.py` | 修改 | 追加两 schema 校验用例 |

### 3.2 `schemas/api_entry_table.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ai-apk-tracer.local/schemas/api_entry_table.schema.json",
  "title": "API entry table",
  "type": "object",
  "required": ["schema_version", "package", "api_entries"],
  "properties": {
    "schema_version": {"const": "1.0.0"},
    "package": {"type": ["string", "null"]},
    "api_entries": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["entry_id", "kind", "component_name", "source"],
        "properties": {
          "entry_id": {"type": "string", "pattern": "^(act|svc|rcv|prv|binder|dynrcv|webview)_[A-Za-z0-9_]+$"},
          "kind": {"enum": ["activity", "service", "provider", "receiver", "binder", "webview_bridge"]},
          "component_name": {"type": "string", "minLength": 1},
          "source": {"enum": ["manifest", "rule_artifact:binder_bindings", "rule_artifact:receiver_registrations", "rule_artifact:webview_js_bridges"]},
          "exported": {"type": ["boolean", "null"]},
          "permissions": {"type": "array", "items": {"type": "string"}},
          "entry_method": {"type": ["string", "null"]},
          "intent_filters": {
            "type": ["array", "null"],
            "items": {
              "type": "object",
              "properties": {
                "actions": {"type": "array", "items": {"type": "string"}},
                "categories": {"type": "array", "items": {"type": "string"}},
                "data": {"type": "array", "items": {"type": "object"}}
              }
            }
          },
          "authorities": {"type": ["array", "null"], "items": {"type": "string"}},
          "interface_method": {"type": ["string", "null"]},
          "transaction_code": {"type": ["integer", "null"]},
          "implementation_method_id": {"type": ["string", "null"]},
          "reliability": {"enum": ["bound", "ambiguous", "unresolved", "not_applicable"]},
          "actions": {"type": ["array", "null"], "items": {"type": "string"}},
          "export_status": {"enum": ["exported", "not_exported", "unknown"]},
          "externally_reachable": {"type": ["boolean", "null"]},
          "bridge_path": {"type": ["string", "null"]},
          "bridge_line": {"type": ["integer", "null"]},
          "bridge_name": {"type": ["string", "null"]}
        }
      }
    }
  }
}
```

> 说明：统一条目声明全部可选字段（各 kind 只用其相关子集）；`required` 四项（entry_id/kind/component_name/source）为跨 kind 恒有，顶层 `package` 恒有（可 null，对齐 manifest 产物；评审 R-7）。字段边界（评审 R-1）：`exported`（boolean，可 null）用于 manifest 组件条目；`export_status`（枚举，对齐 T0.4 receiver 产物）仅用于动态 receiver 条目；两者不并存于同一 entry 表达导出语义。`reliability` 的 `not_applicable` 用于非 binder 入口（评审 R-6 删除了无源的 `unknown`）。

### 3.3 `schemas/attack_surface.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ai-apk-tracer.local/schemas/attack_surface.schema.json",
  "title": "Attack surface (per component kind)",
  "type": "object",
  "required": ["schema_version", "package", "components"],
  "properties": {
    "schema_version": {"const": "1.0.0"},
    "package": {"type": ["string", "null"]},
    "components": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["kind", "name", "exported"],
        "properties": {
          "kind": {"enum": ["activity", "service", "provider", "receiver"]},
          "name": {"type": "string", "minLength": 1},
          "exported": {"type": "boolean"},
          "exported_reason": {"type": ["string", "null"]},
          "permission": {"type": ["string", "null"]},
          "permission_protection": {"type": ["string", "null"]},
          "entry_methods": {"type": "array", "items": {"type": "string"}},
          "intent_filters": {
            "type": ["array", "null"],
            "items": {
              "type": "object",
              "properties": {
                "actions": {"type": "array", "items": {"type": "string"}},
                "categories": {"type": "array", "items": {"type": "string"}},
                "data": {"type": "array", "items": {"type": "object"}}
              }
            }
          },
          "authorities": {"type": ["array", "null"], "items": {"type": "string"}},
          "actions": {"type": ["array", "null"], "items": {"type": "string"}},
          "sensitive_capabilities": {"type": "array", "items": {"type": "string"}},
          "api_entry_refs": {"type": "array", "items": {"type": "string"}},
          "source": {"enum": ["manifest", "dynamic", "manifest+dynamic"]}
        }
      }
    }
  }
}
```

> `source` 语义（对齐决策点 ②；评审 R-2 术语交叉引用）：`manifest`=静态声明组件（仅 manifest）；`dynamic`=仅动态注册，**等价于 api_entry_table 的 `source="rule_artifact:receiver_registrations"`**；`manifest+dynamic`=两者并存。receiver.json 需合并静态与动态注册，其余三组件文件 `source` 恒为 `manifest`。顶层 `component_type` 已删除（评审 R-3：文件命名 activity/service/provider/receiver.json 已区分类型），类型由 `components[].kind` 表达。

### 3.4 设计决策说明

- **宽松产物约束**（`additionalProperties: true`）：与 T0.4 一致——约束保证下游（Agent1 prompt 渲染、`explorer.py`）可读取的核心字段与类型，允许生成侧附带额外字段。
- **`entry_method` 依赖 code-index（对齐决策点 ①）**：schema 中 `entry_method`/`entry_methods` 可空——`api_surface`/`attack_surface` 在 T2.2/T2.3 从 `code-index.json` 解析组件生命周期方法填充；解析不到时为 `null`/空数组（不伪造）。
- **Binder `reliability`（对齐决策点 ②）**：由 `binder_bindings.resolve_status` 映射；`ambiguous`/`unresolved` 入口保留在表中并标记（Agent1 可见但 `explorer.py` 可配置低优先级）；非 binder 入口为 `not_applicable`。
- **静态/动态 Receiver 区分（对齐决策点 ③）**：manifest 静态 receiver `source="manifest"`；动态注册 `source="rule_artifact:receiver_registrations"`；attack_surface 的 receiver.json 合并两者并标注 `source`。
- **`attack_surface` 依赖 `api_entry_table`**：`api_entry_refs` 引用同一组件的 `entry_id`；时序上 `api_surface`（T2.2）先于 `attack_surface`（T2.3）。
- **sensitive_capabilities**：T2.3 从 rule_prescan 规则候选按组件聚合 `rule_id` 集合填充；schema 仅为字符串数组。

### 3.5 测试方案（`backend/tests/test_rule_artifacts.py` 追加）

- 有效样例：api_entry_table 含 act_（manifest，含 intent_filters/entry_method）、binder_（reliability=bound）、binder_（reliability=ambiguous）、dynrcv_（rule_artifact:receiver_registrations）、wv_（webview_bridge）；attack_surface 的 activity 样例（含 sensitive_capabilities/api_entry_refs）+ receiver 样例（source="manifest+dynamic"）。
- 反例：缺 required、kind/source/reliability 非法枚举、entry_id 前缀非法、component_type 非法、顶层类型错、schema_version 错。

### 3.6 与大纲一致性对照

| 大纲条目 | 本方案实现方式 | 一致性说明 |
|---|---|---|
| 方案 §2.1 六类入口来源表 | api_entry_table 统一条目 + `source` 枚举对齐来源表 | 一致 |
| 方案 §5.1 草案（act_/binder_ 示例） | 字段保留并扩展（补 reliability、动态 receiver/webview 类型、顶层 package） | 扩展，草案字段不变 |
| 方案 §2.3 四组件字段（名/导出/权限/入口方法/intent-action-uri/敏感能力/关联入口） | attack_surface components[] 逐字段对应 | 一致 |
| 方案 §2.3 四文件（activity/service/provider/receiver.json） | `component_type` 区分，四文件复用同 schema | 一致 |

## 4. 风险与回退

| 风险 | 影响 | 对策 | 回退方案 |
|---|---|---|---|
| entry_method 依赖 code-index 未实现时字段常空 | Agent1 输入信息少 | T2.2/T2.3 实现入口方法解析；schema 允许 null/空数组 | 探索轨输入降级（无 entry_method 仍可用） |
| `reliability` 枚举与 T2.1 推导漂移 | 入口可靠性误标 | 枚举定义固定，T2.1 推导规则同步 | 修订推导 |
| 静态/动态 receiver 合并歧义 | attack_surface 重复/漏项 | `source` 字段三值区分，T2.3 合并逻辑去重 | 按 source 拆分视图 |

## 5. 依赖

- 前置：T0.4（已提交）；运行时依赖：manifest（既有）、code-index（既有）、T0.4 三规则产物（T2.1 导出）、api_entry_table（attack_surface 引用）。
