# 任务验收方案：T1.6（batch 预算降级测试与迁移测试——收尾核查）

> **任务编号**：T1.6
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t1-6-implementation-plan.md`（核查报告，评审后修订版）
> **状态**：已闭合
> **验收方式**：独立复核（子 agent 代码级核验）+ 补充测试 + 全量回归

---

## 1. 验收范围

- 核查报告结论的正确性（独立复核）；R-1 缺陷修复 + 2 项补充测试；T1.5 手工清单补录。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式 | 结果 |
|---|---|---|---|
| A-1 | 核查报告独立复核 | 子 agent 逐条打开测试文件核验断言实质（非仅名字） | 完成——发现 R-1 高危缺口（报告初判被推翻） |
| A-2 | R-1 修复：orchestrator 消费 run config ai 段 | 代码审查 + `test_batch_real_pipeline_degradation`（修复前该断言必失败） | 通过 |
| A-3 | 真实 pipeline 降级（3 资产真实 jadx 链路） | 同上：run1 正常 / run2/3 墙钟降级（ai_analysis=skipped + reason 含"batch 预算/墙钟降级" + requests_used=0）+ 全 completed + 资产 ready 联动 | 通过 |
| A-4 | =1 字面预算用例 | `test_run_batch_budget_degradation_cap_one`：[0,1,1] 连续降级 + 批次继续 + ai_skipped=2 | 通过 |
| A-5 | 迁移测试矩阵（既有前置覆盖） | 复核确认 v1 legacy/v3 叠加/v4→v5/中断幂等/大表/新库/FK/UNIQUE 全部断言实质 | 通过（无新增） |
| A-6 | 单测通过 | `pytest tests/test_batch.py -q` | 17 项全过（15+2） |
| A-7 | 全量回归 | `pytest -q` | **955 passed / 0 failed**（953+2） |
| A-8 | 统一校验 | `scripts/check-all.sh` | 通过（tsc+build） |
| A-9 | ruff（新代码） | 改动文件检查 | 通过（orchestrator 剩余 BLE001 为既有债务，非本次引入） |

## 3. 方案 Phase 1 验收清单终账（L158-164 逐条）

| 方案验收条目 | 终态证据 | 结论 |
|---|---|---|
| 3 APK 导入批量扫描、独立 run、按批次汇总（L160） | `test_run_batch_full_flow`（协议层）+ `test_batch_real_pipeline_degradation`（3 资产真实 jadx 链路）+ T1.5 手工端到端 | **达成** |
| 单 APK run 行为一致（L161） | 全量 955 passed（含全部既有 run 测试）+ config golden 断言 | **达成** |
| 并发上限、失败可单独重跑（L162） | `test_run_batch_concurrency_limit`（峰值=1）；重跑=失败资产子集新建 batch（语义承载 + T1.5 手工清单补录重跑项） | **达成** |
| max_ai_calls 降级 + 标记可见 + 汇总可审计（L163） | `test_run_batch_budget_degradation`（=2）+ `_cap_one`（=1 字面）+ 真实降级用例（R-1 修复后 AI 真实跳过）+ 分解计数 + 前端徽标 | **达成**（经 R-1 修复） |
| 旧库迁移升级完好（L164） | 迁移矩阵全部断言实质（复核确认） | **达成** |

## 4. T1.5 手工清单补录（评审 R-5）

| 项 | 操作 | 预期 |
|---|---|---|
| 失败资产子集重跑 | 构造含失败资产的批次（如损坏 APK）→ 对 error 资产重新多选发起 | 新批次仅含该资产、独立完成（语义承载验证） |

> 该项为语义路径复用（create_batch 子集已自动化覆盖），手工核验为闭环性补录。

## 5. 回归标准

- [x] 修复不改变既有 run 行为（ai_enabled 默认 True，无标记 run 走原路径）
- [x] 全量 955 passed / 0 failed
- [x] check-all + ruff（新代码）通过

## 6. 验收记录

全部验收点通过（A-1~A-9 见上表"结果"列）。M1 六任务闭环。
