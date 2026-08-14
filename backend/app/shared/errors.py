"""定义可映射为稳定 HTTP 错误响应的应用异常。"""

from __future__ import annotations


class AppError(Exception):
    """携带稳定错误码、HTTP 状态和可选详情的应用异常基类。"""

    def __init__(self, message: str, code: str, status_code: int = 400, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class ValidationError(AppError):
    """表示调用方输入或受检产物不满足约束。"""

    def __init__(self, message: str, code: str = "VALIDATION_ERROR", details: dict | None = None):
        super().__init__(message, code, 422, details)


class NotFoundError(AppError):
    """表示请求的任务、发现项或任务产物不存在。"""

    def __init__(self, resource: str, resource_id: str):
        super().__init__(f"{resource} 不存在: {resource_id}", "NOT_FOUND", 404)


class ConflictError(AppError):
    """表示资源当前状态与请求操作冲突。"""

    def __init__(self, message: str, code: str = "CONFLICT"):
        super().__init__(message, code, 409)


class DependencyError(AppError):
    """表示扫描所需的外部工具或服务不可用。"""

    def __init__(self, message: str, code: str = "DEPENDENCY_UNAVAILABLE"):
        super().__init__(message, code, 503)


class RuleExecutionError(AppError):
    """表示规则执行阶段发生内部错误。"""

    def __init__(self, message: str, code: str = "RULE_FAILED", details: dict | None = None):
        super().__init__(message, code, 500, details)
