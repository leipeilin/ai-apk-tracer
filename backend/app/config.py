"""加载应用配置，并将工作区相对路径解析为受控的绝对路径。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


class SourceAnalysisSettings(BaseModel):
    """控制 DEX 反编译伪源码生成及索引规模。"""

    enabled: bool = Field(default=True, description="是否执行 DEX 反编译伪源码分析；关闭后仅保留 Manifest/资源规则")
    decompiler: str = Field(default="jadx", description="反编译适配器标识")
    jadx_path: str = Field(default="jadx", description="JADX 可执行文件名或绝对路径")
    smali_fallback: bool = Field(default=True, description="伪源码不可用时是否允许使用 Smali 兜底能力")
    max_file_size_kb: int = Field(default=512, description="普通源码文件进入结构索引的大小上限，单位 KiB")
    component_max_file_size_kb: int = Field(
        default=2048,
        description="Manifest 显式组件源码进入结构索引的大小上限，单位 KiB",
    )
    decompile_timeout_seconds: int = Field(default=600, description="JADX 单次执行墙钟超时，单位秒")


class RuleRuntimeSettings(BaseModel):
    """约束单条规则的执行时间、内存、输出与工作目录资源。"""

    max_concurrency: int = Field(default=2, ge=1, description="规则子进程最大并发数")
    wall_timeout_seconds: int = Field(default=120, description="父进程强制执行的单规则墙钟超时，单位秒")
    cpu_timeout_seconds: int = Field(default=60, description="单规则 CPU 时间限制，单位秒，平台支持时生效")
    memory_mb: int = Field(default=1024, description="单规则地址空间上限，单位 MiB，平台支持时生效")
    stdout_max_mb: int = Field(default=10, description="单规则标准输出上限，单位 MiB")
    stderr_max_mb: int = Field(default=10, description="单规则标准错误上限，单位 MiB")
    workdir_max_mb: int = Field(default=256, description="单规则可写工作目录上限，单位 MiB；共享只读索引不计入")


class StorageSettings(BaseModel):
    """定义任务存储位置及 APK 归档校验上限。"""

    data_root: Path = Field(default=Path(".ai-apk-tracer"), description="本地扫描任务及敏感产物根目录")
    retention: Literal["manual"] = Field(default="manual", description="任务保留策略；MVP 仅支持人工清理")
    stale_tmp_hours: int = Field(default=24, description="孤立临时目录可清理年龄，单位小时")
    max_apk_size_mb: int = Field(default=512, description="上传 APK 压缩包大小上限，单位 MiB")
    max_zip_entries: int = Field(default=100_000, description="APK ZIP 条目数上限")
    max_uncompressed_mb: int = Field(default=2048, description="APK 声明解压总大小上限，单位 MiB")


class AISettings(BaseModel):
    """控制外部 AI 分析的端点、模型、超时与代码外发授权。"""

    enabled: bool = Field(default=True, description="是否启用 OpenAI-compatible 深度分析")
    base_url: str | None = Field(default=None, description="OpenAI-compatible API 基础地址")
    api_key_env: str = Field(default="AI_APK_TRACER_OPENAI_API_KEY", description="保存模型密钥的环境变量名，不是密钥本身")
    model: str | None = Field(default=None, description="模型服务端可识别的模型 ID")
    allow_external_code: bool = Field(default=True, description="是否明确允许向模型服务发送方法级代码切片")
    timeout_seconds: int = Field(default=120, ge=1, description="兼容的单次模型 read 超时，单位秒")
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=600, description="建立模型连接的超时，单位秒")
    read_timeout_seconds: float = Field(default=120.0, gt=0, le=3600, description="读取模型响应的超时，单位秒")
    write_timeout_seconds: float = Field(default=30.0, gt=0, le=600, description="写入模型请求的超时，单位秒")
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=600, description="等待 HTTP 连接池的超时，单位秒")
    max_request_bytes: int = Field(default=524288, description="单次 AI 请求 HTTP body 字节上限，超过则不发请求")
    max_concurrent: int = Field(default=6, ge=1, le=64, description="进程级 AI HTTP 最大并发数")
    candidate_concurrency: int = Field(default=4, ge=1, le=32, description="单次扫描并发分析候选数上限")
    provider_max_in_flight: int = Field(default=4, ge=1, le=64, description="同一模型服务进程级最大在途请求数")
    provider_max_cooldown_seconds: float = Field(default=60.0, ge=0, le=3600, description="模型服务共享限流冷却上限，单位秒")
    retry_count: int = Field(default=1, ge=0, le=10, description="兼容配置：可恢复失败后的重试次数")
    retry_max_attempts: int | None = Field(default=None, ge=1, le=11, description="最大总发送次数；未设置时等于 retry_count+1")
    retry_base_seconds: float = Field(default=0.05, ge=0, le=60, description="指数退避基础时长，单位秒")
    retry_max_seconds: float = Field(default=30.0, ge=0, le=600, description="单次重试等待上限，单位秒")
    retry_jitter_seconds: float = Field(default=0.05, ge=0, le=60, description="单次重试随机抖动上限，单位秒")
    cache_max_entry_bytes: int = Field(default=2097152, ge=1024, le=16777216, description="单条任务本地 AI 缓存记录大小上限")
    preflight_strict_protocol: bool = Field(
        default=False,
        description="preflight 是否只接受纯 JSON 响应；默认 False 允许围栏剥离与一次 repair 后再熔断",
    )
    disable_thinking: bool = Field(
        default=True,
        description="向模型服务发送 thinking 禁用参数（deepseek-v4-flash 思维模式默认开启，推理 token 会挤占 "
        "max_tokens 导致 content 为空或截断；JSON 判定任务无需思维过程，显式关闭可显著提高稳定性）",
    )
    thinking_param: str = Field(
        default="thinking",
        description="禁用思维链的请求参数名；deepseek 兼容层为 thinking，其他服务可用 reasoning_effort",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        le=384000,
        description="单次模型输出 token 上限；None 时沿用 context_budget.max_output_tokens，"
        "供在思维链已关闭但仍需更大输出的场景覆盖",
    )

    @model_validator(mode="before")
    @classmethod
    def apply_legacy_read_timeout(cls, value: object) -> object:
        """仅在未显式配置 read timeout 时采用旧 timeout_seconds。"""

        if isinstance(value, dict) and "read_timeout_seconds" not in value and "timeout_seconds" in value:
            return {**value, "read_timeout_seconds": value["timeout_seconds"]}
        return value


class FunnelSettings(BaseModel):
    """控制高风险 L1 候选进入 AI 分诊的阈值与任务级预算。"""

    max_l1_candidates_per_run: int = Field(default=20, ge=0, description="单次任务最多进入 AI 的 L1 代表候选数")
    min_l1_risk_score: int = Field(default=80, ge=0, le=100, description="L1 候选进入 AI 分诊的最低风险分")
    demote_unproven_flow: bool = Field(
        default=False,
        description=(
            "值流未证明的链（control_to_sink 作用域未解析、legacy 回退匹配）是否降级为 "
            "signal 不占 AI 预算。默认关闭：需先完成历史回归（被降级集合中真漏洞数必须为 0）"
            "与至少 3 个不同风格 APK 的复现验证，方可开启。"
        ),
    )
    l1_priority_clean: bool = Field(
        default=False,
        description=(
            "L1 预算按可判定性排序（R-2）：receiver_flag_tier 高的（confirmed_exported_clean "
            "真实暴露面）优先进 AI 预算。默认关闭：需口径 A/B 对比确认 clean 形态判定质量后开启。"
        ),
    )


class ContextBudgetSettings(BaseModel):
    """限制单候选上下文体积、扩片次数和任务级 AI 请求量。"""

    max_input_tokens: int = Field(default=24_000, ge=256, description="单次 AI 输入的近似 token 上限")
    max_output_tokens: int = Field(default=3_000, ge=1, description="单次 AI 输出 token 预算")
    max_requests_per_candidate: int = Field(default=4, ge=1, description="单候选最多 AI 请求次数，含最终收尾")
    max_expansions_per_candidate: int = Field(default=2, ge=0, description="单候选最多成功或失败的扩片次数")
    max_candidate_wall_seconds: int = Field(default=300, ge=1, description="单候选 AI 与扩片阶段墙钟上限")
    max_requests_per_run: int = Field(default=140, ge=1, description="单次扫描最多 AI 请求次数")
    max_contexts_per_slice: int = Field(default=24, ge=1, description="送入单次 AI 请求的上下文数量上限")
    max_additions_per_request: int = Field(default=8, ge=1, description="单轮上下文扩片最多新增的上下文数量")
    max_context_bytes_per_slice: int = Field(default=96_000, ge=1, description="单个切片中 contexts 的确定性 UTF-8 JSON 字节上限")
    max_lines_per_context: int = Field(default=240, ge=8, description="单个代码上下文保留的最大行数")
    max_methods_per_class_request: int = Field(default=8, ge=1, description="单次 class/component 扩片最多加入的方法数")


class Settings(BaseSettings):
    """汇总服务配置，并支持环境变量覆盖 YAML 默认值。"""

    model_config = SettingsConfigDict(
        env_prefix="AI_APK_TRACER_",
        env_nested_delimiter="__",
        env_file=WORKSPACE_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="AI-APK-Tracer", description="应用显示名称")
    app_version: str = Field(default="0.1.0", description="扫描引擎语义版本")
    host: Literal["127.0.0.1"] = Field(default="127.0.0.1", description="本地服务固定回环监听地址")
    port: int = Field(default=8000, ge=1, le=65535, description="本地 HTTP 服务端口")
    log_level: str = Field(default="INFO", description="Python 日志等级")
    database_path: Path = Field(default=Path(".ai-apk-tracer/tracer.sqlite3"), description="任务元数据 SQLite 路径")
    analysis_platform_api: int = Field(default=36, description="Android 导出与权限语义采用的平台 API")
    source_analysis: SourceAnalysisSettings = SourceAnalysisSettings()
    rule_runtime: RuleRuntimeSettings = RuleRuntimeSettings()
    storage: StorageSettings = StorageSettings()
    ai: AISettings = AISettings()
    funnel: FunnelSettings = FunnelSettings()
    context_budget: ContextBudgetSettings = ContextBudgetSettings()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: InitSettingsSource,
        env_settings: EnvSettingsSource,
        dotenv_settings: DotEnvSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """按进程环境、.env、YAML 初始化值、模型默认值的顺序取值。"""

        del file_secret_settings
        return env_settings, dotenv_settings, init_settings

    def resolved_database_path(self) -> Path:
        """返回相对工作区解析后的数据库绝对路径。"""

        return _resolve_workspace_path(self.database_path)

    def resolved_data_root(self) -> Path:
        """返回相对工作区解析后的任务数据根目录。"""

        return _resolve_workspace_path(self.storage.data_root)


def _resolve_workspace_path(path: Path) -> Path:
    """将配置中的相对路径限定到项目工作区，绝对路径保持不变。"""

    return path if path.is_absolute() else (WORKSPACE_ROOT / path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载默认 YAML 与环境变量，并缓存单个配置实例。"""

    config_path = WORKSPACE_ROOT / "config" / "default.yaml"
    values = yaml.safe_load(config_path.read_text("utf-8")) if config_path.exists() else {}
    return Settings(**(values or {}))
