# M3 / M4 阶段实现审查报告

> **审查对象**：M3（报告生成 + PoC 骨架 + 修复建议）与 M4（golden 探索轨标注 / 批量评估 / 报告质量检查 / 优化门槛）全部合入提交（`6622493..084665f`）
> **审查日期**：2026-08-23
> **审查方法**：
> - 逐提交核对交付物与验收记录（M3-1 / M3-2 / M4-SEED-HOPS / M4-T4.1~T4.4）
> - 独立运行全量 pytest 与 prompt/schema 同步门禁
> - 代码级核对：reporting 生成链路、report prompt 协议、seed-hops 注入、golden 标注数据、evaluation runner / report_quality / gate、CLI 与文档
> - 对照上级依据：`2026-08-21-explorer-track-implementation-plan.md` §4.4/§4.5 与 M2 验收记录 `2026-08-23-m2-acceptance-runs.md`
> **总体表态**：**M3/M4 代码交付完整、测试充分、真实 AI 报告链路已跑通；但 M4 作为“度量闭环”仍缺两块真实数据底座：探索命中标注集与 M2 动态终审 8 项不一致、评估基线快照未入库。** 因此 M3 可以进入交付状态；M4 目前是“机制可用、数据未闭”状态，须补齐数据后才能在后续 prompt/模型变更中真正承担优化门槛职责。

---

## 1. 交付物核对（M3 / M4）

| 任务 | 交付 | 核对结论 |
|---|---|---|
| M3-1 报告生成 + PoC + 修复建议 | `backend/app/reporting/{models,generator,poc,repair}.py` + `POST /api/findings/{id}/report-draft` + `test_report_poc.py` | ✅ 三重门禁 / AI-确定性分离 / 零可执行骨架 / 真实 V-01/V-02 端到端 |
| M3-2 report prompt 协议 | `prompts/report/1.0.0/` + `report_entry` + `ReportInput/ReportDraftOutput` + registry/schema sync | ✅ 真实 V-01 `provenance=ai_report_protocol` 零 fallback；降级回投投影不阻塞 |
| M4-SEED-HOPS 骨架链 | `SeedHop` + `call_tree.get_seed_hops` + `explorer.py` 注入 + prompt 约束 | ✅ 数据源三要素确定性（SQL 直查 `start_line,resolved_target_id`），约束 4/10/12 对齐 |
| M4-T4.1 探索轨命中标注 | `ExplorerExpectation` / `GoldenCase.explorer_expected` / 8 个 golden case 标注 | ⚠️ 机制正确，但**标注集与 M2 成立脆弱性 8 项不一致**（见问题 4.1） |
| M4-T4.2 批量评估 | `runner.py` explore_runs / costs / CLI `--runs` | ✅ 三本账提取 + 加权命中率 + 真实 shop run 冒烟数字与 manifest 一致 |
| M4-T4.3 报告质量检查 | `evaluation/report_quality.py` | ✅ separation/references/poc 三检 + verdict 聚合 + 真实 document PASS |
| M4-T4.4 优化门槛 | `evaluation/gate.py` + `docs/evaluation-workflow.md` | ⚠️ 门禁逻辑通过测试，但**基线快照未入库**（见问题 4.2） |

## 2. 验证执行情况

- **全量测试**：`backend/.venv/bin/python -m pytest -q` → **1248 passed / 0 failed**（收集统计 1248 项）。
- **协议同步门禁**：`backend/.venv/bin/python scripts/sync-ai-protocol.py --check` → 退出码 **0**。
- **工作区**：`git status` 干净，无未提交产物混入。
- **真实 AI 链路**：
  - M3-2 A-8：真实 V-01 `report_entry` 首次调用即过 schema，`prompt_version=1.0.0`、`model=deepseek-v4-pro-0813`、零 fallback；
  - M4-T4.2 A-9：真实 shop run `dc24a077` 评估输出与 manifest 三本账逐项一致（explorer 424 / verify 29 / ai_stage 62 / total 486）。
- **流程纪律**：M3-1 存在“先实施后补评审”流程违规，但已事后补评审 R-1~R-11 并全部闭合；M3-2 及 M4 各任务按六阶段执行。

## 3. 肯定项（核心设计点均已落实）

| 事项 | 证据 | 结论 |
|---|---|---|
| 报告 AI 与确定性事实分离 | `ReportDocument.deterministic` / `ai_draft` 分列；`_build_report_input.deterministic_summary` 排除 L2 AI 文本；provenance 枚举合法 | ✅ |
| 报告降级永不阻塞 | `_ai_report_draft` 失败 → 回投 `projected_from_l2_review` + fallback metadata；M3-2 A-6 实测 schema_invalid 降级 | ✅ |
| 零可执行 PoC 三重保险 | `PoCSkeleton.executable_files_created` 恒空 validator + 命令骨架占位符 + 落盘扫描测试 | ✅ |
| 探索候选证据 caveat | `EXPLORER_CAVEAT` 仅在 `candidate_source=="explorer"` 注入；M2 质量未闭环期间口径固定 | ✅ |
| seed-hops 三要素确定性 | `SeedHop` from/to/call_site_line；`get_seed_hops` 直查 `call_sites(resolve_status='resolved', ORDER BY start_line LIMIT 8)`；prompt 约束 4/12 同步 | ✅ |
| 规则轨评估零回归 | `ExplorerExpectation` 为可选字段，v2 case 无标注可加载；`hit-only` 二元语义与 metrics 排除 conditional 对齐 | ✅ |
| 报告质量检查容错 | 缺键 → violation 不抛；FAIL > WARN > PASS 聚合 | ✅ |
| gate 边界齐全 | 白名单点路径 / current 缺指标 BLOCK / baseline 缺指标 SKIP / 结构混用 BLOCK / CLI 退出码 0-1 | ✅ |

## 4. 问题清单（按严重度排序）

### 4.1 [高] M4-T4.1 探索命中标注集与 M2 动态终审 8 项不一致

**证据**：

- `docs/analysis/milestones/2026-08-23-m4-t4-1-implementation-plan.md:28` 列出 8 个标注样本：`remote-aidl-unguarded / provider-query-helper-delegation / sport-binder-unguarded-effect / router-validation-overwritten / fragment-external-class-name / account-broadcast-external-sender / keepalive-proxy-data-status-injection / extra-splashinfo-plugin-injection`，并声称“对应 M2 验收覆盖映射表条目”。
- 但 M2 动态终审成立 8 项（`2026-08-16-vulnerability-discovery-success-plan.md:19/184`）是：health V-01/V-02/V-03/P-08 + shop V-01/**V-02**/P-01 + health P-05。
- 当前 golden 数据逐项核对结果：
  - `evaluation/golden/v1/cases/extra-close-url-unregistered-dos.json`（= shop V-02，`label=positive`）**无 `explorer_expected`**；
  - `remote-aidl-unguarded`（规则轨合成正样本）**不是 M2 动态终审 8 项之一**，却被列入标注集。
- 后果：真实探索轨即使命中 shop V-02（`extra_close_url` → `go2CloseSet`/`startActivity`），`evaluate_runs` 也**不会**把它计入 `explorer_hit_rate` 分母；M2 §2.3 覆盖映射表仍有一项无法机器判定；指标口径与产品口径长期漂移。

**建议**：

1. 为 `extra-close-url-unregistered-dos` 补 `explorer_expected`（expectation 建议 `hit` 或 `conditional`，按其动态终审成立性质应至少进条件/命中口径；匹配键从该 case 既有 `sources`/`sinks` 的 symbol/path 派生，如 source `MainActivity`/`extra_close_url`，sink `go2CloseSet`/`startActivity`）；
2. 同步修正 T4.1 实施方案与相关测试中的“8 项”清单口径，避免把合成样本混入动态终审统计；
3. 若有意排除 shop V-02，必须在方案与文档中写明理由与代价（M2 覆盖映射永远缺一项），并上升用户决策——而不是静默不标注。

### 4.2 [高] M4-T4.4 评估基线快照未入库——优化门槛暂时“有闸无基”

**证据**：

- `docs/evaluation-workflow.md:5-9` 明确要求 `evaluation/baselines/<name>.json` 提交入库，并称“首次创建目录后加入版本控制”；
- 当前仓库无 `evaluation/baselines/` 目录，也无 `evaluation/results/` 基线（`ls evaluation/` 实证）；
- `gate.py` 与 `docs/evaluation-workflow.md:12-14` 的合入前必跑门槛，因此**无法针对真实基线执行**；当前测试均为合成 fixture（`test_evaluation_gate.py`），不能替代“真实 deviation 较上次基线劣化”的判定。

**建议**：

1. 立即生成并提交基线快照：
   - 探索轨：`python -m backend.app.evaluation.runner --runs 20260822T202633Z_2a80fc5a8735_7ecd4288 > evaluation/baselines/m4-health.json`（shop 同理 `dc24a077`）；
   - 规则轨：若已有 `results` 文件则 `--results` 生成基线；没有则先补实际结果并记录。
2. 在 `evaluation/baselines/README.md` 中注明每份快照来自哪个 run/配置（模型、prompt 版本、日期），否则门槛将不可审计。

### 4.3 [中] 报告 AI 调用未纳入 run 预算/cache/trace，存在不可复现与成本未计量

**证据**：

- `backend/app/api/routes.py:303`：`analyzer = request.app.state.ai_runtime.create_analyzer()`——未传 `cache_dir`、`max_output_tokens`、`budget_policy`；
- `generate_report_document`（`generator.py`）与 `_ai_report_draft` 同 run 预算/三本账体系无任何关联；
- M3-2 验收 A-6 只验证降级路径，未验证“报告 AI 调用计入 run 开销”或“同 finding 重复生成命中缓存”。

**影响**：报告生成作为逐个可点的人工操作，成本可被隐性放大；同 finding 重复点击不命中缓存；模型/协议变更时缺少 run 级 trace 溯源。

**建议**：

1. 在 API 层为 route 创建 analyzer 时补 `cache_dir`（run 内或共享报告缓存）与 `max_output_tokens`（沿用 `context_budget.max_output_tokens`），并把报告调用记入 run 级 AI 账本（可新增 `stage_summary.report_requests_used`）；
2. 若短期不接预算，至少在 `ReportDocument` 中持久化 `trace_id` 与请求 metadata（部分已有），并在验收中固定“连续两次生成同 finding 的缓存命中”为可选项。

### 4.4 [中] 报告草稿落盘未按承诺收紧文件权限

**证据**：

- `backend/app/reporting/generator.py:296` docstring 标注“0o700——沿报告先例”；
- 实际代码（`generator.py:302-309`）只 `drafts_dir.mkdir(mode=0o700)`，`write_text` 后没有对文件 `chmod`；文件权限受 umask 影响，可能是 0644。

**建议**：`write_text` 后补 `path.chmod(0o600)`，并补测试断言 `posix.stat.S_IMODE == 0o600`（或至少 owner-only）。

### 4.5 [低] 报告质量检查的引用回查范围小于实际投影范围

**证据**：

- `generator.py:60-78` 从 `sources` / `sinks` / `locations` 三桶投影证据引用；
- `report_quality.py:63-71` 只检查 `deterministic.sources` 与 `deterministic.sinks`，未检查 `locations`；`models.py:31` docstring 又声明从三桶投影。

**建议**：`_check_references` 的 `for bucket in ("sources", "sinks")` 扩展为 `("sources", "sinks", "locations")`；或如 `locations` 不参与引用回查，修改 docstring 与 projection 去掉该桶，避免口径含糊。

### 4.6 [低] gate CLI 的 `--tolerance` 非法值未走 parser 错误路径

**证据**：

- `backend/app/evaluation/gate.py:109`：`tolerances[name.strip()] = float(value)`，无异常处理；
- 实测 `--tolerance 'abc'` 产生完整 Traceback（ValueError）而非 `parser.error` 的规范退出（exit 2）。

**建议**：包 `try/except (ValueError, TypeError)` 后 `parser.error(f"非法容差: {chunk!r}")`，并补负例测试。

### 4.7 [低] `report-draft` 路由为 `async def`，但内部同步 IO 阻塞事件循环

**证据**：

- `routes.py:289-308`：`async def generate_finding_report_draft`，内部直接调用同步 `repository.get_finding` / `storage.run_dir` / `save_report_document`；
- 文档注释称“当前同步 IO 可接受，届时统一异步化”。

**影响**：本地单用户下影响极低；但若同时进行批量任务与报告生成，可能对事件循环造成毫秒级阻塞。

**建议**：若本路由没有真正等待网络 IO 的必要，可改为 `def`（FastAPI 自动线程池）；若保留 `async`，则至少把文件/DB 操作包 `asyncio.to_thread`。

## 5. 结论与放行建议

**结论**：

- **M3 可进入交付态**：报告生成链路设计完整、真实 AI 报告草稿已跑通、降级与零可执行承诺有测试背书。
- **M4 机制可用但数据底座未闭**：`runner/report_quality/gate` 代码完成且测试充分，但“探索命中标注集对不上 M2 8 项”与“基线快照未入库”使 M4 尚不能在真实 AI prompt/模型变更中承担优化门槛职责。

**放行建议（按优先级）**：

1. 补 `extra-close-url-unregistered-dos` 的 `explorer_expected` 标注并修正 T4.1 口径（4.1）；
2. 为 health/shop 真实 run 生成并提交 `evaluation/baselines/` 快照（4.2）；
3. 报告 AI 调用接入缓存与成本记录（4.3）；
4. 落盘 `chmod(0o600)` 并补断言（4.4）；
5. 补 `locations` 引用检查或修正口径（4.5）；
6. 修 gate 非法容差的错误处理，并补路由线程化/异步化小节（4.6/4.7）。

> 备注：本报告未将 M3-1 的“先实施后补评审”列为新问题——该流程违规已由 `2026-08-23-m3-report-poc-review.md` 事后评审 R-1~R-11 闭环；但后续任务应禁止再出现同类流程违规。

---

## 6. 处置结果（被审查方回填，2026-08-23，提交 8fb7568）

**总体**：七项意见全部处置——五项完全认同并修复（4.1/4.2/4.4/4.5/4.6）、一项部分认同（4.3 澄清）、一项不采纳核心建议但采纳防护部分（4.7）。修复后全量 **1251 passed / 0 failed**（+3）、sync --check 通过、app 层 ruff 零错误。**M4 数据底座已闭**（本报告总体表态中"数据未闭"的两个缺口均消除）——优化门槛可真实承担 prompt/模型变更的守门职责。

| 编号 | 处置 | 实测结果 |
|---|---|---|
| 4.1 [高] | **完全认同，已修复**：补 `extra-close-url-unregistered-dos`（shop V-02 动态终审成立）标注——expectation=hit，键 `MainActivity`/`extra_close_url` + `startActivity`/`go2CloseSet`；标注集 **6 hit + 3 conditional = 9**；T4.1 方案口径修正（原"对应 M2 覆盖映射表"表述不实——清单含规则轨合成样本且遗漏 V-02，已在方案文档记录）；manifest 描述、测试集合（`_HIT_CASES`+1）、聚合分母（2 run × 6 = 12）同步 | 原报告指出的"真实探索命中 shop V-02 不计入分母"缺口消除 |
| 4.2 [高] | **完全认同，已修复**：双真实 run 基线快照入库——`evaluation/baselines/m4-health.json`（hit_rate 0.0）/ `m4-shop.json`（hit_rate 0.167，`remote-aidl-unguarded` 命中）+ `baselines/README.md`（来源 run/模型 deepseek-v4-pro-0813/prompt 版本/dd52f12 修复后但 seed-hops 之前/“新增指标先刷基线”/规则轨基线待补的诚实记录） | "有闸无基"消除——gate 可对真实基线执行 |
| 4.3 [中] | **部分认同，澄清**：① `max_output_tokens` 已有默认路径（`AISettings.max_output_tokens=None` → 沿用 `context_budget.max_output_tokens`=3000）——原报告"未传"不构成实际风险；② `cache_dir` 不传是 M3-2 有意设计（ReportDraftOutput 无 evidence_refs，缓存判据恒 False 读写皆弃——M3-2 评审 R-10c 已接受）；③ 预算记账（`report_requests_used`）认同为真实缺口，记为后续项 | 短期无需代码改动；长期项挂账 |
| 4.4 [中] | **完全认同，已修复**：`save_report_document` 补 `path.chmod(0o600)`（mkdir 的 mode 确实不覆盖文件——umask 影响实证成立）+ 测试双断言（文件 0o600/目录 0o700） | 文件权限 owner-only 收紧 |
| 4.5 [低] | **完全认同，已修复**：`_check_references` 桶列表扩展 `("sources", "sinks", "locations")` 与投影三桶对齐 + 正负例测试 | 口径含糊消除 |
| 4.6 [低] | **完全认同，已修复**：`--tolerance` 非法值包 `try/except ValueError` → `parser.error`（退出码 2）+ 负例测试 | 不再产生 Traceback |
| 4.7 [低] | **不采纳"改 def"**：`analyzer.report_entry` 为真实网络 IO（M3-2 async provider 链路必需——`generate_report_document` 内 `await`），改 def 会破坏异步链路（原报告此建议与 M3-2 交付矛盾）。**采纳事件循环防护部分**：`save_report_document` 落盘包 `asyncio.to_thread` + 路由注释说明 async 必要性 | 同步 IO 不再阻塞事件循环 |

**对文档本身的两点反馈**（不影响处置，供后续审查参考）：

1. 4.7 的"若本路由没有真正等待网络 IO 的必要，可改为 def"前提不成立——该路由的 AI 调用（`report_entry`）就是网络 IO；
2. 4.3 未覆盖 `AISettings.max_output_tokens` 的默认回退路径与缓存 no-op 的有意设计——若纳入可减少两个伪修复点。

**后续遗留**（修复中挂账，非本报告新增）：规则轨基线快照（首次离线评估后补）；报告 AI 预算记账 `report_requests_used`；官方双 APK 全量验收（seed-hops 攻击面注入后效果验证）；探索假设种子归一化透传（M3-2 评审 R-1 缓期项）。