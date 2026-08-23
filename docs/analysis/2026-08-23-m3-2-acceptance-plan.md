# 任务验收方案：M3-2

> **任务编号**：M3-2

## 1. 验收点清单

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| A-1 | 模型注册 | 单测（AI_MODEL_REGISTRY/AI_SCHEMA_MODELS） | ReportInput/ReportDraftOutput 在册 |
| A-2 | registry 条目 | sync --write 后 --check | 0；prompt_version 声明对齐（config） |
| A-3 | prompt 严格契约 | 协议断言测试 | 只输出一个 JSON/字段逐字/禁附加/provenance 输入说明（explorer 种子低信任） |
| A-4 | report_entry 状态机 | fake transport 测试（照 verify_entry 模式） | completed/failed/skipped 语义一致 |
| A-5 | 真 provider 成功路径 | fake analyzer | provenance=ai_report_protocol、字段桥接正确 |
| A-6 | 降级回退 | fake analyzer 失败 | provenance=projected_from_l2_review、报告不阻塞 |
| A-7 | 大纲 T3.2 种子 | 单测 | explorer 来源 finding 的 ReportInput 含假设三字段；规则轨 None |
| A-8 | 真实端到端 | V-01 真实 AI 调用 | ReportDraft 生成（provenance=ai_report_protocol）；失败则验证降级路径并如实记录 |
| A-9 | 既有零回归 | 全量 pytest | 1210+ 全过（routes/provider None 模式不变） |

## 2. 边界与负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | AI 输出缺字段 | schema_invalid → repair → 仍失败 → 降级投影（A-6） |
| N-2 | analyzer=None | 投影模式（M3-1 现状行为） |
| N-3 | 规则轨 finding（无假设字段） | 种子三字段 None、prompt 正常 |

## 3. 回退

provider 传 None（投影模式）；prompt/registry revert + sync 重写。
