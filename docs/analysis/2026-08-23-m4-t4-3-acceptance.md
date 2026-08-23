# 任务验收记录：M4-T4.3（报告质量检查）

> **任务编号**：M4-T4.3
> **流程**：六阶段完整执行（评审 R-1~R-7 全采纳：删内嵌检查防自相矛盾/intent 映射加 other/映射检查降级为枚举/键集交叉定位回归锚点/27 键勘误/notes 检查/合成兜底用例）

## 验收结果

| 编号 | 结果 | 实测 |
|---|---|---|
| A-1 | 通过 | 分离检查：provenance 非法 → FAIL（test_illegal_provenance_fails）；ai_draft 缺失 → FAIL |
| A-2 | 通过 | path 空/line 非法 → WARN violations；line=None 容忍（真实产物口径——locations line 可 null） |
| A-3 | 通过 | executable 非空 → FAIL；具体命令（无占位符）→ 违规；kind/component 枚举；notes 关键词 |
| A-4 | 通过 | FAIL > WARN > PASS 聚合 |
| A-5 | 通过 | 缺键 → violation 不抛 |
| A-6 | 通过 | **真实 V-01 投影 document 全 PASS** + 合成 confirmed finding 兜底（CI skip 场景覆盖） |
| A-7 | 通过 | 全量 **1236 passed / 0 failed**（+11）；ruff 零错误 |

## 回归

全量 1236 passed / 0 failed。新模块 `backend/app/evaluation/report_quality.py` 独立可 revert。
