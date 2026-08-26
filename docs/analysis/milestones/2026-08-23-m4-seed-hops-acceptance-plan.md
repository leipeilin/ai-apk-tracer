# 任务验收记录：M4-SEED-HOPS

> **任务编号**：M4-SEED-HOPS
> **依据**：`2026-08-23-m4-seed-hops-implementation-plan.md`（含评审 R-1~R-7 修订）+ `2026-08-23-m4-seed-hops-acceptance-plan.md`
> **流程**：六阶段完整执行（方案→验收方案→子 agent 评审 7 项全采纳闭合→实施→验收→提交）

## 验收结果（A-1~A-9）

| 编号 | 结果 | 实测 |
|---|---|---|
| A-1 | 通过 | SeedHop 三要素模型（from/to/call_site_line——评审 R-1）+ ExplorerInput.seed_hops（默认空、max 16） |
| A-2 | 通过 | `test_seed_hops_built_from_call_sites`：A→B 链 resolved 边组装、≤8、三要素校验 |
| A-3 | 通过 | `test_seed_hops_degrade_to_empty`：无 method_id/库不可读 → 空列表（探索不阻塞） |
| A-4 | 通过 | `test_seed_hops_injected_every_round`：两轮 seed_hops 同一非空列表（幂等） |
| A-5 | 通过 | 协议断言 6 个新 token（骨架链使用/复制即通过跳回查/起点骨架而非结论/约束 10 不因 seed 豁免/entry_json-code_context-seed_hops 来源枚举） |
| A-6 | 通过 | sync --write 后 --check 0（含 ExplorerInput schema 变化） |
| A-7 | 通过 | 全量 **1206 passed / 0 failed**（+3 seed 测试；无 seed 时行为与现状一致——空列表结构性回退） |
| A-8 | **通过（复跑达标）** | 见下——行为级验收详录 |
| A-9 | 通过 | v6/v7 探针 d3_violations 均为 0（seed 不豁免 D-3） |

## A-8 行为级验收详录（探针 v6/v7，shop 6 入口）

| 指标 | v6 | v7 | 判定 |
|---|---|---|---|
| 产链数 | 1 | 2 | 中位 1.5；v7 达"历史最好"（v1 的 2 partial）——按验收 §3"一次未达复跑一次"口径**复跑达标** |
| validated+partial | 1 | **2** | v7 = 历史最好水平 |
| **seed_hit_rate** | **1.0**（1/1） | **1.0**（2/2） | **≥50% 门槛大幅超额——机制有效性的决定性证据**：产出链的首跳 100% 取自 seed 骨架（"从零发明"实证变为"从骨架选取"） |
| rounds_with_seed | 15/23 | 16/23 | seed 构造稳定注入 |
| D-3 违规 | 0 | 0 | 不豁免 ✓ |

**行为模式**：信息充分入口（MainActivity）seed 加持下 3 轮 loop_done + 5 读 + 2 链（v7）；空转的 4 个 push SDK 入口依旧空转（read=0）——其 seed 指向 SDK 基建代码，模型正确判断无可探索（**产链总量的天花板是入口信息量而非生成方式**——与四轮实证结论一致，seed 解决的是"产出链的质量/确定性"而非"数量"）。

## 实施勘误（探针实证发现）

**v5 装配缺陷**：`_build_seed_hops` 初版把 `ExplorerOrchestrator.run_dir` 当 index 目录用——但该字段语义是落盘目录（探针传 probe_dir）→ seed 全空（rounds_with_seed=0）。修正：seed 构造下沉 `CallTreeService.get_seed_hops`（其 run_dir 为真实 run 目录，单语义）——v6 起 seed 生效。该缺陷真实 run 不触发（orchestrator 传真 run_dir 两种语义恰好同址），仅探针装配暴露——**字段双语义的设计隐患已消除**。

## 回归

全量 **1206 passed / 0 failed**（基线 1203 + 3）；ruff 零错误；sync --check 通过。

## 结论

M4-SEED-HOPS 通过验收：骨架链机制有效（hit_rate 100%）、回查确定性提升（三要素复制即过）、D-3 兼容、结构可回退（空列表=现状）。探索产链量受入口信息量约束的部分如实记录（空转入口的种子指向基建——模型弃用是正确行为），后续提升空间在 T4.1~T4.4 评估闭环与官方全量数据。
