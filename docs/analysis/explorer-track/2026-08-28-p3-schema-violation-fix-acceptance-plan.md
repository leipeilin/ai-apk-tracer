# 任务验收方案：P-3 探索输出协议违规修复

> 对应实施方案：`2026-08-28-p3-schema-violation-fix-implementation-plan.md`
> **根因参照**：T1 error_detail 实证（schema_invalid 100%——string_too_long +
> value_error 两类；HTTP 200 排除网络/超时/限流）

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| P3-1 | reason 放宽（L1） | 单测（模型层 + schema） | reason 1500 字符通过（旧 256 拒）；Explorer **与 Verify** 两协议同步；**schema 变更 diff 校验**：仅 reason 字段 maxLength 256→2000（git diff 逐行确认其他字段零改动——P-1 误改 5 处教训） |
| P3-2 | 干净出口关键词集（L2） | 单测 | 中文（无敏感/未发现敏感）+ 英文小写变体（no sensitive/no security/none found）→ done+空链通过；偷懒 reason（"需要更多上下文"/"more context"）仍拒 |
| P3-3 | schema_invalid 轮级重试（L3） | 单测（FakeAnalyzer 首败后成） | 首次 schema_invalid + 二次 completed → 入口正常继续（不弃）；轮记录含 `schema_retry: true`；**stage 级 `ai_requests_used` = 首次 1 + 重试 1 = 2（叠加——评审 O-2）** |
| P3-4 | 重试耗尽原语义不变 | 单测（两次均 schema_invalid） | terminated_by=error（原路径）；无熔断触发（其他入口不受影响） |
| P3-5 | 非 schema_invalid 失败不重试 | 单测（transient 失败） | 网络类失败不进入轮级重试（仍走 transport 层既有重试——不叠加） |
| P3-6 | 零回归 + 存量测试适配 | 全量 pytest + sync --check | 1342+ 全过；**F5 干净出口存量测试断言随关键词集更新**（硬编码"无敏感"的用例同步适配——含协议断言与模型校验两类） |
| P3-7 | **T1 重跑（核心验收）** | 全量探针（**显式 `--max-entries 300` + 验证 selected=278**——评审补充：T1 实跑 198 因取样逻辑，重跑必须前置确认全量） | **error 率 50.5% → <10%**；候选产出显著回升（对照 T1 的 47）；四参数分布数据有效化；`redundant_done_rounds` 观测（L2 偷懒监控——评审 O-3：`none found` 为集内最宽词，若偷懒通过优先审视） |
| P3-8 | T2 golden 判决 | T1 重跑产物离线评估 | extra-close 命中判定 + 引导域产出（重跑数据干净后方可判） |
| P3-9 | 探针 error_detail | 已实证（本排查） | 复现批 7 失败轮全含详情（classification/违规字段）——随提交归档 |

## T1 重跑前置修正

- 探针 `_select_entries` 的 198 截断（kind 取样上限）→ 显式全量模式
  （`--max-entries 300` 已证可选 278——T1 实跑 198 因默认取样逻辑，重跑需确认参数传递）；
- T1 旧产物目录（20260827T163512Z）保留作对照基线（error 率 50.5% 的证据）。

## 验收原则

- P3-7 是本任务核心——**error 率不达标则 L1/L2/L3 修复无效**（需回到根因再排查）；
- P3-8 依赖 P3-7 的干净数据——golden 判决是 T1 的最终目的（探索轨定位分叉点）；
- 回退：L1（类型替换 revert）/L2（关键词集回单串）/L3（重试块删除）各自独立；
  schema 哈希随 sync 恢复。
