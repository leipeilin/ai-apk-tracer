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
| GET | `/api/findings/{finding_id}/slice` | 最新方法级切片 |
| PATCH | `/api/findings/{finding_id}/review` | 更新 review |
| GET | `/api/findings/{finding_id}/report` | 生成 Markdown 报告 |
| POST | `/api/runs/{run_id}/cleanup` | 清理产物 |

## 3. POST `/api/runs`

Multipart 字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | APK |
| `authorized` | Boolean | 是 | 必须为 true |
| `source_analysis_enabled` | Boolean | 否 | 默认 true |

响应为新 run，主键字段是 `id`：

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

## 10. v1 兼容

历史数据可能缺少所有 v2 新字段，或使用旧 review 值 `pending`、`false_positive`、`ai_candidate`。读取端应兼容显示；迁移端不得在无法证明人工来源时擅自改写结论。
