# 任务验收记录：M3-2（report prompt 协议）

> **任务编号**：M3-2
> **依据**：`2026-08-23-m3-2-implementation-plan.md`（含评审 R-1~R-10 修订）+ acceptance-plan
> **流程**：六阶段完整执行

## 验收结果

| 编号 | 结果 | 实测 |
|---|---|---|
| A-1 | 通过 | 三 dict 注册（AI_MODEL_REGISTRY/AI_OUTPUT_MODEL_REGISTRY/AI_SCHEMA_MODELS——评审 R-3 扩展）+ RepairInput 枚举含 ReportDraftOutput（R-2） |
| A-2 | 通过 | registry 手工条目 + sync --write 哈希/schema 生成；--check 0；config 三方同步（config.py/default.yaml/config.schema.json——R-8） |
| A-3 | 通过 | 严格契约断言（叙述基于输入事实/低信任种子声明/不得虚构/顶层字段清单）+ `_prompt_variable("report")=="report_input_json"` 惯例（R-10b） |
| A-4 | 通过 | report_entry 照抄 verify_entry 状态机（缓存 no-op docstring——R-10c） |
| A-5 | 通过 | 真 provider 成功：provenance=ai_report_protocol + prompt_version/model 从 metadata 回填 + evidence_refs 确定性补齐（R-5） |
| A-6 | 通过 | 降级：schema_invalid → projected_from_l2_review + fallback 可观测（classification/message——R-9）；registry 缺条目时真实端到端曾实证降级不阻塞 |
| A-7 | 如实记录（评审 R-1 缓期） | 规则轨 finding 种子三字段 None（负例断言过）；**种子数据缓期**——归一化层不透传假设层原文，接口就绪（字段存在即透传）；大纲 T3.2 真正兑现须先做归一化透传（记 M4 后续项） |
| **A-8** | **通过** | **真实 V-01 真协议端到端**：`provenance=ai_report_protocol`、`prompt_version=1.0.0`、`model=deepseek-v4-pro-0813`、零 fallback——首次调用即过 schema（严格契约前置生效——verify 教训的价值）；叙述全锚定输入事实（"确定性事实显示 defpackage/v5e.java:213"）且诚实标注未确认项（"dispatch 目标未解析、效果未验证"） |
| A-9 | 通过 | 全量 **1216 passed / 0 failed**（+6）；API 测试补 ai_runtime fake；ruff 零错误 |

## 实施勘误

- 首次真实端到端降级（prompt_registry_invalid）：**sync 脚本不发现新 prompt**——registry 条目须手工写入（sync 只生成哈希/schema）。已补条目并 sync 通过。
- `deterministic_summary` 拼接排除 `finding.description`（L2 AI 文本隔离——R-6，测试含隔离探针断言）。

## 大纲回写事项

- 落点偏差（reporting/ 替代 findings/report_generator.py）与 T3.2 种子缓期（归一化透传为前置）——随 M4 后续任务处理大纲文档。

## 回归

全量 1216 passed / 0 failed；sync --check 通过；ruff 零错误。routes 生产接线（R-4：`ai_runtime.create_analyzer()`——共享 transport）。
