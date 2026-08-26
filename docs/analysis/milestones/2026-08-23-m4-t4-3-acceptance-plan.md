# 任务验收方案：M4-T4.3

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| A-1 | 分离检查 | 单测 | 键集无交叉 + provenance 合法 → PASS 项；非法 provenance → FAIL |
| A-2 | 引用回查 | 单测 | sources/sinks 的 path 空/line 非法 → WARN violations；正常 → PASS |
| A-3 | PoC 一致性 | 单测 | executable 非空 → FAIL；命令无占位符 → WARN；kind 映射错 → WARN |
| A-4 | verdict 聚合 | 单测 | FAIL > WARN > PASS 优先级 |
| A-5 | 容错 | 单测 | 缺键 document → 对应 violation（不抛） |
| A-6 | 真实产物 | 单测（真实 V-01 生成的 document） | 全 PASS |
| A-7 | 零回归 | 全量 pytest | 1225+ 全过 |

回退：新模块独立——revert 即回退。
