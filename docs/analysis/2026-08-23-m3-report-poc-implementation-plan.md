# 任务实施方案：M3-1 报告生成 + PoC 骨架 + 修复建议（最小闭环）

> **任务编号**：M3-1
> **日期**：2026-08-23
> **依据**：`2026-08-21-explorer-track-implementation-plan.md` §4.4（T3.1~T3.4）；`2026-08-23-m2m3-forward-guidance.md` §6（最小闭环原则——UI 后置）
> **状态**：方案就绪（子 agent 调研交付 + 主 agent 整理），实施待执行
> **独立性声明**：M3 验收独立于 M2 质量验收（指引 §6.2）

---

## 1. 现状锚点（调研已核实，M3 无需重做）

| 项目 | 现状 | 位置 |
|---|---|---|
| 配置门禁 | `ReportSettings` 已存在：`allow_executable_poc=False`、`require_confirmed_finding=True`（T0.7 交付） | `backend/app/config.py:258-262` |
| 确定性报告 | `build_report_payload` + `render_markdown`（含 L1 拒绝、`_poc_guide` ADB 模板、`remediation` 字段）——M1 口径（schema 2.0.0） | `backend/app/findings/report.py` |
| 报告触发 | **API 按需触发**（非 scan 流水线阶段）：`GET /api/findings/{id}/report`，落盘 `run_dir/reports/*.md`（0o700、stale 清理） | `backend/app/api/routes.py:268-285` |
| AI 协议样板 | `explore_entry`/`verify_entry` 复用 render→cache→budget→transport→strict-parse→repair 状态机 | `backend/app/analysis/ai.py:437-495` |
| 证据来源标注 | `candidate_source == "explorer"` 已参与分源；规则候选无该字段 | `backend/app/analysis/candidate_funnel.py:506,526` |
| ReportDraft | **不存在**——按本方案新建 | `backend/app/analysis/ai_models.py` |

## 2. 字段设计（`backend/app/reporting/models.py` 新建）

**`ReportDraft`（AI 草稿层）**：`summary` / `vulnerability_narrative` / `exploit_scenario`（LongText）、`evidence_refs: list[EvidenceReference]`（复用 ai_models——path/line 必须可回查到 finding 的 slice/verified_evidence_refs）、`confidence_tier: Literal["low","medium","high"]`、`analysis_complete: bool`。

**`ReportDocument`（服务层合并产物，落盘 `run_dir/reports/drafts/{finding_id}.json`）**：
- `finding_id` / `run_id` / `generated_at`
- `evidence_source: Literal["rule_candidate","explorer_candidate"]`（由 `finding.candidate_source` 映射）
- `explorer_caveat: str | None`（仅 explorer 来源注入："explorer_validated=0 期间，探索质量未达标，探索候选证据置信度低于规则候选"——指引 §6.3）
- `deterministic: {...}`（确定性投影：sources/sinks/propagation_paths/locations/severity/review_status/review_reason 等——直接从 finding 复制不改写）
- `ai_draft: {summary, narrative, exploit_scenario, confidence_tier, provenance, prompt_version, model}`（`provenance: Literal["projected_from_l2_review","ai_report_protocol"]`——诚实标注草稿来源）
- `poc_skeleton: PoCSkeleton` / `repair: RepairDraft`

**`PoCSkeleton`（零可执行产物）**：`component_kind`、`kind: Literal["intent","uri","binder_transaction","broadcast","provider_query"]`、`steps: list[str]`、`command_skeleton: list[str]`（命令骨架**文本**，全占位符 `<PACKAGE>`/`<ACTION>`/`<EXTRA_KEY>`）、`notes: list[str]`（授权设备/占位符替换/非可执行声明）、`executable_files_created: list[str]`（**恒空——供机器断言**）。

**`RepairDraft`（确定性与 AI 分离）**：`deterministic_recommendations: list[str]`（按 rule_id/组件类型确定性映射——复用 `report.py:477` remediation 先例扩展）、`ai_recommendations: list[str]`、`ai_rationale: str | None`。

## 3. 模块落点与关键取舍

**落点**：新建 `backend/app/reporting/`（`__init__.py` / `models.py` / `generator.py` / `poc.py` / `repair.py`）——与既有确定性 `findings/report.py` 清晰分离。API：`routes.py` 新增 `POST /api/findings/{finding_id}/report-draft`（模式照抄 `finding_report`）。

**取舍 1（最重要）：T3.1 报告 prompt 协议延后为 M3-2**。注册 `prompts/report/1.0.0/` 须过三重门禁（`_SCHEMA_FILE_RE` 强制 `ai_*.schema.json`、SHA-256 逐字节校验、sync 脚本生成）。M3-1 以 **provider 接口抽象**（`ReportDraftProvider = Callable[..., Awaitable[ReportDraft]]`）隔离 AI 草稿来源；M3-2 接入真 prompt 时仅换 provider 实现，`ReportDocument` 结构零改动——兑现指引 §6.3"M2 数据源升级不需改 M3 结构"。

**取舍 2：AI 草稿的确定性投影**。M3-1 默认 provider 从 finding 既有 `ai_analysis`（L2 已验证输出）投影 `ReportDraft`，`provenance="projected_from_l2_review"` 显式标注——不冒充新 AI 生成。

**取舍 3：拒绝路径语义**。`review_status != "confirmed"` → `ConflictError`（409，`REPORT_DRAFT_REQUIRES_CONFIRMED`）；L1 informational 另行拒绝（沿 `L1_REPORT_FORBIDDEN` 口径）；`allow_executable_poc=True` → `ValidationError`（`EXECUTABLE_POC_FORBIDDEN`——不存在可执行生成路径，开关置真视为配置违例，最保守）。

## 4. 端到端验证数据（已核实真实 confirmed finding）

- **V-01**：`.ai-apk-tracer/runs/20260815T125744Z_2a80fc5a8735_ef5915ff/findings/..._finding_1ed37af9596f8761bda5.json`——SportXmsService / `SERVICE_BINDER_CALLER_CHECK_MISSING` / L2 / confirmed（review_reason 记录动态终审：第三方 bind 成功 + transact(23) 返回 112B 设备数据）；可回查引用已核实（`defpackage/v5e.java:213-216`、`SportXmsService.java#onBind:45-51` 等）；PoC 类型 `binder_transaction`（注明 ADB 不可直接构造需测试 APK——沿 `report.py:296` 先例）
- **V-02**：同目录 `..._finding_5312960eaa38fec5d8bd.json`——RouterActivity / `ACTIVITY_INTENT_TO_SENSITIVE_SINK` / L2 / confirmed；source `RouterActivity.java:38` / sink `:70`；PoC 类型 `intent`（am start 骨架）

## 5. 测试与验收

**测试**（`backend/tests/test_report_poc.py`，fake provider 对齐 `FakeVerifyAI` 模式）：
- 拒绝路径：pending_manual/pending_ai → ConflictError；L1 → 拒绝；`allow_executable_poc=True` → 拒绝
- 正向：全字段断言；ai_draft 与 deterministic 键分离 + provenance；`executable_files_created == []` + 落盘目录无可执行后缀扫描；evidence_source 两分支 + caveat 注入；引用回查（tmp_path 真实行号）

**验收**（四点自查 + M3 专项）：
1. 字段完整（模型校验）；
2. 全部代码引用可回查（path+line 真实存在）；
3. AI 草稿与确定性证据分开展示（结构分离 + provenance）；
4. 默认零可执行产物（机器断言）。

## 6. 遗留

- M3-2：真 prompt 协议（provider 换实现 + `report_entry` 照抄 `verify_entry` 模式 + registry 注册）
- UI 后置（`frontend/src/features/reports/` 不做——JSON API 已为前端分区展示预留）
