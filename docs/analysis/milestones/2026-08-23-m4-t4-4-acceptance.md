# 任务验收记录：M4-T4.4（优化门槛）

> **任务编号**：M4-T4.4
> **流程**：六阶段完整执行（评审 R-1~R-6 全采纳：白名单点路径/真实输出 fixture 防误入 by_category/规则轨快照命令/结构校验与"先刷基线"硬性步骤/文档命令实录断言/浮点边界与 None 用例）

## 验收结果

| 编号 | 结果 | 实测 |
|---|---|---|
| A-1 | 通过 | 5 白名单指标对比：劣化 BLOCK+deficit、持平/提升 ALLOW |
| A-2 | 通过 | 容差边界（=tol → ALLOW，严格小于才 BLOCK）+ 浮点尾差 round(10) |
| A-3 | 通过 | baseline 缺 → SKIP（守卫回归不守卫演进）；current 缺 → BLOCK |
| A-4 | 通过 | 嵌套点路径取值；结构混用（键集全不交）→ BLOCK+明确 reason |
| A-5 | 通过 | CLI 退出码 0/1（项目惯例）+ `--tolerance f1=0.02` 短名覆写 |
| A-6 | 通过 | `docs/evaluation-workflow.md` 四节 + 命令实录断言（--runs/--results/gate） |
| A-7 | 通过 | 全量 **1248 passed / 0 failed**（+12）；ruff 零错误 |

## 附加验证

by_category 劣化值（0.5→0.1）不参与判定（白名单不含——R-2 防误报实证）。

## 回归

全量 1248 passed / 0 failed。gate.py + 文档独立可 revert。
