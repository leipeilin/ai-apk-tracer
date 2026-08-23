# M2 验证收尾与 M3 推进指引

> **日期**：2026-08-23
> **性质**：指导性文档。约束后续 M2 收尾与 M3 启动的执行顺序、方法与验收门槛。
> **前置状态**：最新三提交（`d2f6ed3` / `ea332ee` / `aa73291`）已闭合代码缺陷与机械链路验收；M2 质量验收仍待完成。
>
> **执行进展（2026-08-23 回填）**：
> - §3.1 shop 验收记录回填 ✅（m2-acceptance-runs.md §2.2——run manifest 口径全指标回填）；
> - §3.2 DEFECT-FIX 状态标注 ✅（已于 aa73291 完成）；
> - §4.1 `probe_explorer_entry.py` ✅ 已落地并真实探针（shop 6 入口 117s）：**D-3 行为级验证通过**（18 个无上下文轮零产链）；validated=0/partial=2/unverified=1（差 1 达门槛）；**新发现：4 个 service/receiver 入口空转**（read_req=0、零候选、4 轮耗尽——模型既不读码也不产链，全量跑不可见的模式，prompt 下一迭代点：每轮必须 read_requests 或 done）；
> - §4.2 `probe_verify_entry.py` ✅ 已落地并真实探针（shop 3 L2 候选）：**根因精确定位**——3/3 `schema_invalid` 同源确认，`initial_validation_errors`：`claims_verdicts.N.kind/verdict: extra_forbidden`（模型输出被禁字段）+ `conclusion: missing`（schema 要求字段缺失）+ 顶层 `summary/confidence_tier/exploitability/refutation_basis` 缺失——修复路径 = EXPLORER-PROMPT-FIX 同款 verify prompt 严格契约重写（M2 收尾-2 输入就绪）；
> - §6 M3 调研 ✅ 方案就绪（字段设计/落点 `backend/app/reporting/`/provider 抽象取舍/2 个真实 confirmed finding 路径已核实）——实施待续。
>
> **执行进展（2026-08-23 第二轮回填——M2 收尾-2/空转修复）**：
> - **M2 收尾-2 verify prompt 严格契约重写 ✅ 行为级验证通过**：探针 v2 **PASS**（3/3 completed、fallback 3→0、schema_invalid 清零、聚合层证据回查通过、58s）——52+29 全 fallback 根因彻底修复；
> - explorer 空转修复（硬约束 11）**未达预期，按 §9 预案上升用户决策**：三轮探针产链 3→0→1 波动（v1 无约束 3 链/v2 done-链绑定 0 链/v3 松绑 1 链），门槛（≥3）未达，4 入口空转依旧；D-3 三轮全部 100% 遵守；
> - **意外收获——驱动层真实缺陷修复**：探针 v3 触发 `code_context` 跨轮累积超 ExplorerInput LongText 10000 上限（ExplorerInput 构造直接 ValidationError 崩溃——deep_dive 有 9500 上限先例而 explore 无防御）——已加 `_MAX_EXPLORE_CONTEXT_CHARS=9500` 保头部截断；
> - M3-1 方案落盘 ✅（`2026-08-23-m3-report-poc-implementation-plan.md`——子 agent 调研蓝图完整固化）——实施待执行。

---

## 1. 现状定论

1. **M2 机械链路已验收通过**：探索轨全链路（抛错入口 → attack_surface/api_surface → call_tree → 探索检索循环 → 三档校验 → 归一化/funnel → 核验分流/回退 → 人工队列）在健康真实 run 上端到端跑通。
2. **M2 质量验收未达标**：
   - health：validated=0 / partially_validated=1 / unverified=49；
   - shop：validated=0 / partially_validated=4 / unverified=46；
   - 因此 §4.3“覆盖 ≥5 validated”与“8 项成立漏洞覆盖映射表”均无法勾选。
3. **verify 质量不达标**：真实 run 中 health 52/52、shop 29/29 候选全部 fallback；主链未阻塞（降级机制生效），但核验 agent 未跑通。
4. **根因已定位且部分已修复**：
   - Explorer schema_invalid → `EXPLORER-PROMPT-FIX` 重写 prompt 严格契约；
   - Explorer 首轮无上下文产链导致 hops 不可回查 → `M2-DEFECT-FIX D-3` 增加“禁止无据产链”；
   - Manifest 解码与 AI 请求长挂起 → `M2-DEFECT-FIX D-1/D-2` 已加超时兜底。
5. **做法教训**：整包全量跑“每轮几十分钟”不适合 AI 输出质量迭代。后续验证必须降维为**可复用的定向 harness + 小样本**，只在最终官验时做一次全量。

---

## 2. 后续推进总原则

1. **不再默认全量重跑**：AI 质量迭代用现有 run 产物做分钟级定向验证。
2. **M2 只做“定向验证 + 最后一次官方全量验收”**；M3 不等待 M2 完成，并行启动。
3. **所有阶段性修复先经过 harness 再进全量验收**：harness 不达标的修复不进入官方全量。
4. **如实记录**：质量项未达标就明确未达标，不宣称 M2 整体验收通过；M3 独立验收，不混淆 M2 指标。

---

## 3. 立即执行的零成本项

### 3.1 回填 shop 验收记录

- 文档：`docs/analysis/2026-08-23-m2-acceptance-runs.md` §2.2
- 依据 run：`20260822T210017Z_1c55d3fb9f95_dc24a077`
- 应回填内容：

| 指标 | 实测 |
|---|---|
| run status | completed |
| 探索候选 | 50（= max_candidates_per_run 上限） |
| 三档校验 | validated=0 / partially_validated=4 / unverified=46 |
| findings | 151（= M1 基线 = M2 默认） |
| 三本账/核验 | ai_analysis `requests_used` 与 `verify_counts` 按 run_manifest 回填 |

### 3.2 更新 M2-DEFECT-FIX 验收记录状态

- 文档：`docs/analysis/2026-08-23-m2-acceptance-runs.md` §4
- 明确标注 4/5 已修复状态与 1 项待行为级验证告警（若未完整标注）。

---

## 4. 定向验证 harness（关键基础设施）

> 目标：把“改 prompt → 验证”的循环从每轮 40-60 分钟降到分钟级。

### 4.1 `scripts/probe_explorer_entry.py`

**输入**：已跑完的真实 run_dir（不重新反编译/规则/索引）。

- 复用 `run_dir/api-surface/api_entry_table.json`；
- 复用 `CallTreeService`（run_dir + `SQLiteCodeIndexReader`）；
- 调用正式 registry 路径的 `OpenAICompatibleAnalyzer.explore_entry()`；
- 可选参数：
  - `--run-id`：读取 run 产物；
  - `--entries`：入口子集（默认异构取 activity/service/receiver/provider 各 2-3 个）；
  - `--max-entries`：总入口上限（默认 8）；
- 输出：每个入口每轮 `status` / `rounds` / `read_requests` / `chain_proposals` / 候选 `validation` 计数。

**判定阈值（harness 门槛）**：

- 10 个异构入口中，`validated/partially_validated` ≥ 5；
- 所有入口第一轮不存在“无 code_context 产链”违规（机器断言：`code_context is None` 时读回 `chain_proposals` 必须为空）。

### 4.2 `scripts/probe_verify_entry.py`

**输入**：已跑完的真实 run 中 L2 候选 + 已切片产物。

- 从 `slices/` 或候选映射取 N=2-5 个真实 L2 候选；
- 调用正式 `VerifyAgent.verify()`；
- 记录：`status` / `terminated_by` / `fallback` / 证据引用是否被聚合层回查通过。

**判定阈值（harness 门槛）**：

- N=5 中 `completed` ≥ 1；
- `schema_invalid` 必须归因清楚（大概率与 explorer 同源 → 修改 verify prompt strict 契约后复验）。

> 备注：harness 脚本统一默认走真实 AI；CI/快速回归可加 `--dry-run`（只构造输入不调用 AI）。

---

## 5. M2 收尾路径

```text
shop 验收记录回填
  → probe_explorer_entry（对 D-3 修复做行为级验证）
  → probe_verify_entry（定位 verify schema_invalid 同源问题）
  → verify prompt 修复 + harness 复验
  → explorer/verify 双脚本均达标后
  → 官方全会运行（health/shop 各一次）
  → 回填 §4.3 质量 checkbox + m2-acceptance-runs.md
```

官方全量验收的进入条件（缺一不可）：

1. `sync-ai-protocol.py --check` 通过；
2. 全量 pytest 通过；
3. `probe_explorer_entry` 达到门槛；
4. `probe_verify_entry` 达到门槛。

---

## 6. M3 并行启动

### 6.1 M3 入口与范围

- 任务来源：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §4.4；
- 交付物：报告生成 + PoC 骨架 + 修复建议，默认 `allow_executable_poc=false`、`require_confirmed_finding=true`；
- 不依赖 M2 质量验收，依赖已有 confirmed finding。

### 6.2 最小闭环

- 选 2 个已确认 finding（建议 health `v_01`/`v_02` 对应的 finding，或 shop `v_03` 对应 finding）；
- 生成 `ReportDraft` + PoC 骨架，人工检查：
  - 字段完整；
  - 全部代码引用可回查；
  - AI 草稿与确定性证据分开展示；
  - 默认不产出任何可执行文件。
- 验收按 M3 专项标准执行，结论独立于 M2 质量验收。

### 6.3 M3 与 M2 的同步点

- M3 生成的报告可继续沿用 M2 的 caveat“explorer_validated=0 期间，探索 quality 未达标”——报告需标注证据来源（规则候选 vs exploration 候选）。
- 若 M2 官方全量产出 validated 候选，M3 的数据源直接升级，不需要改 M3 结构。

---

## 7. 后续建议提交顺序与验收门槛

| 阶段 | 提交内容 | 验收门槛 |
|---|---|---|
| M2 收尾-1 | shop 验收记录回填 + harness probe_explorer_entry | 文档 + pytest + harness 门槛 |
| M2 收尾-2 | verify prompt 对齐修复（若确认同源 schema_invalid） | harness（`probe_verify_entry` 门槛） |
| M2 收尾-3 | 官方双 APK 全会验收（最后一次全量） | §4.3 质量项达成或如实未达成 |
| M3-1 | 报告生成 + PoC 骨架 + 修复建议实现 | M3 专项验收 |
| M4-1 | golden 扩展 + 指标闭环（可并行准备） | 参考 §4.5 |

---

## 8. 决定不做的

- **不用整包全量跑来做 prompt 迭代**；
- **不继续用一次性 shell 命令替代入库 harness**；
- **不在 M2 质量项未闭环时宣称 M2 整体通过**；
- **不阻塞 M3 等待 M2 质量项**。

---

## 9. 风险与应对

| 风险 | 应对 |
|---|---|
| harness 使用 run 产物与官方全量口径不一致 | 让 harness 复用与 orchestrator 相同的 `CallTreeService` / `explorer` / `validator` 入口代码 |
| verify 问题不是 prompt 同源 | harness 会暴露 `_invoke_prompt` failure 分类；分支处理后再写方案 |
| D-3 补丁后行为级仍不佳 | 立即停止全量，继续在 harness 上迭代 prompt；若 2 轮 harness 未达标，上升为用户决策 |
| M3 因报告协议设计不清晰而拖期 | M3 先做最小闭环，不铺完整 UI；UI 后置 |