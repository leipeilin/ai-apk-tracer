# 任务评审报告：M4-SEED-HOPS

> **评审对象**：`2026-08-23-m4-seed-hops-implementation-plan.md`、`2026-08-23-m4-seed-hops-acceptance-plan.md`
> **评审日期**：2026-08-23
> **评审模型**：deepseek-v4-flash（独立只读子 agent）
> **状态**：第 1 轮已闭合（R-1~R-7 全部采纳）

## 1. 评审结论摘要

方案的数据源判断与注入模式选择基本成立（get_callees 确含 method_id、attack_surface_json 先例可复用、输出契约零改动），测试与 registry 门禁链可判定。但核心机制存在一处事实性错误：seed 不含 call_site_line，"seed 起点的 hop 天然通过回查"不成立——回查失败点被整体推迟到 call_site_line，与本任务要修的 validated=0 可能同源未解。验收侧 A-8 的 seed 命中率指标在现有探针上不可计算。

## 2. 问题清单与处置记录

| 编号 | 严重度 | 问题摘要 | 处置 | 修订动作 |
|---|---|---|---|---|
| R-1 | 关键 | seed 缺 call_site_line——`_verify_hops` 要求 (method_id, start_line) 命中 resolved 边，模型从无行号方法体数行推算正是 line_mismatch 已知失败模式；"天然通过回查"是事实性错误 | **采纳** | SeedHop 增加 `call_site_line`；驱动层直查 `call_sites`（`SELECT start_line, resolved_target_id ... resolve_status='resolved' ORDER BY start_line LIMIT 8`——含行号且确定序，R-6 一并解决）；约束 12 修正为三要素（from/to/call_site_line）直接可用（回查必过——真正确定性） |
| R-2 | 高 | A-8"第一跳命中率 ≥50%"无载体：探针 round_probes 不记 seed、方案变更清单无探针改动 | **采纳** | 变更清单加 probe_explorer_entry.py：wrapper 捕获 model_input.seed_hops 与产链首跳 from/to，summary 加 seed_hit_rate |
| R-3 | 高 | 前置依赖引用失实：指引 §9 实为风险应对表，全文无"seed"字样——任务授权链断裂 | **采纳** | 引用改为四轮探针实证记录（guidance 执行进展两轮回填 + 2026-08-23 与用户的稳定修复分析结论——出处为对话实证与 m2-acceptance-runs §4① 根因链） |
| R-4 | 高 | 约束 12 与约束 4/10 冲突：4/10 封闭枚举上下文来源为 entry_json/code_context，12 却称"直接可用"；D-3 首轮禁链下 seed 不可能催生首轮产链（未言明） | **采纳** | R-1 修复后三要素全确定性（"直接可用"成立）；约束 4 的可回查事实来源枚举补 seed_hops；约束 12 注明 D-3 不豁免（无上下文仍禁产链——seed 是方向指引与首跳素材） |
| R-5 | 中 | 验收达标线（≥2）与探针出口判定（threshold=3 时 exit 1）不一致 | **采纳** | 验收方案 §3 注明判定读 summary JSON 的 validation_counts/产链数与 seed_hit_rate，不以退出码为验收口径 |
| R-6 | 中 | "前 8 个"无确定序（_method_summaries 无 ORDER BY，ordinal 在 summary 层丢失） | **采纳** | R-1 的直查 SQL 按 start_line 排序——确定序 |
| R-7 | 低 | N=8 与 max_length=16 不一致；"_build_seed_hopes" 拼写；N-2 需 ≥9 callees fixture | **采纳** | N=8 为构造截断、schema 上限 16 留余量（方案注明）；拼写修；变更清单标注 fixture 新增 |

## 3. 认可项（摘）

1. 数据源核验正确：get_callees 的 callees 确含 method_id，seed 构造可行。
2. 注入落点准确：attack_surface_json 先例 + user.md 整体 JSON 渲染——加字段即自动进 prompt，零模板改动。
3. 输出契约零改动定位准确（回退开关 = 空列表结构性成立）。
4. registry 门禁链正确（sync 从 AI_SCHEMA_MODELS 生成 schema + pytest --check 门禁）。
5. 测试基建可行（_service 真实 A→B→C 链 + FakeAnalyzer 捕获 inputs；extra=forbid 对 default_factory 新字段向后兼容）。

## 4. 闭合结论

R-1~R-7 全部采纳，修订已合入实施方案（SeedHop 三要素/直查 SQL/约束 4+12 重写/探针增强/引用修正/口径统一）。进入实施。
