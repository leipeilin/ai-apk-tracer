# 任务验收方案：M4-T4.4

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| A-1 | 指标发现与对比 | 单测 | rate/precision/recall/f1 键自动发现；劣化 → BLOCK+deficit；提升/持平 → ALLOW |
| A-2 | 容差 | 单测 | tol 内劣化 → ALLOW；超 tol → BLOCK |
| A-3 | None/缺失语义 | 单测 | baseline 缺 → SKIP；current 缺 → BLOCK |
| A-4 | 嵌套取值 | 单测 | current 的 aggregate 嵌套与顶层均可对比（取值函数支持点路径） |
| A-5 | CLI | 子进程/直接调 main | --current/--baseline JSON 输出 gate；退出码 0=ALLOW 1=BLOCK |
| A-6 | 流程文档 | 文件存在性+内容断言 | docs/evaluation-workflow.md 含基线快照命令/门槛命令/默认开启检查点三节 |
| A-7 | 零回归 | 全量 pytest | 1236+ 全过 |

回退：gate.py 独立 + 文档——revert 即回退。
