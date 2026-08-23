# 任务验收记录：M3-1 报告生成 + PoC 骨架 + 修复建议

> **任务编号**：M3-1
> **日期**：2026-08-23
> **依据方案**：`2026-08-23-m3-report-poc-implementation-plan.md`
> **验收方式**：pytest 专项（17 用例）+ 真实 V-01/V-02 finding 端到端 + 全量回归

## 1. 交付清单

| 文件 | 内容 |
|---|---|
| `backend/app/reporting/models.py` | EvidencePointer / ReportDraft / PoCSkeleton / RepairDraft / ReportDocument（+ EXPLORER_CAVEAT 常量） |
| `backend/app/reporting/generator.py` | 三重门禁（EXECUTABLE_POC_FORBIDDEN / REPORT_DRAFT_REQUIRES_CONFIRMED / L1_REPORT_FORBIDDEN）+ `project_draft_from_l2_review` 默认投影 provider + `generate_report_document` 组装 + `save_report_document` 落盘（0o700） |
| `backend/app/reporting/poc.py` | 骨架类型确定性映射（rule/组件 → intent/uri/binder_transaction/broadcast/provider_query）+ Binder ADB 限制注记 + 恒空 executable_files_created |
| `backend/app/reporting/repair.py` | 确定性建议映射（沿 report.py remediation 先例扩展）+ L2 复核投影的 AI 部分 |
| `backend/app/api/routes.py` | `POST /api/findings/{finding_id}/report-draft`（模式照抄 finding_report 先例） |
| `backend/tests/test_report_poc.py` | 17 用例（拒绝 4 / 正向契约 7 / PoC 与修复 3 / 真实端到端 3） |

## 2. 四点自查（指引 §6.2）——实测结论

| 检查点 | 结果 | 证据 |
|---|---|---|
| ① 字段完整 | **通过** | `test_document_fields_complete`：ReportDocument 全字段模型校验（pydantic 必填强制）；V-01 端到端 deterministic 26 键投影完整 |
| ② 引用可回查 | **通过** | `test_real_findings_evidence_path_exists`：V-01/V-02 的 sources/sinks path 在反编译源码树真实存在（`decompile/sources/` 命中断言）；`test_projected_draft_reference_alignment`：投影 evidence_refs 与 finding 的 sources/sinks 逐条对齐且 line ≥1 |
| ③ AI 草稿与确定性证据分离 | **通过** | `test_ai_and_deterministic_separated`：ai_draft 与 deterministic 键结构性分离 + `provenance="projected_from_l2_review"` 诚实标注；`test_explorer_source_caveat_injected`：explorer 来源注入 caveat |
| ④ 零可执行产物 | **通过** | `test_zero_executable_artifacts` + `test_save_creates_file_with_no_executables`：`executable_files_created == []` 恒空 + 落盘目录无 .py/.sh/.apk/.jar/.dex 文件扫描 + 命令骨架全占位符断言 |

## 3. 真实 finding 端到端（V-01/V-02）

**V-01**（SportXmsService / SERVICE_BINDER_CALLER_CHECK_MISSING / L2 / confirmed）：
- evidence_source=rule_candidate（无 caveat）；provenance=projected_from_l2_review、confidence=low（L2 复核真实值）；
- PoC kind=binder_transaction、3 步骤 + 2 命令骨架（含"ADB 不可直接构造需测试 APK"注记）、executable_files_created=[]；
- repair 确定性建议 2 条（Binder 调用方校验/事务白名单）；deterministic 投影 26 键。

**V-02**（RouterActivity / ACTIVITY_INTENT_TO_SENSITIVE_SINK / L2 / confirmed）：
- PoC kind=intent（am start 骨架 + 占位符）；executable_files_created=[]；repair 确定性建议非空。

## 4. 门禁与拒绝路径实测

| 场景 | 异常与错误码 |
|---|---|
| review_status ∈ {pending_manual, pending_ai, refuted} | ConflictError / REPORT_DRAFT_REQUIRES_CONFIRMED |
| evidence_level=L1 | ConflictError / L1_REPORT_FORBIDDEN（沿确定性报告先例） |
| allow_executable_poc=True | ValidationError / EXECUTABLE_POC_FORBIDDEN（无生成路径，配置违例即拒绝——最保守） |
| require_confirmed_finding=False | 显式放行（配置语义测试） |

## 5. 实施偏差记录（相对方案）

1. **EvidencePointer 替代 EvidenceReference**：方案原定复用 `ai_models.EvidenceReference`，实测其 context_id/claim 必填属 AI 协议切片语义、finding 投影场景无此数据——改用轻量结构（path/line/end_line/note）。M3-2 接入真 prompt 协议时可切换。
2. **async 测试同步包装**：项目无 pytest-asyncio 配置（既有惯例 asyncio.run）——测试用 `run()` 辅助包装。

## 6. M3 与 M2 指标独立性声明

M3-1 验收结论**独立于 M2 质量验收**（指引 §6.2/§6.3）：本任务数据源为 M1 规则轨 confirmed finding（evidence_source=rule_candidate）；M2 探索质量项（validated=0）不影响本验收。M2 官方全量若产出 validated 探索候选并 confirmed，M3 的 explorer_candidate 分支与 caveat 移除路径自动生效（结构零改动——`evidence_source` 字段设计兑现）。

## 7. 遗留（M3-2）

- 真实 report prompt 协议（`prompts/report/1.0.0/` 注册 + `report_entry` 照抄 verify_entry 模式 + provider 换实现——`test_provider_injection_point` 已锁定衔接点）；
- UI 后置（`frontend/src/features/reports/`——JSON API 的 ai_draft/deterministic 分离结构已为前端分区展示预留）。

## 8. 回归

- 专项：**17 passed**（tests/test_report_poc.py）；
- 全量：**1195 passed / 0 failed**（基线 1178 + 17）；
- ruff：reporting/ + routes.py + 测试零错误。
