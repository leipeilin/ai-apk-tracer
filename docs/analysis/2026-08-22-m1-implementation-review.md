# M1（资产批量扫描层）实施提交审查报告

> **审查对象**：`docs/analysis/2026-08-21-explorer-track-implementation-plan.md` §3.2 M1（T1.1–T1.6）的实施提交，commit 范围 `a1c8773..9e6fa5e`（10 个提交，48 文件，+4795 行）
> **审查日期**：2026-08-22
> **审查方法**：
> - 逐任务核对交付物与验收记录（T1.1–T1.6 的 implementation-plan / acceptance-plan / review 三文档）；
> - 独立运行全量 pytest 与前端生产构建（非沙箱环境）；
> - 代码级核对：迁移实现（v4/v5）、registry 安全边界、batch 编排并发/降级/失败隔离、orchestrator 的 `ai.enabled` 消费、API 路由与 SPA fallback；
> - 对可疑行为做实测验证（未知 `/api/*` 路径的响应语义、guard_verifier 测试环境依赖）。
> **总体表态**：**通过，可进入 M2 准备**。M1 体量大、核心功能完整、测试充分（全量 955 passed / 0 failed，独立复现）、流程合规，且 T1.6 自核查发现并修复了一个高危缺陷（batch 预算/墙钟降级只落审计元数据、未真正跳过 AI）。发现 1 项中严重度测试环境依赖问题、1 项 API 语义回归及若干低严重度事项，均不阻塞，建议按 §5 处理。

---

## 1. 交付物核对（T1.1–T1.6）

| 任务 | 计划交付物 | 实际交付 | 结论 |
|---|---|---|---|
| T1.1 | 迁移 v4（assets/batches + runs 关联列）+ 旧库升级测试 | `repository.py` `DATABASE_SCHEMA_VERSION 3→5` + `_migrate_assets_batches_v4`/`_migrate_batches_assets_json_v5` + `test_repository_v4_migration.py`（9 项） | ✅ 幂等、逐条 execute、测试矩阵完整 |
| T1.2 | 资产注册表（package/sha256/来源/状态/last_run_id） | `backend/app/assets/registry.py`（238 行）+ 11 项测试 | ✅ 内容寻址、防路径穿越、参数绑定 |
| T1.3 | 批量编排（并发/失败重试/预算降级） | `backend/app/assets/batch.py`（355 行）+ `run_config.py` + 17 项测试 | ✅ 并发/降级/TOCTOU 防护/失败隔离齐备 |
| T1.4 | 4 个 API 端点 | `routes.py` 新增 4 端点 + `models.py` `BatchCreateRequest` + `main.py` 组装 | ✅ 门禁/授权/脱敏齐备 |
| T1.5 | 前端资产/批量页面 | `AssetsPage` / `BatchPanel` / `ImportAssetForm` + `api.ts`/`types.ts` + SPA fallback | ✅ build 通过、URL 状态恢复 |
| T1.6 | batch 预算降级测试与迁移测试 | 自核查报告 + 高危缺陷修复 + 2 项补充测试 | ✅ 修复真实缺陷（见 §3） |

## 2. 验收执行情况核对

- **全量测试**：审查者独立运行 `backend/.venv/bin/python -m pytest` → **955 passed / 0 failed**（26.4s），与 T1.6 验收记录一致。
- **前端构建**：`npm run build`（tsc + vite）成功，5003 modules，产物正常（chunk 526KB 为既有规模提示，非阻塞）。
- **三文档流程**：T1.1–T1.6 均有 implementation-plan / acceptance-plan / review，验收记录逐项勾选；T1.4 显式记录 D1 收窄（包名列表导入 Phase 1 不实现）；T1.6 记录独立复核推翻初判。
- **基线回归（M0 §4.1 闭环）**：`2026-08-22-m1-baseline-runs.md` 建立 health/shop 双 APK 确定性产物基线（清单聚合哈希 + `scripts/baseline-manifest.py` 工具），M0 审查放行建议 1 已落实。
- **M0 审查其余放行建议闭环**：config.schema.json 补 description 声明"非运行时校验器"（§4.4）；实施计划同步 T0.3 命名/计数与 T2.12 证据引用适配层条目（§4.2/§4.3）。

## 3. 肯定项

| 事项 | 证据 | 结论 |
|---|---|---|
| T1.6 自核查发现并修复高危缺陷 | 核查初判"无新增代码"，独立复核发现 `ai.enabled` 无消费点 → 预算/墙钟降级只落审计元数据、预算帽会被超耗；修复后 orchestrator `_run` 读 run config `ai.enabled`（默认 True 兼容历史 run），`_run_ai_stage` 新增跳过路径（`disabled_by_run_config`），补 `test_batch_real_pipeline_degradation`（修复前必失败断言） | ✅ 真实缺陷被闭环修复，且用真实 jadx 链路验证降级 run `requests_used=0` |
| 迁移实现合规 | v4/v5 逐条 `db.execute()`（避免 `executescript` 隐式 COMMIT 破坏 initialize 回滚契约）；幂等加列/建表；`initialize` 迁移链接入；测试覆盖新库/v1 legacy/v3 叠加/中断恢复/FK SET NULL/UNIQUE/半迁移补列/get_run 新键/大表 1000 行 | ✅ |
| registry 安全边界 | 内容寻址副本（sha256 目录）；文件名显式拒绝路径分隔符；`validate_apk_zip` 大小/ZIP 结构校验；重复注册 `ConflictError` 保留副本不误删（T1.2 评审 R-1）；`safe_remove_tree` 防软链接删除；SQL 参数绑定 + 注入测试 | ✅ |
| batch 编排语义 | 信号量包整个资产处理（并发语义与降级判定顺序一致）；`_claim_batch` 条件 UPDATE 防 TOCTOU 双触发；`gather(return_exceptions)` 资产级失败隔离；`assets_json` 快照审计；预算（`max_ai_calls`）/墙钟（`max_wall_seconds`）双降级 + `skip_reason` 分解计数 | ✅ |
| orchestrator AI 预算事实源 | AI 阶段三个 summary 构造点（正常/跳过/断路早退）全部补 `requests_used`；batch 预算计数以 manifest 持久化事实源为准 | ✅ |
| API 层 | `assets.enabled` 门禁（503 语义）；`authorized` 确认；响应脱敏（`apk_path`/`assets_json`）；`ConflictError` 补 `details`（向后兼容） | ✅ |
| 前端 | `?batch=`/`?import=` URL 状态恢复（评审 R-1）；发起后清空选中与授权（R-3）；503 引导态；`usePolling` 扫描中 2s 轮询 | ✅ |

## 4. 问题清单（按严重度排序）

### 4.1 [中] guard_verifier 3 个测试的环境依赖未修复（"翻绿"≠修复）

**证据**：`tests/test_guard_verifier.py:22-23` 的 `_latest_index()` 用 `glob.glob(str(WORKSPACE_ROOT / ".ai-apk-tracer/runs/*/"))` 取**真实 runs 目录**里最新 run 的 `analysis.sqlite3`，且 `assert runs, "未找到 run 目录"`——全新环境（CI/新 clone，runs 目录不存在）必失败。M0 时该 3 项失败被披露为 pre-existing；M1 后"翻绿"的根因是基线双 APK run 创建了 runs 目录（T1.1 验收记录自述），**不是测试本身被修复**。T1.5 验收记录进一步证明环境耦合：端到端验证使用默认 `data_root` 曾"污染 guard_verifier 两测试取'最新 run'受扰"。

**影响**：全量 955 passed 的结论依赖本地存在 runs 目录及其中最新 run 的内容，CI 或干净环境不可复现；测试结果随环境中的 run 变化，属不稳定测试。

**建议**：将 `_latest_index()` 改为 fixture 隔离——用 `tmp_path` 构造固定 `analysis.sqlite3`（含 guard 相关方法/事实）注入测试，消除对真实 runs 目录的 glob 依赖；T1.1 已标记"建议后续修复测试隔离"，建议在 M2 开工前随 T1.x 收尾一并修复。

### 4.2 [低-中] SPA catch-all 改变了未知 `/api/*` 路径的响应语义

**证据**：`main.py` 以 `@app.get("/{full_path:path}")` catch-all 替换原 `StaticFiles(html=True)`。实测（TestClient）：`GET /api/definitely-not-an-endpoint` → **200 + `text/html`（index.html）**。原实现下未知 API 路径返回 404；路由未匹配的 `/api/*` 现在落入 catch-all。

**影响**：API 消费者无法用 404 判定端点不存在；依赖 `ApiError.status===404` 的前端容错逻辑可能收到误导性 200 + HTML。

**建议**：catch-all 中对 `/api/` 前缀的未匹配路径返回 404 JSON（保持 API 语义），其余路径回退 index.html。

### 4.3 [低] 预算计数双源与崩溃恢复限制

**证据**：batch 预算判定用内存态 `self._ai_used` 累加（`batch.py`），`ai_skipped_count` 来自 runs 列聚合（DB）。执行中进程崩溃则内存计数丢失，恢复后新建 batch 可能超耗预算（T1.3 已声明 D6"崩溃恢复为 Phase 1 已知限制"）。

**影响**：已声明的限制，单机个人版场景可接受。

**建议**：在 `POST /api/batches` 响应或前端批次面板标注该限制；M2 前若出现真实批量崩溃场景再评估持久化计数。

### 4.4 [低] "按包批量"语义当前由 asset_ids 子集承载，包名筛选未实现

**证据**：T1.4 实施计划 D1 显式收窄——包名列表导入 Phase 1 不实现；前端 `AssetsPage` 仅资产列表 + 多选，无按包名筛选 UI。方案 L138"给定 package list 批量创建 run"的语义由 `POST /api/batches` 的任意 asset_ids 子集承载。

**影响**：资产规模变大后（如数百 APK）无包名筛选会降低可用性；当前验收标准未要求，属范围偏差已记录。

**建议**：M2 前明确真实批量场景是否需要"按包名筛选/包名列表导入"，需要则补 `GET /api/assets?package=` 查询参数与前端筛选（为后续资产规模扩展预留）。

## 5. 结论与建议

**结论**：M1 实施质量高、功能完整、测试充分，T1.6 自核查闭环修复真实缺陷（降级未生效）是本轮最大亮点；M0 审查放行建议全部闭环（基线建立、config schema 说明、T2.12 适配层条目）。**通过，建议进入 M2 准备。**

**建议（按优先级）**：

1. 修复 guard_verifier 测试环境依赖（§4.1，CI 可复现性关键）；
2. catch-all 排除 `/api/` 路径返回 404（§4.2）；
3. 明确包名筛选/导入的 M2 前决策（§4.4）；
4. 预算计数崩溃限制在 API/前端标注（§4.3，可选）。

> 备注：本报告为审查结论；审查中独立验证：全量 pytest 955 passed / 0 failed、前端 build 通过、未知 `/api/*` 路径 200 HTML 实测、guard_verifier 环境依赖代码核对。
