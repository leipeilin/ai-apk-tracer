# 核验报告：P2 确定性 bug 修复（ruleset-quality-review）

> **任务编号**：P2（E1 有序广播权限判定重写 + E2 getLastKnownLocation 补录 + promote 锚点加固）
> **核验日期**：2026-08-27
> **核验对象**：`docs/analysis/rules-review/2026-08-27-p2-deterministic-bugfix.md` 及其变更
> **核验模型**：deepseek-v4-pro（独立子 agent，只读评审，52 次工具调用）
> **状态**：第 1 轮（已闭合）

---

## 1. 核验结论摘要（子 agent 原文）

任务主体达标：E1 的 `_ordered_broadcast_has_permission`（detector.py:203-222）经逐形态独立推演，正确消除了"第一参数内嵌逗号 + null 权限"的旧漏报路径且未引入新误报/新漏报路径，旧正则在执行代码中无残留；E2 的 `getLastKnownLocation` 在 dataflow.py:2918（arity {1}，与公开签名单参一致）与 versions.yaml:190-194 双源落位；promote 自环拒绝与无约束拒绝位置正确、不误伤用法 B 与既有 4 条 manual 条目。主要风险有三：promote CLI 加固零自动化测试覆盖；报告对 getLastLocation 条目"不算错误条目"的定性不完整（versions.yaml 侧死条目与两轨反向缺口未披露）；测试数字无法在核验环境独立复跑（已完成静态核对与账目自洽性验证）。无关键/高级问题。

## 2. 问题清单（子 agent 提出）

**【R-1】【中】** promote CLI 两处加固无任何自动化测试（backend/tests 仅测 library 层 `promote_custom_sink()` 函数，不触及 CLI 的自环拒绝与无约束拒绝分支；无约束拒绝连手工实测记录都没有）。
**【R-2】【中】** 报告对 getLastLocation 条目"不算错误条目"的定性只覆盖 dataflow 视角：versions.yaml:185-189 该条目仅挂 `receiver_leaves: [LocationManager]`——LocationManager.getLastLocation 组合在 framework 不存在（死条目），真实组合 `FusedLocationProviderClient.getLastLocation()` 在消费端零命中（leaf 匹配取 FQCN 尾段）；属 check_sink_taxonomy_sync.py:31-33 自认的反向缺口盲区。
**【R-3】【低】** 自环判断对 from/to 双缺失形态报误导性错误（"" == "" 被当自环）。
**【R-4】【低】** 无约束拒绝的错误信息未给出用法 B 逃生路径（与自环拒绝信息不一致）。
**【R-5】【低】** E1 维持文件级聚合语义：同文件混合调用（null + perm）时 null 调用不报，候选 locations 固定 line=1——既有行为非新回归，但未在 rule.yaml limitations 声明。
**【R-6】【低】** §3.1 "仅影响候选加权（评分可能变化）"的"评分"消费路径无代码证据——实际链路是 dynamic: 前缀 → attack_surface 排除 → funnel NONE → finding 跳过。
**【R-7】【低】** `_split_top_level_args` 不处理引号内转义（与 `_matching_paren_end` 不一致），病态形态 `new Intent("a\"b,c")` 下漏报——既有边缘非新回归。
**【R-8】【低】** E1 测试只有 2 参形态，未覆盖 7/8 参版本（args[1] 恒为 receiverPermission）。

## 3. 认可项（节选）

1. E1 实现正确且旧正则执行代码零残留（`[^n][^u][^l][^l]` 仅存于 docstring/评审文档）；形态推演（嵌套逗号+null/权限串/常量/引号内逗号/未闭合括号/7-8 参）全部独立成立；
2. 旧漏报路径被测试真实覆盖（test_dataflow_multichain.py:1837-1855 正是旧正则失守形态；测试经真实索引 + FTS + 完整 execute 链路，非旁路）；
3. E2 双源一致、签名单参、账目自洽（73→74）、CI 同步测试在位（P1 移交 R-4 闭合确认）；
4. promote 加固逻辑位置与覆盖关系正确（自环拒绝在锚点入口、无约束拒绝在 promote 调用前；用法 B 完全不受影响；既有 4 条 manual 条目不触碰）；
5. 行为影响评估 §3.2/§3.3 准确；P1 移交项 ①② 均已实施（E2 的"先 dataflow 后 yaml"顺序约束被遵守）。

## 4. 边界检查表（子 agent 原文）

| 检查项 | 结论 |
|---|---|
| E1 正确性 | 通过（R-5/R-7 既有边界、R-8 测试缺口不阻断） |
| E2 核对 | 有条件通过（getLastLocation 死条目未披露——R-2） |
| promote 加固 | 有条件通过（零自动化测试 R-1 + 错误信息瑕疵 R-3/R-4） |
| 测试质量 | 有条件通过（promote 无测试、E1 缺 7/8 参形态） |
| 行为影响评估 | 通过（附 R-6 表述修正） |

---

## 5. 处置记录（主 agent 回填，2026-08-27）

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 中 | **采纳（已实施）**：test_sink_taxonomy.py 新增 3 条 CLI 测试——`test_promote_cli_rejects_self_loop_anchor`（自环拒绝，断言 exit≠0 + "自环"）、`test_promote_cli_rejects_unconstrained_entry`（反查失败 + 无约束拒绝）、`test_promote_cli_usage_b_still_appends`（用法 B 正向对照，断言 appended）；辅助 `_cli_run_dir` 构造最小 run 目录 | backend/tests/test_sink_taxonomy.py |
| R-2 | 中 | **采纳（已实施）**：versions.yaml 的 getLastLocation 条目 receiver 修正为 `leaves [FusedLocationProviderClient] + prefixes [com.google.android.gms.location.]`（消除死组合，FusedLocation 真实组合恢复消费端命中）；报告 §1.2 补披露段（含核验 R-2 修订标记） | versions.yaml、P2 报告 §1.2 |
| R-3 | 低 | **采纳（已实施）**：自环判断前显式判 from/to 缺失，双缺失报"链尾跳 from/to_method_id 缺失（锚点不可靠）" | promote_custom_sink.py |
| R-4 | 低 | **采纳（已实施）**：无约束拒绝错误信息补"或确认 receiver 与 sink 无关后改用 --method（用法 B）" | promote_custom_sink.py |
| R-5 | 低 | **采纳（短期方案）**：rule.yaml limitations 补文件级粒度声明（同文件混合调用只按带权限计 + locations 固定 line=1）；逐调用点枚举（finditer + 真实行号）记为后续迭代 | rules/signals/ORDERED_BROADCAST_UNRESTRICTED/rule.yaml |
| R-6 | 低 | **采纳（已实施）**：§3.1 措辞更正为"影响 rule-results 候选产物集合，不进 funnel/finding/确定性闭链（dynamic: 前缀 → attack_surface 排除 → funnel NONE → finding 跳过）" | P2 报告 §3.1 |
| R-7 | 低 | **采纳（已实施）**：`_split_top_level_args` 补 escaped 处理（与 `_matching_paren_end` 行为对齐，引号内 `\"` 不再提前关闭引号状态） | detector.py |
| R-8 | 低 | **采纳（已实施）**：新增 `test_ordered_broadcast_seven_arg_variant_permission_position`（7 参版本 null 报 / 权限串不报） | test_dataflow_multichain.py |

**闭合结论**：R-1~R-8 全部采纳并实施。核验后全量测试 **1281 passed / 0 failed**（1277 + 4 新增）、同步校验 **base 74 条：PASS 74 / CONFLICT 0 / ORPHAN 0**（getLastLocation 修正后 FusedLocation 探针命中，无 ORPHAN）。遗留：E1 逐调用点枚举（R-5 后半）→ 后续迭代；COVERAGE 粒度升级维持 P1 记录的中期方向。
