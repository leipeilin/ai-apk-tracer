# 任务验收方案：T0.5（api_entry_table / attack_surface Schema）

> **任务编号**：T0.5
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t0-5-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（jsonschema）+ 全量回归

---

## 1. 验收范围

- 2 个产物 schema + 测试追加。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 两 schema 文件存在且结构合法 | 文件存在；draft/2020-12 + `$id`/`title`/`required` | 通过 |
| A-2 | api_entry_table act_ 样例通过 | `jsonschema.validate(样例, schema)` | 通过（manifest 条目，含 entry_method/intent_filters） |
| A-3 | api_entry_table binder 样例通过 | 同上 | 通过（含 reliability=bound 与 ambiguous 两种） |
| A-4 | api_entry_table dynrcv/wv 样例通过 | 同上 | 通过（source=rule_artifact:receiver_registrations / webview_bridge） |
| A-5 | attack_surface activity 样例通过 | 同上 | 通过（含 sensitive_capabilities/api_entry_refs；无顶层 component_type） |
| A-6 | attack_surface receiver 样例通过 | 同上 | 通过（source="manifest+dynamic"，kind=receiver） |
| A-7 | 反例：缺 required | 缺 `entry_id`/`kind`/`component_name`/`source` 或 attack `kind`/`name`/`exported` | 抛 `ValidationError` |
| A-8 | 反例：枚举越界 | `kind="widget"`、`source="bogus"`、`reliability="partial"`、attack `kind="widget"`、`source="bogus"` | 抛 `ValidationError` |
| A-9 | 反例：entry_id pattern | `entry_id="xxx_com_example"`、`entry_id="act_非法"`、`entry_id="rcv"`（缺后缀）、`entry_id="webview_bridge_x"`（kind 应为 webview_bridge 但前缀用 webview 后跟 _bridge_x 属合法—需构造非法如 `entry_id="wv_x"` 因前缀改为 webview） | 抛 `ValidationError` |
| A-16 | svc_/prv_/静态 rcv_ 样例通过（评审 R-4） | `jsonschema.validate(样例, schema)` | 通过（`svc_`、`prv_`（含 authorities）、`rcv_` source=manifest） |
| A-10 | 反例：类型错误 | `exported="yes"`、`transaction_code="1"`、`bridge_line="8"` | 抛 `ValidationError` |
| A-11 | 反例：顶层 | `api_entries` 非数组 / `components` 非数组 | 抛 `ValidationError` |
| A-12 | 反例：schema_version | `schema_version="2.0.0"` | 抛 `ValidationError` |
| A-13 | 测试通过 | `.venv/bin/python -m pytest tests/test_rule_artifacts.py -q` | 全部通过 |
| A-14 | 全量回归 | `.venv/bin/python -m pytest -q` | 除 3 个 pre-existing guard_verifier 失败外全部通过 |
| A-15 | 统一校验 | `scripts/check-all.sh` | 同上，无新增失败 |

## 3. 回归标准

- [ ] T0.4 三产物 schema 测试不受影响。
- [ ] 两 schema 不注册进 `AI_SCHEMA_MODELS`/`prompts/registry.yaml`（确定性产物）。
- [ ] `ruff check` 通过。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 空数组 | `api_entries=[]` / `components=[]` | 通过 |
| N-2 | 额外字段容忍 | 条目加 `extra_debug` | 通过 |
| N-3 | 可空字段 | `entry_method=null`、`exported=null`、`permissions=[]` | 通过 |
| N-4 | reliability=not_applicable | act_ 条目设 `reliability="not_applicable"` | 通过（非 binder 兜底） |
| N-5 | entry_method 空数组 | attack `entry_methods=[]`（code-index 未解析） | 通过 |

## 5. 回退方案

- 任一验收点失败：修复后复验；若字段与生成侧实际不符（T2.2/T2.3）按对齐修正并回归。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 7 项意见第 1 轮全部采纳（含 2 项高严重度：`export_status` 无源 `"null"` 枚举、attack/api 两侧 `source` 语义割裂）。全量 873 passed + 3 个 pre-existing guard_verifier 失败（同前）。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 两 schema 存在，draft/2020-12 + `$id`/`title`/`required` | - |
| A-2 | 通过 | act_ 条目（manifest + entry_method/intent_filters）通过 | - |
| A-3 | 通过 | binder 条目 reliability=bound/ambiguous 均通过 | - |
| A-4 | 通过 | dynrcv_（source=rule_artifact:receiver_registrations）/webview_（webview_bridge）通过 | - |
| A-5 | 通过 | attack activity 样例（sensitive_capabilities/api_entry_refs，无顶层 component_type）通过 | - |
| A-6 | 通过 | attack receiver 样例（source="manifest+dynamic"）通过 | - |
| A-7 | 通过 | 缺 entry_id/kind/component_name/source 或 attack kind/name/exported → ValidationError | - |
| A-8 | 通过 | kind/source/reliability/attack kind 非法枚举 → ValidationError（含 reliability="unknown" 拒绝） | - |
| A-9 | 通过 | `xxx_`/`act_非法`/`rcv`/`wv_`（前缀已改 webview）/`act_` 空后缀 → ValidationError | - |
| A-10 | 通过 | `transaction_code="1"`/`bridge_line="88"` → ValidationError | - |
| A-11 | 通过 | api_entries/components 非数组 → ValidationError | - |
| A-12 | 通过 | schema_version=2.0.0 → ValidationError | - |
| A-13 | 通过 | test_rule_artifacts.py 53 项全过 | - |
| A-14 | 通过 | 全量 pytest：873 passed + 3 pre-existing | - |
| A-15 | 通过 | check-all：873 passed + 3 pre-existing；ruff 全过 | - |
| A-16 | 通过 | svc_/prv_（含 authorities）/rcv_（source=manifest）通过 | - |
| N-1 | 通过 | api_entries=[]/components=[] 通过 | - |
| N-2 | 通过 | 额外字段容忍（宽松） | - |
| N-3 | 通过 | entry_method=null、exported=null、permissions=[] 通过 | - |
| N-4 | 通过 | reliability=not_applicable 对 act_ 条目通过 | - |
| N-5 | 通过 | attack entry_methods=[] 通过（code-index 未解析不伪造） | - |
