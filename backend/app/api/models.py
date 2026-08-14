"""定义 API 请求模型及跨字段业务校验。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ReviewStatus(StrEnum):
    """发现项的复核状态。"""

    pending_ai = "pending_ai"
    pending_manual = "pending_manual"
    ai_false_positive = "ai_false_positive"
    manual_false_positive = "manual_false_positive"
    confirmed = "confirmed"


class LegacyReviewStatus(StrEnum):
    """仅用于旧记录的乐观并发校验，不接受为新的复核结论。"""

    pending = "pending"
    false_positive = "false_positive"
    ai_candidate = "ai_candidate"


class ReviewRequest(BaseModel):
    """提交发现项复核状态及可选理由。"""

    status: ReviewStatus
    reason: str | None = Field(default=None, max_length=4000)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    expected_status: ReviewStatus | LegacyReviewStatus | None = None
    basis: str | None = Field(default=None, max_length=4000)
    actor: str = Field(default="human", min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_review_reason(self):
        """要求人工确认和误报结论都附带可审计的非空理由。"""

        if self.status in {ReviewStatus.confirmed, ReviewStatus.manual_false_positive} and not (
            self.reason and self.reason.strip()
        ):
            raise ValueError("人工确认或标记误报时必须填写 reason")
        return self


class CleanupMode(StrEnum):
    """任务数据的受支持清理级别。"""

    prune_intermediates = "prune_intermediates"
    clear_sensitive_content = "clear_sensitive_content"
    delete_run = "delete_run"


class CleanupRequest(BaseModel):
    """指定清理级别及完整删除的显式确认。"""

    mode: CleanupMode
    confirm_delete: bool = False
