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
    read_timeout_seconds: float = Field(default=240.0, gt=0, le=3600, description="读取模型响应的超时，单位秒（P-1 验证值 240——原 120 对慢推理模型不足，全量数据后回归定参）")
    write_timeout_seconds: float = Field(default=30.0, gt=0, le=600, description="写入模型请求的超时，单位秒")
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=600, description="等待 HTTP 连接池的超时，单位秒")
    request_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=7200,
        description=(
            "单次模型 HTTP 请求总时长兜底（墙钟）——防御中间层 keepalive 重置分项超时的"
            "长挂起（M2-DEFECT-FIX D-2）；超时归入可重试 network 失败。"
            "None 时动态取 read_timeout_seconds + 60"
        ),
    )
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

    @model_validator(mode="after")
    def derive_request_timeout(self) -> AISettings:
        """总时长兜底未显式配置时随 read 超时缩放（评审 R-2——硬编码会在
        read_timeout 配大时先于分项超时触发，误杀正常长响应）。"""

        if self.request_timeout_seconds is None:
            self.request_timeout_seconds = self.read_timeout_seconds + 60.0
        return self


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
    l1_skip_ai: bool = Field(
        default=True,
        description=(
            "L1 informational 候选默认不进 AI（建议 2）：L1 承载'暴露事实/无法判定'而非"
            "'漏洞成立'（实证：AI 在 L1 上 0 supported、漏洞报告全来自 L2），coverage_insufficient/"
            "deterministically_refuted 形态不占预算。例外保留 AI 有可判定输入的面："
            "receiver_flag_tier=confirmed_exported_clean 与 funnel_disposition 在 "
            "{exposure_only, high_risk_uncertain}（其他规则族 L1 确定性暴露面）。"
            "false 完全回退旧行为。"
        ),
    )
    l2_ai_undecidable_route: bool = Field(
        default=False,
        description=(
            "S4（2026-08-16）：L2 AI 预算按可判定性路由——纯运行期目标/符号歧义且无任何"
            "AI 可补证据的候选不进 AI（白烧预算）。默认关闭：需先对历史 run 的 unresolved "
            "逐条归类形成路由表并校准阈值（方案 §S4 v2 前置）后开启；开启后 WebView URL "
            "来源等'切片可补证据'候选仍送 AI，不受影响。"
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


class CallTreeSettings(BaseModel):
    """call_tree on-demand 有界构建预算（方案 §2.2）。"""

    max_depth: int = Field(default=8, ge=1, description="按入口构建调用树的最大深度")
    max_nodes: int = Field(default=500, ge=1, description="按入口构建调用树的最大节点数")


class ExplorerSettings(BaseModel):
    """探索轨（Agent1）开关与循环预算（方案 §2.4/§5.5）。"""

    enabled: bool = Field(default=False, description="是否启用探索轨；默认关闭，开启前须过 M2 三加一验收")
    max_candidates_per_run: int | None = Field(default=50, ge=1, description="单次扫描最多纳入 funnel 的探索候选数；None = 无上限（P-1 验证阶段临时形态——采集无截断数据后须回归定参恢复，见 docs/todo/ T1）")
    auto_promote: bool = Field(default=False, description="validated 探索候选是否自动升入正式候选池；默认 false（走 L2 复核）")
    allow_external_code: bool = Field(default=True, description="是否允许向模型发送探索检索读回的代码片段（仅方法级片段，非完整代码索引）")
    prompt_version: str = Field(default="explorer/1.0.0", description="探索协议版本；先声明后注册（T2.5），注册前不得运行时解析")
    max_rounds_per_entry: int = Field(default=4, ge=1, description="单入口检索循环轮数上限（评审 §4.3）")
    max_requests_per_entry: int = Field(default=20, ge=1, description="单入口读码请求总数上限（评审 §4.3）")
    max_requests_per_candidate: int = Field(default=4, ge=1, description="单探索候选的 AI 请求上限")
    deep_dive_prompt_version: str = Field(default="explorer-deep-dive/1.0.0", description="partial 候选深挖协议版本（T0.3 已注册）")
    custom_sink_taxonomy_path: Path | None = Field(
        default=None,
        description=(
            "sink taxonomy 版本化文件路径（T2.9）；None=自动发现 "
            "rules/sink_taxonomy/versions.yaml（存在即启用判定——文件缺失/"
            "损坏/路径不存在即禁用，兼容保守行为）。关闭=删除文件或将路径"
            "指向不存在的位置"
        ),
    )
    call_tree: CallTreeSettings = CallTreeSettings()


class VerifySettings(BaseModel):
    """核验 agent（L2 agent 化演进）开关与预算（方案 §2.7）。"""

    enabled: bool = Field(default=False, description="是否启用核验 agent 替代单轮 L2；默认关闭")
    prompt_version: str = Field(default="verify/1.0.0", description="核验协议版本；先声明后注册（T0.9），注册前不得运行时解析")
    max_rounds_per_candidate: int = Field(default=4, ge=1, description="单候选取证循环轮数上限")
    max_requests_per_candidate: int = Field(default=12, ge=1, description="单候选读码请求总数上限")
    fallback_to_single_turn_l2: bool = Field(default=True, description="agent 失败/预算耗尽时回退现有单轮 L2（主链不阻塞）")


class ApiSurfaceSettings(BaseModel):
    """API 入口表生成开关（方案 §2.1）。"""

    enabled: bool = Field(default=False, description="是否生成 api_entry_table 产物；默认关闭")
    include_binder: bool = Field(default=True, description="是否包含 Binder 入口（读 binder_bindings 产物）")
    include_webview_jsbridge: bool = Field(default=True, description="是否包含 WebView bridge 入口（读 webview_js_bridges 产物）")


class AssetsSettings(BaseModel):
    """资产注册与批量扫描（Phase 1）。"""

    enabled: bool = Field(default=False, description="是否启用资产/批量扫描；默认关闭")
    max_concurrent_runs: int = Field(default=2, ge=1, description="资产扫描层并发 run 上限（资产级，区别于 batch 段批内并发）")
    data_root: Path = Field(default=Path(".ai-apk-tracer/assets"), description="资产元数据与 APK 副本根目录（相对路径以工作区为基准，经 resolved_assets_data_root 解析）")


class BatchSettings(BaseModel):
    """batch 级预算帽（评审 §4.12：Phase 1 即生效）。"""

    max_concurrent_runs: int = Field(default=2, ge=1, description="批内 run 并发上限（batch 级，区别于 assets 段资产扫描并发）")
    max_ai_calls: int = Field(default=0, ge=0, description="batch 总 AI 预算帽；0=沿用 run 级（默认）；>0 超限降级为仅确定性主链")
    max_wall_seconds: int = Field(default=0, ge=0, description="batch 墙钟上限；0=不限（默认）")


class ReportSettings(BaseModel):
    """漏洞报告与 PoC 骨架（Phase 3）。"""

    allow_executable_poc: bool = Field(default=False, description="是否允许生成可执行 PoC；默认禁止（仅骨架）")
    require_confirmed_finding: bool = Field(default=True, description="仅已确认 finding 可触发报告生成")
    prompt_version: str = Field(default="report/1.0.0", description="报告协议版本；先声明后注册（T0.9），注册前不得运行时解析")


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
    explorer: ExplorerSettings = ExplorerSettings()
    verify: VerifySettings = VerifySettings()
    api_surface: ApiSurfaceSettings = ApiSurfaceSettings()
    assets: AssetsSettings = AssetsSettings()
    batch: BatchSettings = BatchSettings()
    report: ReportSettings = ReportSettings()

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

    def resolved_assets_data_root(self) -> Path:
        """返回相对工作区解析后的资产数据根目录（Phase 1）。"""

        return _resolve_workspace_path(self.assets.data_root)


def _resolve_workspace_path(path: Path) -> Path:
    """将配置中的相对路径限定到项目工作区，绝对路径保持不变。"""

    return path if path.is_absolute() else (WORKSPACE_ROOT / path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """加载默认 YAML 与环境变量，并缓存单个配置实例。"""

    config_path = WORKSPACE_ROOT / "config" / "default.yaml"
    values = yaml.safe_load(config_path.read_text("utf-8")) if config_path.exists() else {}
    return Settings(**(values or {}))
