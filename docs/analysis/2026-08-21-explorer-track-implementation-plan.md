# 探索轨与资产批量扫描：改造计划与验收标准

> **日期**：2026-08-21
> **性质**：实施计划与验收标准（指导后续编码与验收，非实施记录）。
> **输入文档**：
> - 方案：`2026-08-18-project-optimization-plan.md`（2026-08-21 修订版，评审修订点已合入正文）
> - 评审：`2026-08-18-project-optimization-plan-review.md`（§4 全部 13 项问题已解决）
> **决断记录（2026-08-21 用户拍板）**：
> 1. **深挖协议**（评审 §7.1 / §4.4）：新增 `explorer_deep_dive` 协议，不复用 l2-review；深挖=补齐事实，L2=独立裁决，职责分离。
> 2. **规则侧能力复用**（评审 §7.2 / §4.11）：方案 B——规则运行时输出产物 JSON（`binder_bindings` / `receiver_registrations` / `webview_js_bridges`），`api_surface.py` 读产物，backend → rules 保持零依赖。
> 3. **核验 agent（2026-08-21 讨论后确定）**：L2 的 agent 化演进形态（非新增一层）——命题清单输入 + 盲验（剥离探索假设层）+ 受控取证循环；M2 试点分流：探索 `validated` 候选必进 + 规则 L2 候选以核验 agent 替代单轮 L2 review（单轮 L2 保留为 A/B 对照与降级基线）；L1 攻击面典型验证列为 M4 评估后的扩展项（前置条件：确定性命题化 + 抽样上限，L1 无待证命题，直接验证属探索职责）；设计细节见方案 §2.7。

---

## 1. 总体原则

1. **确定性核心不可污染**：所有新能力默认关闭（`explorer.enabled=false`、`api_surface.enabled=false`、`assets.enabled=false`），每个里程碑有回归门禁，不通过不放行。
2. **探索轨低信任**：Agent1 只输出建议链（`hops` + `evidence_refs` + `hypothesis` + `impact_proposal`），最终判定复用现有 L2 链路（funnel L2 路由 → 切片 → L2 复核 → 证据回查 → DecisionEngine → `pending_manual`）。
3. **每个里程碑结束即跑全量回归**：默认配置下 run 输出与基线一致（产物 diff 为空）。

---

## 2. 里程碑与依赖

```text
M0（Phase 0，Schema/映射表/配置） ──→ M1（Phase 1，资产批量） ──→ M2（Phase 2，探索轨） ──→ M3（Phase 3，报告/PoC）
                                                                  ↑
M4（Phase 4，评估，可与 M2/M3 并行） ──────────────────────────────┘（golden 扩展依赖 M2 产出探索候选）

M2 内部依赖：T2.1（规则产物） → T2.2（api_surface）；T2.2/T2.3/T2.4 → T2.5（检索循环） → T2.6（三档校验） → T2.7（归一化+funnel）→ T2.8（deep_dive）→ T2.11（核验 agent）→ T2.12（分流与降级）；T2.9/T2.10 可与 T2.7 并行
```

时间估算沿用方案 §8：M0 约 1 周、M1 约 2 周、M2 约 3-4 周、M3 约 2 周、M4 约 2 周（并行）。

---

## 3. 改造计划（任务分解）

### 3.1 M0：基线与接口设计

| 编号 | 任务 | 涉及文件 | 类型 | 依赖 | 对应问题 |
|---|---|---|---|---|---|
| T0.1 | `ExplorerObservation` Schema：`chain_proposals[].hops`（from/to method_id、call_site_line、arg_positions、resolved_via）、`hypothesis`、`impact_proposal`、`read_requests[]`、`component_summary`、`loop:{done}` | `schemas/explorer_observation.schema.json` | 新增 | - | §4.1/§4.2/§4.3 |
| T0.2 | `ExplorerCandidate` Schema（含 `validation` 三档结果占位） | `schemas/explorer_candidate.schema.json` | 新增 | T0.1 | §4.1/§4.2 |
| T0.3 | `explorer_deep_dive` 协议 Schema + prompt 骨架（输入：partial 候选 + 缺失事实清单；输出：可回查证据；禁止改写链） | `schemas/ai_explorer_deep_dive_{input,output}.schema.json`（M0 审查 §4.3 同步实际命名）、`prompts/explorer-deep-dive/1.0.0/{system,user}.md`、`prompts/registry.yaml` | 新增 | T0.2 | §4.4（决断 1） |
| T0.4 | 规则产物 Schema：binder / receiver / webview 三类绑定结果的结构定义（含 `BINDER_IMPLEMENTATION_AMBIGUOUS/UNRESOLVED` gap 透传） | `schemas/binder_bindings.schema.json`、`schemas/receiver_registrations.schema.json`、`schemas/webview_js_bridges.schema.json` | 新增 | - | §4.11（决断 2） |
| T0.5 | `api_entry_table` / `attack_surface` Schema（来源字段按产物传递标注） | `schemas/api_entry_table.schema.json`、`schemas/attack_surface.schema.json` | 新增 | T0.4 | 方案 §2.1/§2.3 |
| T0.6 | 归一化映射表：ExplorerCandidate → Candidate，覆盖 required 全部 10 项（字段级，方案 §2.5 已定义映射规则，此处固化为文档 + 单测用例） | `docs/analysis/`（映射表文档）+ `backend/tests/` | 新增 | T0.2 | §4.6 |
| T0.7 | 配置模型扩展：`explorer` / `verify` / `api_surface` / `assets` / `batch` / `report` 段，全部默认关闭；含 `max_rounds_per_entry` / `max_requests_per_entry` / `deep_dive_prompt_version` / `verify.max_rounds_per_candidate` / `verify.fallback_to_single_turn_l2` / `batch.max_ai_calls` | `backend/app/config.py`（修改）、`config/default.yaml`（修改） | 修改 | - | §4.3/§4.9/§4.12/2026-08-21 决断 3 |
| T0.8 | `Asset` / `BatchScan` 数据模型与迁移脚本设计（版本号、升级路径） | `backend/app/shared/repository.py`（设计稿） | 设计 | - | §4.13 |
| T0.9 | 核验 agent（`verify`）协议 Schema + prompt 骨架（方案 §2.7）：命题清单输入结构、盲验输入构造规则（剥离假设层）、逐命题判定输出、轮数与降级配置项 | `schemas/ai_verify_{input,output}.schema.json`、`prompts/verify/1.0.0/{system,user}.md`、`prompts/registry.yaml` | 新增 | - | 2026-08-21 决断 3 |

**M0 交付物**：11 个新 Schema（explorer_observation / explorer_candidate / deep_dive input+output / 三规则产物 / api_entry_table / attack_surface / verify input+output；M0 审查 §4.3 修正计数）+ 1 个映射表 + 配置模型 + 迁移设计稿。

### 3.2 M1：资产批量扫描层

| 编号 | 任务 | 涉及文件 | 类型 | 依赖 | 对应问题 |
|---|---|---|---|---|---|
| T1.1 | 数据库迁移：`assets` / `batches` 表 + `runs.asset_id` / `runs.batch_id`（可空外键），注册 `schema_migrations`，含旧库升级测试 | `backend/app/shared/repository.py` + 迁移脚本 + `backend/tests/` | 修改 | T0.8 | §4.13 |
| T1.2 | 资产注册表：package name、apk path/sha256、来源、状态、最近 run_id；SQL 全参数绑定 | `backend/app/assets/registry.py` | 新增 | T1.1 | 方案 Phase 1 |
| T1.3 | 批量编排：串行/并发（`batch.max_concurrent_runs`）、失败重试、batch 预算帽与降级（`ai_skipped_by_batch_budget` 标记） | `backend/app/assets/batch.py` | 新增 | T1.2 | §4.9/§4.12 |
| T1.4 | API：`GET /api/assets`、`POST /api/assets/import`（本地 APK/包名列表，校验 sha256 与大小上限）、`POST /api/batches`、`GET /api/batches/{batch_id}` | `backend/app/api/routes.py`、`backend/app/api/models.py` | 修改 | T1.2/T1.3 | 方案 Phase 1 |
| T1.5 | 前端资产/批量页面：列表、导入、批量扫描、按包/按批次 findings 汇总 | `frontend/src/features/assets/` | 新增 | T1.4 | 方案 Phase 1 |
| T1.6 | batch 预算降级测试与迁移测试 | `backend/tests/` | 新增 | T1.3 | §4.12/§4.13 |

### 3.3 M2：API surface + call tree + 探索轨合流

| 编号 | 任务 | 涉及文件 | 类型 | 依赖 | 对应问题 |
|---|---|---|---|---|---|
| T2.1 | 规则产物导出：规则运行时（`rule_runner.py` 汇总侧）将 Binder 绑定（`rules/shared/index_reader.py` 解析结果）、动态 Receiver（`rules/shared/receiver_registration.py`）、WebView bridge（`rules/shared/detector.py` `WEBVIEW_JS_BRIDGE_EXPOSED` 调用点推导）落盘为三个 JSON，注册 `run_manifest.artifacts`；backend 不 import 规则侧代码 | `backend/app/analysis/rule_runner.py`（修改）、规则侧导出适配 | 修改 | T0.4 | §4.11（决断 2）/§4.7 |
| T2.2 | `api_surface`：读规则产物 + manifest 组装 `api_entry_table.json`，排在 `rule_prescan` 之后（`orchestrator._run` 在 guard 块后、funnel 前新增阶段） | `backend/app/analysis/api_surface.py`（新增）、`backend/app/analysis/orchestrator.py`（修改） | 新增+修改 | T2.1 | §4.11/时序约束 |
| T2.3 | `attack_surface`：四组件攻击面 JSON（组件名、导出、权限、入口方法、intent/action/uri、敏感能力、关联 API 入口） | `backend/app/analysis/attack_surface.py` | 新增 | T2.2 | 方案 §2.3 |
| T2.4 | `call_tree` on-demand 服务：`get_entry_points` / `get_method_body` / `get_callees` / `get_callers` / `resolve_invoke_target` / `class_hierarchy` / `search_symbol`，有界预算（深度 ≤8、节点 ≤500），可选落盘 | `backend/app/analysis/call_tree.py` | 新增 | - | 方案 §2.2 |
| T2.5 | 探索 Agent：prompt + 检索循环驱动（`explorer.py` 驱动、每轮落盘、`loop.done` 终止、预算强制终止产出部分链+缺口清单）；每轮观测写 `run_dir/explorer/observations.json` | `prompts/explorer/1.0.0/`、`backend/app/analysis/explorer.py` | 新增 | T2.2/T2.3/T2.4 | §4.1/§4.3 |
| T2.6 | 三档校验：引用回查 + taxonomy 命中（未命中标 `custom_sink_proposal`）+ **hops 逐跳回查**（`call_sites` 表：`resolved_target_id`、`resolve_status='resolved'`）+ Guard/授权阻断 | `backend/app/analysis/explorer_validation.py` | 新增 | T2.5 | §4.2/§4.5 |
| T2.7 ✅ | 归一化 + funnel 扩展：validated → Candidate（`rule_id=EXPLORER_AGENT`、`evidence_level=L2`）；funnel 加 `candidate_source` 与 `explorer_promoted` / `explorer_partial` / `explorer_unverified` disposition；identity 含 `candidate_source`；同链规则候选以 `related_candidate_ids` 关联 | `backend/app/analysis/candidate_funnel.py`（修改）、归一化模块 | 修改 | T2.6/T0.6 | §4.6/§4.8 |
| T2.8 ✅ | `explorer_deep_dive` 实现：partial 候选送深挖（占复核预算），产出可回查证据；L2 复核独立裁决不受影响 | `backend/app/analysis/explorer.py`（扩展） | 修改 | T2.7/T0.3 | §4.4（决断 1） |
| T2.9 | custom sink 升级闭环工具：人工确认 → sink taxonomy 版本化扩展 → 候选重校验脚本 → golden 用例生成 | 脚本 + taxonomy 版本化文件 | 新增 | T2.6 | §4.5 |
| T2.10 | 探索产物注册与审计视图：`explorer/candidates.json`、call_tree 可选落盘注册进 `run_manifest.artifacts`；前端人工队列展示 unverified/partial（按置信度排序） | `orchestrator.py`、`frontend/src/features/findings/` | 修改 | T2.6 | §4.5/方案 §5.3 |
| T2.11 ✅（464c15e） | 核验 agent（L2 agent 化演进，方案 §2.7）：prompt `verify/1.0.0/` + 命题清单生成器（确定性代码从候选 sources/sinks/Guard 事实与探索 hops 生成待证命题）+ 盲验输入构造（剥离 hypothesis/impact_proposal/confidence，仅保留可回查事实）+ 受控取证循环（终止条件=命题全部判定，非模型自声明；轮数预算内逐命题判定）；输出沿用 verdict/flaw_holds/exploitability/evidence_refs，DecisionEngine 消费方式不变 | `prompts/verify/1.0.0/`、`schemas/ai_verify_{input,output}.schema.json`、`backend/app/analysis/verify_agent.py` | 新增 | T2.7 | 2026-08-21 决断 3 |
| T2.12 ✅ | 核验分流与降级（M2 收官）：`verify.enabled` 时 L2 候选（含探索 validated）以 VerifyAgent 替代单轮 L2；失败/预算尽/索引不可用/意外异常自动回退单轮 L2（主链永不阻塞，`verify_fallback_reason` 溯源；fallback=false 失败终态对齐原路径）；适配层 `adapt_verify_result`（确定性默认值补齐 + evidence_refs→EvidenceReference 的 path#window context_id + `ai_evidence_contexts` 显式注入——评审 R-1；evidence.py track="verify" 按 l2_review 取证据需求——评审 R-2）；checkpoint identity 隔离；第三本账 `verify_requests_used` + `verify_counts`（batch 帽经 run 级 requests_used 自动覆盖）；`_budgeted_protocol_call` 工厂（三协议共用） | `backend/app/analysis/verify_agent.py`、`backend/app/analysis/orchestrator.py`、`backend/app/findings/evidence.py` | 新增+修改 | T2.11 | 2026-08-21 决断 3 / M0 审查 §4.2 |
| T2.12 | 核验分流与降级：探索 `validated` 候选必进核验 agent；规则 L2 候选以核验 agent 替代单轮 L2 review；agent 失败/预算耗尽自动回退现有单轮 L2（主链永不阻塞，候选标记 fallback 来源）；核验预算独立记账（第三本账，batch 帽覆盖）；**适配层（M0 审查 §4.2）**：verify 输出补齐 L2 其余字段（harm/reachability_class/impact_vector/reverse_exclusion 以确定性默认值）并做 `evidence_refs` 类型转换（`ExplorerEvidenceRef` → `EvidenceReference`，context_id 从输入上下文映射回填），确保 DecisionEngine 证据校验可消费 | `backend/app/analysis/verify_agent.py`、`backend/app/analysis/candidate_funnel.py`（路由）、`backend/app/config.py` | 新增+修改 | T2.11 | 2026-08-21 决断 3 / M0 审查 §4.2 |

### 3.4 M3：报告、PoC 骨架与修复建议

| 编号 | 任务 | 涉及文件 | 类型 | 依赖 |
|---|---|---|---|---|
| T3.1 | 报告协议 prompt：输入已确认 finding + 证据 + 代码引用 + 决策事实；输出 `ReportDraft`；仅 `confirmed` 后可触发 | `prompts/report/1.0.0/`、`schemas/report_draft.schema.json` | 新增 | M2 |
| T3.2 | 报告生成：`ReportDraft` 与确定性 evidence 合并，AI 内容与确定性事实分离展示；`hypothesis`/`impact_proposal`/`component_summary` 作为描述种子并标注"探索假设"来源 | `backend/app/findings/report_generator.py` | 新增 | T3.1 |
| T3.3 | PoC 骨架：按组件类型生成非可执行骨架（Intent/URI/transaction 描述）；`allow_executable_poc=false` 默认关闭 | `backend/app/findings/poc_skeleton.py` | 新增 | M2 |
| T3.4 | 前端报告面板：生成按钮 + 分离展示 | `frontend/src/features/reports/` | 修改 | T3.2/T3.3 |

### 3.5 M4：评估与持续回归（与 M2/M3 并行）

| 编号 | 任务 | 涉及文件 | 类型 | 依赖 |
|---|---|---|---|---|
| T4.1 | golden 扩展：正样本 8 项 + 负样本（V-04/V-05/V-06、shop 140、OwnSystem）+ 探索轨命中标注 | `evaluation/golden/` | 修改 | M2 |
| T4.2 | 批量评估：多 APK 输入，输出 precision/recall/F1、AI 调用数（探索/复核/核验三本账）、wall-time | `backend/app/evaluation/` | 修改 | T4.1 |
| T4.3 | 报告质量检查：AI/确定性内容混淆检测、引用回查、PoC 骨架一致性 | `backend/app/evaluation/` | 新增 | M3 |
| T4.4 | 优化门槛：golden 指标不劣于基线才可默认开启 | 评估流程文档 | 新增 | T4.2 |

---

## 4. 验收标准

### 4.1 通用门禁（每个里程碑必须全部通过）

- [ ] `cd backend && python -m pytest` 全量通过；`scripts/check-all.sh` 通过。
- [ ] 默认配置（新开关全关）下，对基线 APK 跑 run，产物与当前基线 diff 为空（新增产物文件不算 diff，现有产物内容不变）。
- [ ] 新增 Schema 全部通过 schema 校验测试（复用现有 schema 测试机制）。
- [ ] 新增 prompt 全部注册进 `prompts/registry.yaml` 并过版本/哈希门禁。
- [ ] 无 lint 绕过注释（`# noqa` 等）；SQL 一律参数绑定；无硬编码密钥。

### 4.2 M1 专项验收

- [ ] 3 个本地 APK 导入并批量扫描成功；每个 APK 独立 run；结果可按批次汇总（`GET /api/batches/{id}` 返回每 run 状态与 findings 计数）。
- [ ] 单 APK 上传式 run（现有 `POST /api/runs`）行为与当前一致（回归）。
- [ ] 并发上限生效（`batch.max_concurrent_runs=2` 时最多 2 个 run 同时运行）；失败任务可单独重跑且不影响批次内其他任务。
- [ ] 预算降级：构造 `batch.max_ai_calls=1` 的批次，第二个及以后的 run 降级为仅确定性主链，run 记录含 `ai_skipped_by_batch_budget`，batch 汇总可见降级计数。
- [ ] 迁移：用旧版本 `tracer.sqlite3`（含 runs/findings 数据）执行迁移，升级后表结构正确、既有数据完好；全新库初始化路径同样通过。

### 4.3 M2 专项验收（三加一口径）

1. **覆盖**：
   - [ ] health/shop 双 APK 开启探索轨，各产出 ≥ 5 条 `validated` 或 `partially_validated` 候选；
   - [ ] 已知 8 项动态终审成立漏洞中，探索轨覆盖 ≥ 6 项，其中 ≥ 4 条为 `validated`、其余 `partially_validated`；
   - [ ] "同一链"判定口径：候选与 ground truth 的 source 组件一致 **且** sink 方法一致（方法名 + 所在类），即计为命中。
2. **负样本**：
   - [ ] V-04/V-05/V-06、shop 140 控制流共现、OwnSystem 未选择等负样本不出现在探索轨 supports/候选池；
   - [ ] 未通过校验（引用不可回查）的探索候选 0 条进入正式 finding；
   - [ ] `unverified` 候选不占 AI 预算（AI 调用计数不含 unverified 相关请求）。
3. **成本**：
   - [ ] 记录探索轨 AI 调用数与 wall-time 基线，探索 / 复核 / 核验三本账分开统计并可从 run 产物导出。
4. **性能**：
   - [ ] call_tree 单入口查询在 health 上延迟与内存可控（默认预算深度 ≤ 8、节点 ≤ 500；延迟阈值建议 ≤ 2s/入口，实测记录）。
5. **回归与边界**：
   - [ ] 默认配置下探索轨零影响（产物 diff 为空）；
   - [ ] 检索循环状态机：单入口轮数 ≤ `max_rounds_per_entry`（4）、读码请求 ≤ `max_requests_per_entry`（20）；跑满预算产出"部分链 + 缺口清单"而非报错；
   - [ ] hops 逐跳回查：构造含伪造 method_id 的候选，校验器正确判 `unverified`；
   - [ ] deep_dive：partial 候选深挖后链结构未被改写（diff 前后 hops 不变，仅新增证据）；
   - [ ] backend 源码中仍无 `import rules`（grep 断言写进测试）。
6. **核验 agent（试点）**：
   - [ ] 盲验：核验 agent 请求输入中不含探索 `hypothesis` / `impact_proposal` / `confidence`（trace 断言检查）；
   - [ ] 命题清单：每候选输入含确定性生成的 claims，输出逐命题判定且与 verdict 一致；
   - [ ] 循环语义：终止条件为命题全部判定或预算耗尽（非模型自声明 done），每轮落盘可审计；
   - [ ] 降级：构造 agent 循环失败/预算耗尽场景，候选自动回退单轮 L2 并标记 fallback 来源，主链不阻塞；
   - [ ] 证据引用适配：构造含 `ExplorerEvidenceRef` 的 verify 输出，断言适配层转换为 `EvidenceReference`（context_id 回填）后 DecisionEngine 证据校验通过（M0 审查 §4.2）；
   - [ ] `ai_likely_supported` 占比与现有单轮 L2 基线对比记录（预期下降但不归零：agent 化消除"上下文不足"型不完整，"静态不可判定"型诚实保留）。

### 4.4 M3 专项验收

- [ ] 对 2 个已确认 finding 生成报告草稿与 PoC 骨架：字段完整、全部代码引用可回查、AI 草稿与确定性证据分开展示且来源标注清晰。
- [ ] 未确认（`pending_manual` 及以下）finding 触发报告生成返回明确拒绝。
- [ ] 默认配置不产生任何可执行文件（`allow_executable_poc=false`）。

### 4.5 M4 专项验收与指标定义

- [ ] health/shop 双 APK 输出完整指标表：precision / recall / F1 / 机器闭合率 / `explorer_hit_rate = explorer_hit_count / confirmed_vulns` / AI 调用数（探索/复核/核验三本账）/ wall-time / 报告质量检查结果。
- [ ] 与 2026-08-16 验收基线对比：机器闭合率不下降；探索轨命中率 ≥ 6/8。
- [ ] 负样本零误报进入正式 finding（探索轨未校验候选不计入误报统计）。
- [ ] golden 集（正 8 + 负 5+）纳入回归，作为后续默认开启探索轨的门槛。
- [ ] 核验 agent vs 现有单轮 L2 对照（A/B）：同批候选在 precision / recall / AI 调用数 / wall-time / `ai_likely_supported` 占比上的对比表；核验 agent 指标不劣于单轮 L2 才可转正为默认 L2 形态，否则保留为探索轨专用。
- [ ] L1 攻击面典型验证扩展项评估：确定性命题化规则（从 L1 暴露面生成待证命题）+ 抽样上限的成本测算，数据支持才启用（L1 无待证命题，未经命题化不得直接进核验 agent）。

### 4.6 回退方案

- 任一里程碑验收不过：保持该里程碑全部开关为默认关闭，不合并到默认行为；已合代码通过配置回退，不回滚确定性主链。
- M2 探索轨指标不达标：`explorer.enabled` 维持 false，产出物保留供调优，golden 指标驱动 prompt/预算迭代后再验收。

---

## 5. 实施顺序建议

1. **先 M0 后 M1**：Schema/映射表先行是 M1 迁移设计与 M2 全部协议的前提。
2. **M2 内部先 T2.1/T2.4**（纯确定性、可独立测试），再 T2.5-T2.8（AI 相关）；`explorer_deep_dive`（T2.8）与核验 agent（T2.11/T2.12）最后，均依赖三档校验与归一化稳定。
3. **M4 的 T4.1 golden 扩展可与 M2 同步做**（标注字段不依赖探索轨实现），但命中率指标须等 M2 验收时才有数据。
4. 与 8-16 方案 S1–S11 的顺序：**确定性补强（S1–S11）先行，探索轨承接残余缺口**；两者同链产出时以 `related_candidate_ids` 关联，不合并 identity。
