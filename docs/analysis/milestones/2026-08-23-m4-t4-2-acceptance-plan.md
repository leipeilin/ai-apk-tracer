# 任务验收方案：M4-T4.2

## 1. 验收点清单

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| A-1 | 探索命中率 | 合成 fixture 单测 | hit case 命中/未命中正确分类；hit_rate 分母仅 hit case |
| A-2 | conditional 单独报告 | 单测 | 不进 hit_rate 分母；conditional_hit_rate 独立 |
| A-3 | 容错 | 单测 | explorer/candidates.json 缺失 → proposals_total=0 + 指标 None（不抛） |
| A-4 | 三本账提取 | 合成 manifest 单测 | explorer/deep_dive/verify/ai_stage/total 五值正确；缺字段容错 |
| A-5 | wall-time | 单测 | completed-created 秒数；解析失败 None |
| A-6 | 聚合 | 双 run fixture 单测 | 平均 hit_rate + 总三本账 |
| A-7 | CLI --runs | 子进程冒烟（合成 run） | JSON 输出含全字段；退出码 0 |
| A-8 | 既有零回归 | 全量 pytest | 1216+ 全过（离线模式不变） |
| A-9 | 真实 run 冒烟 | shop dc24a077 真实产物 | 指标输出（hit_rate 预期 0——validated=0 期间的如实数据） |

## 2. 边界与负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | 无 golden 标注的 run 评估 | hit_total=0 → rate None |
| N-2 | manifest stages 缺 AI 阶段 | 三本账相关字段 None |
| N-3 | --runs 含不存在目录 | 明确错误退出码 2 |

## 3. 回退

新函数/CLI 独立入口——revert runner.py 改动即回退（离线模式不受影响）。
