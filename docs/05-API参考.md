# 05 - API 参考

## 1. 基础约定

- 本地地址：`http://127.0.0.1:8000`
- JSON API；APK 上传使用 multipart。
- 每个请求带 trace ID；错误响应提供可追踪信息。
- v2 字段采用增量兼容：历史 run/finding 可能没有新字段，客户端必须按可选字段处理。
- run 配置快照可返回协议级 `provider_kind`，但不会返回 AI `base_url` 原文、API key 或密钥环境变量名。

## 2. 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| GET | `/ready` | 就绪检查 |
| GET | `/api/runs` | run 列表 |
| POST | `/api/runs` | 上传 APK 创建 run |
| GET | `/api/runs/{run_id}` | run、manifest、stage、版本和安全配置快照 |
| GET | `/api/runs/{run_id}/findings` | finding 列表 |
| GET | `/api/runs/{run_id}/explorer/candidates` | 探索候选人工队列（投影 + 预排序 + 计数；产物缺失返回空态） |
| GET | `/api/findings/{finding_id}/slice` | 最新方法级切片 |
| PATCH | `/api/findings/{finding_id}/review` | 更新 review |
| GET | `/api/findings/{finding_id}/report` | 生成 Markdown 报告 |
| POST | `/api/findings/{finding_id}/report-draft` | AI 报告草稿 + PoC 骨架 + 修复建议（仅 confirmed finding；目前无前端调用方） |
| POST | `/api/runs/{run_id}/cleanup` | 清理产物 |
| GET | `/api/assets` | 资产列表（assets.enabled 门控，apk_path 脱敏） |
| POST | `/api/assets/import` | 导入本地 APK 资产（201；重复 sha256 返回 409） |
| POST | `/api/batches` | 创建批量扫描（202 秒回 pending，后台跑批） |
| GET | `/api/batches/{batch_id}` | 批量进度与 runs 聚合汇总 |

## 3. POST `/api/runs`

Multipart 字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | APK |
| `authorized` | Boolean | 是 | 必须为 true |
| `source_analysis_enabled` | Boolean | 否 | 默认 true |
| `explorer_enabled` | Boolean | 否 | 任务级探索轨开关（explorer-run-toggle）；缺省沿用服务端 `explorer.enabled` 配置 |

任务级配置以落盘 manifest 的 `config` 快照为准（`config.explorer.enabled` 可审计）；HTTP 响应按脱敏设计仅透出 config 的 `ai` 段。

成功状态码为 **202 Accepted**：上传与安全入库在请求内同步完成，扫描通过后台任务异步启动。响应体为新 run，主键字段是 `id`：

```json
{
  "id": "20260804T120000Z_...",
  "status": "queued",
  "stage": "queued",
  "config": {
    "ai": {
      "enabled": true,
      "allow_external_code": true,
      "provider_kind": "openai-compatible",
      "model": "configured-model"
    }
  }
}
```

`config.ai` 是安全快照。当前 API 写入 `enabled`、`allow_external_code`、`provider_kind`、`model`；不返回 provider URL、API key 或密钥环境变量名。当前 `provider_kind=openai-compatible` 是后端返回的协议族标识，不是具体供应商身份；历史 run 缺失时前端显示“未记录”。

## 4. GET `/api/runs/{run_id}`

v2 响应关键字段：

```json
{
  "id": "...",
  "status": "completed",
  "stage": "completed",
  "pipeline_version": "2.0.0",
  "schema_version": "2.0.0",
  "artifact_schema_versions": {
    "candidate": "2.0.0",
    "report_payload": "2.0.0"
  },
  "config": {
    "ai": {
      "enabled": true,
      "allow_external_code": true,
      "provider_kind": "openai-compatible",
      "model": "configured-model"
    }
  },
  "stages": [
    {
      "name": "candidate_funnel",
      "status": "completed",
      "summary": {
        "candidate_count": 100,
        "identity_group_count": 80,
        "deduplicated_count": 20,
        "ai_representative_count": 25,
        "l1_ai_selected_count": 10,
        "l1_ai_deferred_count": 5
      }
    },
    {
      "name": "ai_analysis",
      "status": "completed",
      "summary": {
        "analyzed": 25,
        "completed": 22,
        "failed": 1,
        "incomplete": 2,
        "peak_concurrent": 4
      }
    }
  ],
  "manifest": {
    "analysis_incomplete": false,
    "coverage_gaps": []
  }
}
```

Stage summary 允许增加字段；客户端不能只识别固定键。当前 `ai_analysis` 汇总 `preflight`、`circuit_open`、`analyzed`、`completed`、`failed`、`skipped`、`incomplete` 和可选 `circuit_reason`；正常进入候选调度时还包含 `peak_concurrent`，无候选、无 context 或 preflight 失败的早退路径可能缺失，前端应显示“未汇总”。cache hit、统一 stop reason、L1/L2 分轨统计尚未由该 stage summary 汇总，不能补造。L1 selected/deferred 应从 `candidate_funnel` summary 读取。

## 5. GET `/api/runs/{run_id}/findings`

响应外层为 `{ "items": [...] }`。v2 finding 示例：

```json
{
  "id": "...",
  "run_id": "...",
  "pipeline_version": "2.0.0",
  "schema_version": "2.0.0",
  "title": "...",
  "severity": "pending",
  "evidence_level": "L1",
  "funnel_disposition": "exposure_only",
  "analysis_status": "ai_completed",
  "analysis_track": "l1_triage",
  "candidate_verdict": "exposure_only",
  "ai_analysis": {
    "analysis_track": "l1_triage",
    "triage_disposition": "exposure_only",
    "candidate_verdict": "exposure_only"
  },
  "evidence_decision": "exposure_only",
  "fact_integrity_status": "verified",
  "semantic_status": "not_applicable",
  "exploitability_status": "pending",
  "review_status": "pending_manual",
  "review_state": {
    "status": "pending_manual",
    "reason": "automatic_analysis_requires_manual_review",
    "evidence_decision": "exposure_only"
  },
  "status_layers": {
    "funnel": "exposure_only",
    "analysis": "ai_completed",
    "evidence": "exposure_only",
    "review": "pending_manual"
  },
  "ai_stop_reason": "analysis_complete",
  "ai_analysis_trace": [{"result": {"metadata": {"cache_hit": false, "prompt_version": "2.0.1"}}}],
  "coverage_gaps": [],
  "blocking_gaps": []
}
```

重要语义：

- L1 是 exposure/attack-surface fact，不是漏洞声明。
- `semantic_status=closed` 不是漏洞成立。
- AI 字段（包括 finalization verdict/review recommendation）是 observation；确定性事实和 decision 字段仍是裁决基础。
- L1 的原始 `triage_disposition` 位于 `ai_analysis`；顶层兼容字段主要是 `analysis_track` 与 `candidate_verdict`，客户端不应假设所有 observation 都被提升到顶层。
- cache/Prompt/Schema 元数据通常位于 `ai_analysis_trace[*].result.metadata`，顶层字段仅作可选兼容读取。
- 顶层 `provider`/`provider_kind`/`model` 当前不保证写入 finding；缺失时客户端应显示“未记录”，协议级 provider_kind/model 可另查 run 安全快照。
- gap 为空只说明已记录范围内无缺口，不自动表示动态可利用。

## 6. GET `/api/findings/{finding_id}/slice`

```json
{
  "finding_id": "...",
  "slice_id": "slice_...",
  "round_count": 2,
  "latest_round": "round-001.json",
  "source": "live_slice",
  "slice": {
    "contexts": [],
    "edges": [],
    "guards": [],
    "request_history": [],
    "unresolved": [],
    "limitations": []
  }
}
```

中间产物已清理时可从报告证据副本返回，`source=report_evidence`。

## 7. PATCH `/api/findings/{finding_id}/review`

请求：

```json
{
  "status": "confirmed",
  "reason": "确定性 Source→Sink、授权与 Guard 证据已人工复核",
  "expected_status": "pending_manual",
  "request_id": "review-unique-id",
  "actor": "human",
  "basis": "optional structured basis"
}
```

状态：`pending_ai`、`pending_manual`、`ai_false_positive`、`manual_false_positive`、`confirmed`。

- `confirmed` 和 `manual_false_positive` 必须有非空 `reason`。
- `expected_status` 支持乐观并发。
- `request_id` 支持幂等。
- 人工确认不改变动态验证状态。
- `expected_status` 不匹配或 `request_id` 被不同请求复用时返回 409；相同 `request_id` 和相同 payload 重放返回当前 finding。

当前前端发送 `status`、`reason`、finding 当前原始状态作为 `expected_status`，并为每次操作生成 `request_id`；该状态可以是规范值，也可以是 API 明确允许的 legacy 值 `pending`、`false_positive`、`ai_candidate`。发生 409 时会刷新 finding 并要求用户重新确认。`actor` 和 `basis` 当前由 API 支持，但 UI 未采集；legacy 值只作并发匹配与兼容显示，不能据此反推历史结论来自 AI 还是人工。

## 8. GET `/api/findings/{finding_id}/report`

返回 Markdown。报告包含 pipeline/schema/prompt 版本、四层状态、确定性事实、AI observation、coverage gap 和人工 review。

L1 exposure 不应被渲染成正式漏洞；接口会依据当前 finding 证据等级和报告规则拒绝不满足条件的请求。

## 9. POST `/api/runs/{run_id}/cleanup`

```json
{"mode": "prune_intermediates", "confirm_delete": false}
```

模式：

- `prune_intermediates`
- `clear_sensitive_content`
- `delete_run`（必须 `confirm_delete=true`）

单 run 的三种 cleanup 只处理该 run 目录及相应数据库记录，不会删除数据根目录下的共享 `ai-cache`。共享 cache 只有通过独立的显式管理调用并确认后才能清理，当前 HTTP cleanup 端点不提供该模式。

`prune_intermediates` 会删除该 run 的 `ai-cache` 兼容结果、`ai-trace`、切片、索引等中间目录；finding/报告元数据仍可能保留 Prompt/Schema 版本、hash、stop reason 和摘要，但不会保留密钥。`clear_sensitive_content` 还会删除 findings/reports 并清空数据库 finding；`delete_run` 删除整个 run。外部 provider 已接收的数据不受本地 cleanup 控制。

## 10. GET `/api/runs/{run_id}/explorer/candidates`

探索候选人工队列（探索轨候选的复核入口视图）。主体为 `partially_validated`、`unverified`、`pending` 档位候选；`validated` 已并入主链 findings，仅出现在 `counts` 作计数对照。响应为投影视图：不携带 hops 全文与逐轮审计，防止响应膨胀。

```json
{
  "entries": [
    {
      "candidate_id": "...",
      "component": {"kind": "activity", "name": "...", "entry_method": "..."},
      "chain": {"source": "...", "sink": "...", "hop_count": 2},
      "validation": {
        "status": "partially_validated",
        "verified_hop_count": 1,
        "failed_hop_indices": [],
        "blocked_by_guard": false,
        "custom_sink_proposal": false,
        "notes": null
      },
      "deep_dive": {
        "status": "completed",
        "evidence_count": 4,
        "confirmed_fact_count": 2,
        "remaining_gap_count": 1,
        "unverifiable_evidence_count": 1,
        "evidence_truncated_count": 0,
        "requests_used": 3
      },
      "confidence": "high",
      "sort_keys": {"confidence_rank": 3, "deep_dive_evidence": 4, "hop_ratio": 0.5}
    }
  ],
  "counts": {
    "validated": 5,
    "partially_validated": 2,
    "unverified": 3,
    "pending": 1,
    "total": 11,
    "queue_length": 6,
    "deep_dive_completed": 4
  }
}
```

字段语义：

- `entries[].component` 的 `kind`/`name`/`entry_method` 与 `candidate_id`、`chain.source`/`chain.sink`、`validation.notes`、`confidence` 均可为 null，客户端按可选处理。
- `validation.status` 取值 `partially_validated`、`unverified`、`pending`（未知档位归一化为 `pending`）；`verified_hop_count` 为整数或 null；`blocked_by_guard`、`custom_sink_proposal` 为布尔。
- `deep_dive` 为 null 表示该候选未做深挖；非 null 时 `status` 取候选深挖的原始状态，`counts.deep_dive_completed` 只统计 `status=completed` 的深挖。
- `sort_keys` 是服务端排序依据的透出：`confidence_rank`（high=3 / medium=2 / low=1 / 未知=0）、`deep_dive_evidence`（深挖证据引用数）、`hop_ratio`（已验证跳数 / 总跳数，无跳或未验证为 0）。
- 排序为服务端预排序：置信度降序 → 深挖证据数降序 → 跳回查完整度降序 → `candidate_id` 稳定序；客户端不应依赖自身重排。

计数语义：`counts.total` 为产物文件中全部候选数（含 validated），`queue_length` 为 `entries` 长度（仅入队档位）。

空态语义：run 不存在返回 404；run 存在但 `explorer/candidates.json` 缺失或损坏时返回空态（`entries=[]`、`counts` 全 0），不返回 404——探索轨未启用是常态。

```json
{"entries": [], "counts": {"validated": 0, "partially_validated": 0, "unverified": 0, "pending": 0, "total": 0, "queue_length": 0, "deep_dive_completed": 0}}
```

前端 RunDetailPage 在 run 活跃期间以 2 秒间隔轮询本端点（与 findings 轮询共用同一活跃判定），以捕捉探索阶段末尾落盘的产物。

## 11. POST `/api/findings/{finding_id}/report-draft`

生成 AI 报告草稿 + PoC 骨架 + 修复建议（AI 草稿与确定性证据分离展示）。目前无前端调用方，属后端/手工端点。

安全门禁（不满足即拒绝，最保守语义）：

- **仅 confirmed finding**：`review_status` 非 `confirmed` 返回 409 `REPORT_DRAFT_REQUIRES_CONFIRMED`（`report.require_confirmed_finding` 默认 true）。
- **L1 / informational 拒绝**：`evidence_level=L1` 或 `severity=informational` 返回 409 `L1_REPORT_FORBIDDEN`（与确定性报告同一先例：L1 提示项不进入正式漏洞报告）。
- **禁可执行 PoC**：`report.allow_executable_poc` 必须保持 false，置真视为配置违例直接拒绝（422 `EXECUTABLE_POC_FORBIDDEN`）；PoC 仅为步骤说明与占位符命令骨架文本，`poc_skeleton.executable_files_created` 恒为空列表（schema 级强制，非生成器约定）。

成功返回 200，响应体为报告文档（同时落盘 `run_dir/reports/drafts/{finding_id}.json`，目录 0o700、文件 0o600，含 symlink 防护）：

```json
{
  "finding_id": "...",
  "run_id": "...",
  "generated_at": "2026-08-29T08:00:00Z",
  "evidence_source": "rule_candidate",
  "explorer_caveat": null,
  "deterministic": {
    "rule_id": "...",
    "severity": "high",
    "review_status": "confirmed",
    "evidence_level": "L2",
    "sources": [],
    "sinks": [],
    "guard_status": "..."
  },
  "ai_draft": {
    "summary": "...",
    "narrative": "...",
    "exploit_scenario": "...",
    "confidence_tier": "medium",
    "provenance": "ai_report_protocol",
    "prompt_version": "...",
    "model": "...",
    "analysis_complete": true
  },
  "poc_skeleton": {
    "component_kind": "activity",
    "kind": "intent",
    "steps": ["确认目标组件 exported", "构造测试意图触发入口"],
    "command_skeleton": ["adb shell am start -n <PACKAGE>/<ACTIVITY>"],
    "notes": ["本骨架仅为验证步骤说明，不包含任何可执行文件"],
    "executable_files_created": []
  },
  "repair": {
    "deterministic_recommendations": ["按规则/组件类型的确定性建议"],
    "ai_recommendations": [],
    "ai_rationale": null
  }
}
```

字段语义：

- `deterministic` 为 finding 确定性字段的原样投影（`rule_id`、`severity`、`sources`、`sinks`、`locations`、`guard_status`、`review_state` 等固定字段集合），缺失字段为 null，服务端不改写。
- `ai_draft.provenance` 取 `ai_report_protocol`（真实 AI 协议生成）或 `projected_from_l2_review`（从 L2 已验证输出投影的兜底草稿）；AI 失败或输出不符合严格契约时自动降级为投影，报告永不因 AI 阻塞，降级详情附于 `ai_draft.fallback`。
- `evidence_source` 取 `rule_candidate` 或 `explorer_candidate`；explorer 来源时注入 `explorer_caveat` 置信度告警（探索质量未达标期间探索候选证据置信度低于规则候选）。
- `confidence_tier` 取 `low`/`medium`/`high`。

错误码：

| 状态码 | code | 条件 |
|---|---|---|
| 404 | `NOT_FOUND` | finding 不存在 |
| 409 | `REPORT_DRAFT_REQUIRES_CONFIRMED` | `review_status` 非 `confirmed` |
| 409 | `L1_REPORT_FORBIDDEN` | `evidence_level=L1` 或 `severity=informational` |
| 422 | `EXECUTABLE_POC_FORBIDDEN` | 配置 `report.allow_executable_poc=true` |
| 422 | `FINDING_ID_MISSING` / `FINDING_ID_INVALID` | finding 缺稳定 ID / ID 含非法字符（防路径注入） |

## 12. 资产：GET `/api/assets` 与 POST `/api/assets/import`

两个端点共用 `assets.enabled` 门禁（默认 false）：未启用时返回 **503 ASSETS_DISABLED**——语义是功能未启用，不是请求校验失败（422）或资源不存在（404）。错误响应沿用统一结构：

```json
{
  "error": {"code": "ASSETS_DISABLED", "message": "资产批量功能未启用（assets.enabled=false）", "details": {}},
  "trace_id": "..."
}
```

### GET `/api/assets`

按创建时间倒序返回资产列表，响应外层为 `{ "items": [...] }`。`apk_path` 为服务端路径，API 层脱敏剔除，不出现在响应中：

```json
{
  "items": [
    {
      "id": "20260828T093000Z_1a2b3c4d5e6f_7a8b9c0d",
      "package_name": "com.example.app",
      "apk_filename": "example.apk",
      "apk_sha256": "1a2b...",
      "source": "local_upload",
      "status": "ready",
      "last_run_id": null,
      "created_at": "2026-08-28T09:30:00+00:00",
      "updated_at": "2026-08-28T09:30:00+00:00"
    }
  ]
}
```

- `id` 与 run id 同风格（`{UTC时间戳}_{sha256[:12]}_{uuid[:8]}`），注意与 run id 区分。
- `status` 取 `ready`/`scanning`/`error`；`source` 当前固定 `local_upload`；`last_run_id` 为 null 或最近一次 run 的 id。

### POST `/api/assets/import`

导入本地 APK 资产（同步注册：流式副本 + sha256/大小/ZIP 校验）。成功返回 **201 Created**，响应体为资产记录（与 `GET /api/assets` 的 items 元素一致，不含 `apk_path`）。

multipart 字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | APK（仅 `.apk` 扩展名） |
| `package_name` | String | 是 | 非空 |
| `authorized` | Boolean | 是 | 必须为 true |

错误码：

| 状态码 | code | 条件 |
|---|---|---|
| 422 | `AUTHORIZATION_CONFIRMATION_REQUIRED` | `authorized` 非 true（与 POST /api/runs 同级授权语义） |
| 422 | `PACKAGE_NAME_REQUIRED` | `package_name` 为空 |
| 422 | `INVALID_APK_EXTENSION` / `INVALID_APK_FILENAME` | 非 `.apk` 扩展名 / 文件名含路径分隔符 |
| 422 | `APK_TOO_LARGE` | 超过大小上限（与 run 上传同源限制） |
| 409 | `ASSET_ALREADY_REGISTERED` | 重复 sha256；`details.asset_id` 指向既有资产（供前端跳转），`details.apk_sha256` 为重复哈希 |

重复 sha256 返回 409 时保留已注册资产与其内容寻址副本（同 sha256 内容必然一致，天然幂等），不做清删。

## 13. 批量扫描：POST `/api/batches` 与 GET `/api/batches/{batch_id}`

两个端点同样受 `assets.enabled` 门控（未启用返回 503 `ASSETS_DISABLED`，见 §12）。

### POST `/api/batches`

创建批量扫描。成功返回 **202 Accepted**：请求内仅创建 batch 行并固化资产快照（秒回 `pending`），逐资产扫描经后台任务异步启动（batch 内 run 的 trace_id 即 batch_id，便于按批次聚合审计）。

JSON 请求体：

```json
{
  "authorized": true,
  "asset_ids": ["20260828T093000Z_1a2b3c4d5e6f_7a8b9c0d"]
}
```

- `authorized`：Boolean 必填，必须为 true，否则 422 `AUTHORIZATION_CONFIRMATION_REQUIRED`。
- `asset_ids`：字符串数组必填，1..100 项（越界返回 422）；重复 id 服务端去重保序，去重后为空返回 422 `BATCH_ASSETS_REQUIRED`；引用的资产不存在返回 404 `NOT_FOUND`。

响应为 batch 记录（原始 `assets_json` 列已剔除，解析后的 `assets` 快照在内）：

```json
{
  "id": "20260828T100000Z_112233445566_778899aa",
  "status": "pending",
  "max_ai_calls": 0,
  "max_wall_seconds": 0,
  "ai_skipped_count": 0,
  "assets": [
    {"asset_id": "20260828T093000Z_1a2b3c4d5e6f_7a8b9c0d", "package_name": "com.example.app", "apk_sha256": "1a2b..."}
  ],
  "created_at": "2026-08-28T10:00:00+00:00",
  "updated_at": "2026-08-28T10:00:00+00:00",
  "completed_at": null,
  "total_runs": 0,
  "completed_runs": 0,
  "failed_runs": 0,
  "ai_skipped": 0,
  "ai_skipped_by_budget": 0,
  "ai_skipped_by_wall_clock": 0
}
```

`assets` 为创建时固化的快照数组（`asset_id`/`package_name`/`apk_sha256`），资产删除后审计信息仍可回溯。`max_ai_calls`/`max_wall_seconds` 为创建时刻的 batch 预算帽快照（0 表示沿用 run 级预算 / 不限墙钟）。

### GET `/api/batches/{batch_id}`

返回批量进度与 runs 聚合汇总，响应结构与 POST `/api/batches` 一致（同样剔除 `assets_json` 原始列）。`batch_id` 不存在返回 404 `NOT_FOUND`。

- `status` 状态机：`pending` → `running` → `completed` / `partial` / `failed`；终态由 runs 聚合判定（无失败为 `completed`，全部失败为 `failed`，其余为 `partial`）。
- `total_runs`/`completed_runs`/`failed_runs` 为该 batch 下 runs 的聚合事实源；`ai_skipped` 为 AI 被跳过（降级仅确定性主链）的 run 数。
- `ai_skipped_by_budget` 与 `ai_skipped_by_wall_clock` 为降级原因分解：预算耗尽（`batch_budget`）或墙钟超限（`batch_wall_clock`）的 run 分别以 AI 关闭配置只跑确定性主链，原因记录在 run 自身配置中。
- `ai_skipped_count` 是 batch 行上落库的累计值，终态时与聚合值一致；进行中二者可能短暂不一致，以聚合字段为准。

## 14. v1 兼容

历史数据可能缺少所有 v2 新字段，或使用旧 review 值 `pending`、`false_positive`、`ai_candidate`。读取端应兼容显示；迁移端不得在无法证明人工来源时擅自改写结论。
