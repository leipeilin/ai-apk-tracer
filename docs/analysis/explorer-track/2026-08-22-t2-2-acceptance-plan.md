# 任务验收方案：T2.2（api_surface：API 入口表）

> **任务编号**：T2.2
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-2-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（真实 index + 手写产物文件）+ 集成（TestClient）+ 全量回归

---

## 1. 验收范围

- `build_api_entry_table`（六类入口 + lifecycle 解析 + 转换规则）+ orchestrator `api_surface` 阶段 + 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式 | 预期结果 |
|---|---|---|---|
| A-1 | manifest 入口 + lifecycle 方法 | `test_manifest_entries_with_lifecycle_methods` | 每方法一条 + entry_method 签名格式 + exported bool + schema 通过 |
| A-2 | 无 index 降级 | `test_manifest_entry_without_methods` | entry_method=null（不伪造） |
| A-3 | binder 入口转换 | `test_binder_entries_from_artifact` | reliability 三态 + exported 匹配 + include_binder 开关 |
| A-4 | dynrcv 入口转换 | `test_dynrcv_entries_from_artifact` | legacy_unspecified→unknown + receiver_class=None 兜底 |
| A-5 | webview 入口转换 | `test_webview_entries_from_artifact` | 路径 FQCN + 开关 |
| A-6 | entry_id 合法性 | `test_entry_id_pattern_and_dedup` | 内部类 $ 转换 + 冲突去重 |
| A-7 | 产物缺失容错 | `test_missing_artifacts_tolerated` | manifest 入口仍生成 |
| A-8 | 集成：阶段执行 | `test_orchestrator_api_surface_stage` | artifacts 注册 + stage completed（source=false manifest-only） |
| A-9 | 默认关闭零行为 | 既有全量回归（enabled=false 默认） | 无 api-surface 产物/阶段 |
| A-10 | 单测通过 | `pytest tests/test_api_surface.py -q` | 全部通过 |
| A-11 | 全量回归 | `pytest -q` | 969+ 全部通过（T2.1 基线 969） |
| A-12 | 统一校验 | `scripts/check-all.sh` + `ruff check`（新文件） | 通过 |

## 3. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 空组件清单 | manifest components=[] | api_entries=[]（空表合法）+ package 正常 |
| N-2 | binder 产物 service_class 不在 manifest | 无匹配组件 | exported=null（不伪造） |
| N-3 | 产物 JSON 损坏 | rule-results/binder_bindings.json 非法 JSON | 容错空数组 + 阶段不挂（日志告警） |
| N-4 | 组件名含非常规字符 | FQCN 带 `$`/数字开头段 | sanitize 后 pattern 合法 |
| N-5 | 同组件同方法重复解析 | index 中同名方法多载（onCreate 双签名） | entry_id 去重后缀区分 |

## 4. 回归标准

- [ ] 既有 run 行为零变化（api_surface.enabled 默认 false）
- [ ] 全量 969 passed / 0 failed
- [ ] manifest artifacts 注册不破坏既有条目（decompile/rule artifacts 顺序保留）

## 5. 回退方案

- 任一验收点失败：修复后复验；设计缺陷上升评审第 2 轮。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 9 项意见第 1 轮全部采纳（含高 R-1 entry_method 实际格式 `name(params)->return`（indexer 点分形态，T0.5 JVM 样例为草案理想态）/ R-2 exported 四值域 conditional·unknown→None + exported_reason 透传 / R-3 qualified_class 精确过滤）。实施中三处修正：① `component_files` 返回的 file dict 已含 `methods`（键 `_file_id` 非 `id`）——方法解析直接消费现成列表（原方案 get_methods_for_files 路径作废）；② entry_method 参数为声明处简单名形态（`onCreate(Bundle)->void`）——断言按实际产出；③ 去重测试语义修正（`onCreate_2` 非白名单方法本就不产 entry——`__2` 后缀为防御性设计，测试改为验证重载 `onCreate/onCreate__2` 各一条 + 非白名单不产 entry）。全量 979 passed / 0 failed（+10）。

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 每方法一条（onCreate/onNewIntent）+ `onCreate(Bundle)->void` 实际格式 + exported conditional→None + reason 透传 + service true→True/permission→permissions + provider authorities + 同简名异包不误匹配 + helper 非白名单不出现 | - |
| A-2 | 通过 | reader=None → 组件级单条 + entry_method=null | - |
| A-3 | 通过 | reliability 三态 + exported 按 service_class 匹配（JobService=True / Ambiguous=None）+ include_binder=false 不生成 | - |
| A-4 | 通过 | legacy_unspecified→unknown + receiver_class=None 兜底 path 类名 + BootReceiver onReceive 解析 + kind="receiver" + actions/reachable 透传 | - |
| A-5 | 通过 | 注册类 FQCN（sources/ 前缀条件剥离两形态）+ include_webview_jsbridge=false 不生成 | - |
| A-6 | 通过 | `MainActivity$Inner`→`_` 合法 + onCreate 重载 `__2` 去重 + 非白名单 onCreate_2 不产 entry | - |
| A-7 | 通过 | 三文件全缺 + 空数组文件（`{"bindings": []}`）两形态均容错 | - |
| A-8 | 通过 | 集成：artifacts 含 api_entry_table + stage completed + manifest-only 下 rule_artifact 入口为空 | - |
| A-9 | 通过 | 默认 enabled=false 零行为（全量 979 既有测试无回归） | - |
| A-10 | 通过 | 10 项全过 | - |
| A-11 | 通过 | 全量 **979 passed / 0 failed**（969+10） | - |
| A-12 | 通过 | check-all 全过；api_surface.py/test ruff 零违规（removeprefix 修复 + 导入排序） | - |
| N-1 | 通过 | 空组件清单 → 空表 schema 合法 | - |
| N-2 | 通过 | binder service_class 不在 manifest → exported=None（A-3 断言） | - |
| N-3 | 通过 | 信封结构错误（wrong_key）+ 非法 JSON 两形态容错空记录 | - |
| N-4 | 通过 | `$` 内部类 sanitize（A-6） | - |
| N-5 | 通过 | 同名重载 `__2` 后缀区分（A-6 修正语义） | - |
