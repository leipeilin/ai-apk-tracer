# 任务验收方案：T0.4（规则产物 Schema）

> **任务编号**：T0.4
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t0-4-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（jsonschema）+ 依赖校验 + 全量回归

---

## 1. 验收范围

- 3 个规则产物 schema + 测试文件 + `jsonschema` 依赖声明。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 三 schema 文件存在且结构合法 | 三个文件存在；`$schema` 为 draft/2020-12、含 `$id`/`title`/`required` | 通过 |
| A-2 | binder_bindings 有效样例通过 | `jsonschema.validate(样例, schema)` | 通过；样例含 `resolve_status` 三态各一、`gaps` 透传 |
| A-3 | receiver_registrations 有效样例通过 | 同上 | 通过；样例含 `externally_reachable=null` 变体、`path` 可空 |
| A-4 | webview_js_bridges 有效样例通过 | 同上 | 通过；样例含 `line`+`text`+`description`+`sink_kind`，`path`/`bridge_name` 由 T2.1 注入/提取 |
| A-5 | 反例：缺 required | 移除 `bindings[0].resolve_status` / `registrations[0].receiver_class` / `bridges[0].line` | 抛 `ValidationError` |
| A-6 | 反例：枚举越界 | `resolve_status="partial"`、`export_status="partial"` | 抛 `ValidationError` |
| A-7 | 反例：类型错误 | `line` 为字符串、`code` 为字符串、`actions[0]` 为非字符串、`dispatch_call_site` 为字符串（应为 object\|null） | 抛 `ValidationError` |
| A-8 | 反例：schema_version | `schema_version="2.0.0"` | 抛 `ValidationError` |
| A-9 | 反例：顶层类型 | `bindings` 为非数组（dict） | 抛 `ValidationError` |
| A-10 | jsonschema 依赖声明 | `pyproject.toml` test 组含 `jsonschema==4.23.0`（评审 R-8 固定版本）；venv 可 `import jsonschema` | 通过 |
| A-11 | 测试通过 | `cd backend && .venv/bin/python -m pytest tests/test_rule_artifacts.py -q` | 全部通过 |
| A-12 | 全量回归 | `.venv/bin/python -m pytest -q` | 除 3 个 pre-existing guard_verifier 失败外全部通过 |
| A-13 | 统一校验 | `scripts/check-all.sh` | 同上，无新增失败 |

## 3. 回归标准

- [ ] 既有测试（含 AI schema/registry）全部通过，未受影响。
- [ ] `ruff check` 新测试文件通过。
- [ ] 三 schema 不注册进 `AI_SCHEMA_MODELS`/`prompts/registry.yaml`（属规则产物，非 AI 协议）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 空数组产物 | `bindings=[]` / `registrations=[]` / `bridges=[]` | 通过（合法空产物） |
| N-2 | 额外字段容忍 | 样例中加 `extra_debug_field` | 通过（`additionalProperties: true`） |
| N-3 | 顶层非对象 | 产物为 JSON 数组 | 抛 `ValidationError` |
| N-4 | `bridge_name` null | `bridges[0].bridge_name=null` | 通过（可空） |
| N-5 | `code` null | `bindings[0].code=null` | 通过（`["integer","null"]`） |
| N-6 | `actions` 元素非字符串 | `registrations[0].actions=["android.action.X", 123]` | 抛 `ValidationError`（评审 R-7） |
| N-7 | `implementation_*` 未绑定缺失 | binder 样例不含 `implementation_*` 字段（仅 required 三件套 + 未绑定 gap） | 通过（可空非 required；评审 R-7） |
| N-8 | `dispatch_call_site` 类型错 | `bindings[0].dispatch_call_site="call site"` | 抛 `ValidationError` |

## 5. 回退方案

- 任一验收点失败：修复后复验；字段名与规则侧实际不符时按调研修正 schema 并回归。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审识别 2 个致命字段名错误（R-1 receiver `call_path/call_line`、R-2 webview `path`）已在实施前修订；样例严格取自规则侧实际产出字段。全量 847 passed + 3 个 pre-existing guard_verifier 失败（同前）。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 三 schema 存在，draft/2020-12 + `$id`/`title`/`required` | - |
| A-2 | 通过 | binder 样例（bound/ambiguous/unresolved 三态 + gaps 透传）校验通过 | - |
| A-3 | 通过 | receiver 样例（含 `externally_reachable=null`、`path=null` 变体）通过 | - |
| A-4 | 通过 | webview 样例（line/text/description/sink_kind）通过 | - |
| A-5 | 通过 | 缺 resolve_status / receiver_class / line → ValidationError | - |
| A-6 | 通过 | resolve_status/export_status 非法枚举 → ValidationError | - |
| A-7 | 通过 | line/code/actions 元素/dispatch_call_site 类型错误 → ValidationError | - |
| A-8 | 通过 | schema_version 非 1.0.0 → ValidationError | - |
| A-9 | 通过 | bindings 为非数组 → ValidationError | - |
| A-10 | 通过 | pyproject test 组含 `jsonschema==4.23.0`；venv `import jsonschema` 成功 | - |
| A-11 | 通过 | test_rule_artifacts.py 27 项全过 | - |
| A-12 | 通过 | 全量 pytest：847 passed + 3 pre-existing | - |
| A-13 | 通过 | check-all：847 passed + 3 pre-existing；ruff 全过 | - |
| N-1 | 通过 | 三产物空数组通过 | - |
| N-2 | 通过 | `extra_debug_field` 容忍（additionalProperties） | - |
| N-3 | 通过 | 顶层数组 → ValidationError | - |
| N-4 | 通过 | `bridge_name=null` 通过 | - |
| N-5 | 通过 | `code=null` 通过 | - |
| N-6 | 通过 | actions 元素非字符串 → ValidationError | - |
| N-7 | 通过 | unresolved 无 implementation_* → 通过 | - |
| N-8 | 通过 | dispatch_call_site 字符串 → ValidationError | - |
