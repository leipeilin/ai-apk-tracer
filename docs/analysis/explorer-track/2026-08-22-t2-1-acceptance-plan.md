# 任务验收方案：T2.1（规则产物导出）

> **任务编号**：T2.1
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-1-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（detector 层真实执行 + rule_runner 层单测）+ 全量回归 + 手工端到端（source enabled 场景）

---

## 1. 验收范围

- 规则侧 artifacts 收集（三分支）+ bridge_name 补齐 + 预算截断；backend 提取/校验/写盘/`last_artifacts`；orchestrator manifest 注册。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | Receiver 产物（**index 路径真实执行**，评审 R-5） | `test_detector_receiver_artifact` | build_code_index 真实索引 → 全量记录（含非 reportable）+ schema 校验通过；legacy 补充用例 |
| A-2 | WebView 产物 + 多桥枚举（评审 R-7） | `test_detector_webview_artifact` | 同文件两桥 → artifacts 两条 + bridge_name 正确 + schema 通过 |
| A-3 | Binder 产物（真实形态 + 推导，评审 R-1/R-2） | `test_binder_bindings_artifact_helper` | mock 真实形态 transaction（无 resolve_status）→ 推导三态正确 + qualified_name 注入 + schema 通过 |
| A-4 | 体积截断 | `test_artifact_budget_truncation` | 截断 + artifact_gaps 含真实 total（CJK 口径） |
| A-5 | 汇总侧写盘 + last_artifacts | `test_rule_runner_exports_artifacts` | 三文件（schema_version + entry_key 结构）+ last_artifacts（record_count/truncated） |
| A-6 | per-record 剔除粒度（评审 R-3） | `test_rule_runner_per_record_invalid` | 坏记录剔除 + RULE_ARTIFACT_RECORD_INVALID gap + 其余记录正常写盘 |
| A-7 | T0.4 回归（含 schema 修订） | 既有 `test_rule_artifacts.py` + receiver_class 可空新用例 | 全部通过 |
| A-7b | orchestrator 注册方法 | `test_register_rule_artifacts`（评审 R-6） | manifest artifacts append 断言 |
| A-8 | 单测通过 | `.venv/bin/python -m pytest tests/test_rule_artifacts.py tests/test_rule_runner_artifacts.py -q` | 全部通过 |
| A-9 | 全量回归 | `.venv/bin/python -m pytest -q` | 957+ 全部通过（M1 审查修复后基线 957） |
| A-10 | 统一校验 | `scripts/check-all.sh` + `ruff check`（改动文件） | 通过 |

## 3. 手工验收清单（source_analysis enabled 真实链路）

| 编号 | 操作 | 预期 |
|---|---|---|
| H-1 | `assets.enabled=true` + source enabled 配置下发起真实扫描（含 Binder service 的 APK） | run_manifest.artifacts 含 `binder_bindings/receiver_registrations/webview_js_bridges` 三条目 |
| H-2 | 检查 `rule-results/{binder_bindings,...}.json` | schema_version=1.0.0 + 记录结构与 schema 一致 |

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 旧规则输出（无 artifacts 键） | mock result 无 artifacts | 汇总侧跳过（零产物、零 gap） |
| N-2 | 未知 artifacts 键 | mock result 含 `"artifacts": {"unknown": []}` | _validate_output 协议错误（RULE_PROTOCOL_ERROR）——白名单拒绝 |
| N-3 | 空记录集 | artifacts 值为 `[]` | 正常写盘（空数组 + record_count=0） |
| N-4 | reader=None（legacy Binder 路径） | 无 index payload | binder_bindings 产物为空数组（无 transactions 数据源）——不报错 |

## 5. 回归标准

- [ ] 既有规则行为零变化（收集只读不改流；artifact_sink 默认 None）
- [ ] stdout 预算：artifacts 后单规则 stdout 不击穿 10 MiB（测试 4 的截断保证）
- [ ] 全量 957 passed / 0 failed

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 7 项意见第 1 轮全部采纳（含致命 R-1 service_class dict 注入 / R-2 resolve_status 推导 / R-5 测试改 index 路径）。实施中测试再暴露两处 schema 与实际产出不齐（R-3 同性）：① 校验 payload 缺 schema_version（required）致全剔除——修复为构造完整顶层结构；② 真实产出含 `export_status='legacy_unspecified'`——T0.4 枚举设计缺口，显式修订 + 评审文档补记（§6b）。另修复注释剔除 rfind 错位（对整个 code rfind 会取到文件末行——改为 match 前缀）。全量 969 passed / 0 failed（+12）。

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | build_code_index 真实 index 路径：产物非空 + 全量 schema 校验通过（含 legacy_unspecified 枚举修订）+ path/line/receiver_class/reportable 断言；legacy 补充用例（空数组通过） | - |
| A-2 | 通过 | 同文件双桥 → 2 条记录 + bridge_name={Bridge1,Bridge2} + sink_kind/path 齐全 + schema 通过；注释行剔除用例通过 | - |
| A-3 | 通过 | 真实形态 mock（service_class=dict、transaction 无 resolve_status）→ qualified_name 注入 + manifest 名兜底 + bound/ambiguous/unresolved 推导正确 + schema 通过 | - |
| A-4 | 通过 | 1500×4KB CJK 记录 → 截断（≤2MiB）+ gap 保 total=1500；未超限零 gap | - |
| A-5 | 通过 | 写盘（schema_version+bridges 结构）+ last_artifacts（record_count/truncated） | - |
| A-6 | 通过 | 坏记录剔除（2 条）+ RULE_ARTIFACT_RECORD_INVALID gap（critical=false，含索引）+ 好记录保留（per-record 粒度实证） | - |
| A-7 | 通过 | T0.4 全部既有用例 + receiver_class=None 新用例通过 | - |
| A-7b | 通过 | mock storage → _register_rule_artifacts：decompile+binder_bindings 顺序 append + 空产物零操作 | - |
| A-8 | 通过 | 65 项全过（12 新增 + 53 既有） | - |
| A-9 | 通过 | 全量 **969 passed / 0 failed**（957+12） | - |
| A-10 | 通过 | check-all（含前端构建）全过；新代码 ruff 零新增（orchestrator 剩余 BLE001 为既有债务） | - |
| H-1 | 待手工 | 真实反编译全链路（source enabled）——留用户环境执行 | - |
| H-2 | 待手工 | 同上 | - |
| N-1 | 通过 | 无 artifacts 键 → _export 直接返回（空清单） | - |
| N-2 | 通过 | 未知键经 _validate_output 白名单拒绝（协议层）；导出侧容错跳过（test_rule_runner_skips_unknown_keys） | - |
| N-3 | 通过 | 空记录集正常写盘（legacy receiver 用例实证空数组合法） | - |
| N-4 | 通过 | 无 index 的 Binder 路径 → binder_batch={} → 产物空数组不报错 | - |
