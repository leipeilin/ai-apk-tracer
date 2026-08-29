"""run 级扫描配置构造（routes 单 run 与 batch 批量编排共享，T1.3 评审 D4）。

从 routes.create_run 的内联构造提取（行为等价，golden 测试固化期望）；
batch 预算/墙钟降级通过 ai_enabled=False + ai_skip_reason 注入（复用
ai.enabled=false 跳过 AI 阶段的既有行为，方案 §Phase1 预算降级语义）。
"""

from __future__ import annotations

from typing import Any


def build_run_config(
    settings: Any,
    *,
    source_analysis_enabled: bool = True,
    ai_enabled: bool | None = None,
    ai_skip_reason: str | None = None,
    explorer_enabled: bool | None = None,
) -> dict[str, Any]:
    """构造写入 run manifest 的扫描配置快照。

    - source_analysis_enabled：调用方开关（API form 字段）；
    - ai_enabled：None 时沿用 settings.ai.enabled；显式 False 用于 batch
      预算/墙钟降级（跳过 AI 仅确定性主链）；
    - ai_skip_reason：降级原因附注（'batch_budget'/'batch_wall_clock'），
      进 manifest 可审计（评审 R-4 的原因分解依据）；
    - explorer_enabled：None 时沿用 settings.explorer.enabled；显式值来自
      API form 字段（explorer-run-toggle：任务级探索轨开关，orchestrator
      门禁按本快照执行——对齐 source_analysis_enabled 的 run 级模式）。
    """

    ai_section: dict[str, Any] = {
        "enabled": settings.ai.enabled if ai_enabled is None else ai_enabled,
        "allow_external_code": settings.ai.allow_external_code,
        "provider_kind": "openai-compatible",
        "model": settings.ai.model,
    }
    if ai_skip_reason is not None:
        ai_section["skip_reason"] = ai_skip_reason
    return {
        "analysis_platform_api": settings.analysis_platform_api,
        "source_analysis": {
            **settings.source_analysis.model_dump(mode="json"),
            "enabled": source_analysis_enabled,
        },
        "ai": ai_section,
        # 全量 dump + enabled 覆盖（与 source_analysis 段同构）：其余 explorer
        # 参数仅如实记录全局值供审计，不开放任务级覆盖
        "explorer": {
            **settings.explorer.model_dump(mode="json"),
            "enabled": settings.explorer.enabled if explorer_enabled is None else explorer_enabled,
        },
    }
