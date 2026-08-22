# 任务实施方案：M2 审查意见闭合（双 APK 探索轨验收 + 基线/测试/文档补齐）

> **任务编号**：M2-ACCEPTANCE-CLOSURE
> **日期**：2026-08-22
> **依据**：`docs/analysis/2026-08-22-m2-implementation-review.md` 审查意见 4.1–4.5；`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §4.3 M2 专项验收（三加一口径）。
> **用户决策**：完整补跑 health/shop 双 APK 探索轨验收（真实 AI）。
> **当前基线**：HEAD `9cbec2e`，全量 pytest 1147 passed。

---

## 1. 任务目标与范围

- **目标**：闭合 M2 审查报告 §4.1–§4.5，使 M2 里程碑验收有真实双 APK 数据与可审计记录。
- **范围（in scope）**：
  1. 补 `backend/tests/test_no_rules_import.py`（backend/app 无 `import rules` / `from rules` 的源码 AST 扫描断言）。
  2. 在 HEAD `9cbec2e` 上对 health/shop 各跑一次**默认配置** run，执行 `scripts/baseline-manifest.py` 与 M1 基线对比，记录“默认配置产物 diff 为空”。
  3. 对 health/shop 各跑一次**探索轨 + 核验轨** run（`api_surface.enabled=true`、`explorer.enabled=true`、`verify.enabled=true`），收集 §4.3 三加一口径全部指标。
  4. 产出 `docs/analysis/2026-08-22-m2-acceptance-runs.md` 记录双 APK run_id、指标、结论。
  5. 更新 `docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §4.3 checkbox 勾选状态与归属说明。
  6. 更新 `docs/analysis/2026-08-22-m2-implementation-review.md` 增加处置记录；更新 `docs/analysis/2026-08-22-t2-12-implementation-plan.md` 的“交接 M2 验收”行，明确由本任务执行。
- **非范围（out of scope）**：不修改探索轨/核验轨核心实现（除非验收发现缺陷）；不开启默认配置开关；不做 M3/M4 报告与 golden 扩展。

---

## 2. 详细实现方案

### 2.1 新增源码扫描测试

新增 `backend/tests/test_no_rules_import.py`：

- 遍历 `backend/app/**/*.py`，用 `ast.parse` 解析源码；
- 仅检测 `ast.Import` / `ast.ImportFrom` 中模块名为 `rules` 或 `rules.*` 的节点；
- 不把注释、字符串、docstring 中的“import rules”字样视为违规（`backend/app/analysis/sink_taxonomy.py` 的 docstring 含说明文字，必须豁免）；
- 失败信息列出违规文件与 AST 行号。

> 说明：这实现了评审要求的“grep 断言写进测试”红线，同时避免文本子串误伤注释/文档。

### 2.2 默认配置基线 diff

步骤：

1. **AI 环境前提**：默认配置 run 必须与 M1 基线同一模型、同一 API key 可用环境（`ai.enabled=true` + 真实 AI）；若 AI 不可用，该 run 单独标注“AI 不可用基线”，不得与 M1 基线直接判一致。
2. 使用默认配置启动后端（不设置 `AI_APK_TRACER_EXPLORER__ENABLED` / `API_SURFACE__ENABLED` / `VERIFY__ENABLED`）；
3. 用 `POST /api/runs`（`authorized=true`）分别上传 M1 基线相同 sha256 的 health/shop APK；
4. 等待 run `status=completed`；
5. 对每个新 run 执行：
   ```sh
   python3 scripts/baseline-manifest.py <new_run_id> .ai-apk-tracer/baselines/m2-default-<apk>.json
   ```
6. 与 `.ai-apk-tracer/baselines/m1-<apk>-baseline.json` 对比：
   - 文件集合一致；
   - 逐文件 sha256 一致；
   - `findings_count` 一致；
   - 聚合哈希一致。
7. **判定规则**：`analysis.sqlite3` 字节级噪声经 `sqlite3 dump` 复核内容一致 → 通过；文件集合/逐文件 sha256/findings_count/聚合哈希任一实质差异 → 验收不通过，按 §4 逐项归因记录。
8. 记录 diff 结果（文件数、聚合哈希、findings_count）。

### 2.3 探索轨 + 核验轨双 APK run

启动后端时设置环境变量：

```sh
AI_APK_TRACER_API_SURFACE__ENABLED=true
AI_APK_TRACER_EXPLORER__ENABLED=true
AI_APK_TRACER_VERIFY__ENABLED=true
```

其余配置保持默认（含探索预算、verify fallback=true）。

对 health/shop 各创建 run（`POST /api/runs`，`authorized=true`），等待完成。

### 2.4 指标采集

从 run manifest 与产物中采集：

| §4.3 项 | 数据源 |
|---|---|
| 覆盖：validated/partially_validated 候选数 | `manifest.json` stages explorer.summary（validation_counts/normalization_counts） + `explorer/candidates.json` |
| 覆盖：已知 8 项命中 | 依据 `docs/analysis/2026-08-16-vulnerability-discovery-success-plan.md` 列出的 8 项成立漏洞，在 `m2-acceptance-runs.md` 内嵌映射表（source 组件 + sink 类/方法名），由候选与最终 finding 逐项比对 |
| 负样本不出现在候选池 | 在 `m2-acceptance-runs.md` 内嵌 5 类负样本判别键（V-04/V-05/V-06、`sp-control-flow-cooccurrence-refuted`（即 shop 140）、`ownsystem-unselected-implementation`），检查 `explorer/candidates.json` 与最终 findings |
| 未通过校验 0 条进正式 finding | `explorer/candidates.json` unverified 集合与 findings 集合求交，断言为空 |
| unverified 不占 AI 预算 | 从 `explorer/candidates.json` 取 `validation.status=unverified` 的 candidate_id 集合，断言：①不出现在任何候选 `deep_dive` 对象中；②不出现在归一化候选/最终 findings 的 `explorer_candidate_id` 中；③`normalization_counts.unverified_kept` 与原始 unverified 数量一致。总计数仅作旁证 |
| 成本：三本账 | `stages[].name=explorer` 的 `summary.deep_dive_requests_used`；`stages[].name=ai_analysis` 的 `summary.{explorer_requests_used, ai_stage_requests_used, verify_requests_used}`；分组/换算：探索 = `explorer_requests_used`；复核 = `ai_stage_requests_used - verify_requests_used + deep_dive_requests_used`；核验 = `verify_requests_used` |
| 性能：call_tree 单入口 ≤2s | 用 `backend/.venv/bin/python` 加载 `CallTreeService` 对 health 代表入口执行 `get_callees/get_method_body` 计时，记录多入口 p50/max；p50 >2s 视为未达标并记录优化方向 |
| 回归：默认配置 diff 为空 | 2.2 结果 |
| 检索循环预算 | `explorer/observations.json`：单入口 read_requests = 该 entry 所有 round 的 `len(requests_executed)` 之和；断言 rounds ≤ `max_rounds_per_entry`（4）、read_requests ≤ `max_requests_per_entry`（20） |
| 预算跑满不报错 | `observations.json` 中所有 `terminated_by=budget` 的 entry 对应 rounds 非空，run `status=completed`；即使零候选也记录“预算耗尽 + 缺口原因”，不允许阶段异常/run 失败 |
| 伪造 method_id 判 unverified | 既有单测（`backend/tests/test_explorer_validation.py`）覆盖，记录测试名 |
| deep_dive 不改写链 | 既有单测覆盖，记录测试名 |
| backend 无 import rules | 2.1 测试 |
| verify 盲验/命题/循环语义 | 盲验剥离与命题一致性以既有单测为准（`backend/tests/test_verify_agent.py` 具体用例名）；run 内 trace 抽查仅限 `verify/observations.json` 元数据（轮数/terminated_by/input_hash/undecided），不声称从产物可证明盲验剥离 |
| verify 降级回退 | 既有单测覆盖；真实 run 若出现 fallback 记录原因，否则记录“未触发” |
| 证据引用适配 | 既有 DecisionEngine 端到端测试 A-6 |
| custom sink 命中/误标 | 从 `explorer/candidates.json` 统计 `validation.custom_sink_proposal=true` 数量、深挖跳过数量、人工确认/promote 数量、与真实 APK 语义不符的误标数量 |
| `ai_likely_supported` 占比对比 | 默认 run 与探索+verify run 的 L2 analysis 字段统计 |

### 2.5 文档更新

- 新建 `docs/analysis/2026-08-22-m2-acceptance-runs.md`，**必须包含**：执行时 HEAD、M2 父链提交顺序、全量测试数为“提交时点快照”（当前 1147，新增后 1148）、run_id、指标、diff 结论、正/负样本映射表、未达标项说明。
- 修改 `docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §4.3：按实测勾选 checkbox，并在 §4.3 末尾写明“执行记录见 m2-acceptance-runs.md；未达标项单独列明”；
- 修改 `docs/analysis/2026-08-22-m2-implementation-review.md`：增加“处置记录”区，逐条标注 4.1–4.5 的处置结果；
- 修改 `docs/analysis/2026-08-22-t2-12-implementation-plan.md` §4 末行：将“交接 M2 验收”改为“已由 M2-ACCEPTANCE-CLOSURE 执行，记录见 m2-acceptance-runs.md”。

---

## 3. 与大纲一致性对照

| 大纲/审查条目 | 本方案对应 |
|---|---|
| 审查 4.1 高：§4.3 未执行 | 2.3/2.4/2.5 真实双 APK 验收并勾选 |
| 审查 4.2 中：grep 断言写进测试 | 2.1（AST 扫描，等价实现） |
| 审查 4.3 中：默认配置 diff 为空未实测 | 2.2 |
| 审查 4.4 低：提交顺序/基线快照易误读 | 2.5 收尾文档注明提交顺序与最终基线 |
| 审查 4.5 低：sink_taxonomy 种子真实验证 | 2.4 custom sink 命中/误标记录 |

---

## 4. 风险与回退

| 风险 | 对策 |
|---|---|
| 探索轨/核验轨真实 run 超时或 AI 预算耗尽 | run 有预算上限，失败不阻塞主链；记录失败原因；若单 APK 失败，先补单 APK 冒烟并如实记录 |
| 默认配置基线 diff 非空 | 按 M1 基线文档口径复核；若 `analysis.sqlite3` 字节噪声用 dump 对比；若实质差异 → 验收不通过并逐项归因记录 |
| §4.3 覆盖未达 ≥6/8 | 不勾选该项，保留为未达标；在验收文档中记录差距并给出后续调优建议，不宣称 M2 验收完全通过 |
| 成本/时间超预算 | 两个 APK 串行执行，先 health 后 shop；若 shop 失败可先完成 health 冒烟并记录 |
| 新测试误报 | 测试使用 AST 只检测 import 节点，注释/字符串/docstring 不视为违规 |

---

## 5. 交付物

- `backend/tests/test_no_rules_import.py`
- `docs/analysis/2026-08-22-m2-acceptance-runs.md`
- 更新：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md`
- 更新：`docs/analysis/2026-08-22-m2-implementation-review.md`
- 更新：`docs/analysis/2026-08-22-t2-12-implementation-plan.md`
