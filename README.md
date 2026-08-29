# AI-APK-Tracer

本地单用户的 Android APK 安全自动化静态分析工具：确定性规则与索引发现攻击面，方法级切片向 AI 提供受控上下文，AI 只返回 strict observation，证据回查与最终判定由确定性代码和人工复核负责。

核心目标：**提高真实漏洞检出准确率，降低误报率与人工复核成本。**

## 架构

```
APK → basic_check → decompiling + index → rule_prescan
    →（确定性 guard 验证，非独立 stage）
    → api_surface → attack_surface（可选，api_surface.enabled 门控，默认关闭）
    → explorer（可选探索轨，默认关闭）→ candidate_funnel
    → code_slicing → ai_analysis（L1 triage / L2 review，或可选 verify 核验）
    → evidence validation + decision → aggregation → manual review
```

- **规则层**（确定性）：34 条内置规则覆盖 Activity / Service / ContentProvider / BroadcastReceiver 四大组件及 WebView、密码学、Manifest、Intent（PendingIntent）、日志攻击面
- **探索轨**（可选，默认关闭）：以规则预扫产物为引导主动检索代码，validated 候选与规则候选同走 funnel，未通过校验的探索候选不进入正式 finding
- **Candidate funnel**：确定性路由、chain identity 精确去重、AI 预算选择
- **切片层**：受预算约束的方法级上下文，结构化扩片请求
- **AI 层**：L1 triage / L2 review / finalization / 探索轨 / 核验 / 报告草稿只输出 strict observation（Pydantic strict + JSON Schema + Prompt 版本/SHA 门禁）
- **证据/决策层**：事实回查、dataflow、Guard、授权与最终 decision 由确定性代码独立裁决

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 + FastAPI + uvicorn + Pydantic strict（`backend/app/`） |
| 前端 | React 19 + Vite 7 + Tailwind 4（`frontend/`） |
| 外部工具 | JADX 反编译、Android SDK platform-tools、OpenAI-compatible AI provider |
| 规范 | 32 个 JSON Schema、版本化 Prompt registry（`prompts/`，9 个 Prompt ID）、SQLite 代码索引 |

## 目录导航

| 目录 | 作用 | 说明 |
|---|---|---|
| [`backend/`](backend/) | 后端实现 | FastAPI 应用（`app/`）、pytest 测试（`tests/`）、依赖锁定（`requirements.txt` / `pyproject.toml`） |
| [`frontend/`](frontend/) | 前端实现 | React 19 + Vite 7 + Tailwind 4 控制台 |
| [`rules/`](rules/) | 规则包 | 34 条内置确定性规则：Activity / Service / ContentProvider / BroadcastReceiver + WebView / 密码学 / Manifest / Intent / 日志（`shared/` 为共享检测逻辑，`sink_taxonomy/` 为 sink 分类单一事实源） |
| [`prompts/`](prompts/) | AI 提示词 | 版本化 prompt registry（9 个 Prompt ID）：preflight / l1-triage / l2-review / finalization / repair / explorer / explorer-deep-dive / verify / report |
| [`schemas/`](schemas/) | JSON Schema | AI 输入输出与产物结构规范（32 个） |
| [`config/`](config/) | 配置 | 默认配置 `default.yaml`（funnel / AI / 上下文预算 / explorer / verify / api_surface / assets / batch / report / 清理） |
| [`scripts/`](scripts/) | 脚本 | 开发与统一校验入口（如 `check-all.sh`、`dev-backend.sh`） |
| [`tools/`](tools/) | 工具 | POC 与验证工具（commonbase-activity-poc、sportxms-poc） |
| [`evaluation/`](evaluation/) | 评估 | golden 集与探索轨命中标注（`golden/`）、基线快照（`baselines/`） |
| [`tests/`](tests/) | 测试 | 根级测试 |
| [`docs/`](docs/) | 系统说明文档 | 编号体系：项目概述、架构、分析流程、规则体系、API 参考、使用/开发指南、风险等级、漏洞判定标准、评估流程与优化门槛 |
| [`docs/analysis/`](docs/analysis/) | 分析方案 | 问题分析报告与实现方案（含 `explorer-track/` 探索轨专题、规则集评审、验收报告） |
| [`docs/updates/`](docs/updates/) | 系统变更记录 | 每次代码/配置/提示词/规则更新的独立 MD 文档（按日期命名） |
| [`apk/`](apk/) | APK 样本 | 本地内容，不随仓库分发 |
| [`manual-verification-report/`](manual-verification-report/) | 人工验证报告 | 本地内容，不随仓库分发 |
| [`.ai-apk-tracer/`](.ai-apk-tracer/) | run 产物与缓存 | 任务隔离目录 `<workspace>/.ai-apk-tracer/runs/<run_id>/`，已 gitignore |

## 后端代码导航

`backend/` 为 FastAPI 后端，核心源码在 `backend/app/`：

| 路径 | 作用 |
|---|---|
| `app/main.py` | 服务入口：启动 FastAPI、挂载路由、静态托管 `frontend/dist` |
| `app/config.py` | Pydantic 配置模型（funnel / AI / 上下文预算 / 清理） |
| `app/api/` | HTTP 层：`routes.py` 路由、`models.py` 请求/响应模型 |
| `app/analysis/` | **核心分析流水线** |
| ├─ `orchestrator.py` | 任务编排：run 生命周期、阶段调度、产物落盘 |
| ├─ `candidate_funnel.py` | 候选漏斗：确定性路由、chain identity 去重、AI 预算与分级排序 |
| ├─ `explorer.py` 等 | 探索轨：`explorer_queue.py`（人工队列）、`explorer_validation.py`（三档校验）、`explorer_normalization.py`（归一化并入主链）、`call_tree.py` |
| ├─ `verify_agent.py` | 核验 agent：verify.enabled 时多轮核验替代单轮 L2，失败回退 |
| ├─ `api_surface.py` / `attack_surface.py` | API 入口表（Binder/动态 Receiver/WebView）与四组件攻击面 |
| ├─ `sink_taxonomy.py` | sink 分类判定（消费 `rules/sink_taxonomy/versions.yaml` 单一事实源） |
| ├─ `context_builder.py` / `context_budget.py` | 方法级切片生成与上下文预算 |
| ├─ `ai.py` + `ai_*` | AI 调用族：runtime、cache、models、recovery、scheduler、trace、transport |
| ├─ `decompiler.py` / `indexer.py` / `index_store.py` | JADX 反编译与 SQLite 代码索引 |
| ├─ `manifest.py` / `manifest_extractor.py` | Manifest 解析与提取 |
| ├─ `rule_runner.py` | 规则子进程执行（JSON 协议、墙钟/CPU/内存限制） |
| ├─ `guard_verifier.py` | Guard 有效性校验（funnel 前置，命中候选不送 AI） |
| ├─ `prompt_registry.py` | 提示词版本注册与解析 |
| └─ `coverage.py` / `coverage_domain.py` | 覆盖缺口传播（规则失败/AI 跳过统一成 gap） |
| `app/findings/` | **发现层**：`aggregate.py`（候选聚合）、`decision.py`（确定性裁决）、`evidence.py`（证据回查）、`severity.py`（定级唯一入口）、`review_state.py`（复核状态）、`report.py`（报告渲染） |
| `app/reporting/` | 报告草稿：AI 报告草稿、PoC 骨架与修复建议（API 按需触发，非流水线阶段） |
| `app/runs/` | run 存储（`storage.py`）与清理（`cleanup.py`） |
| `app/shared/` | 通用：错误、日志、SQLite repository |
| `app/evaluation/` | 评估：golden 集、指标、runner、gate 基线对比门禁、报告质量检查 |
| `tests/` | 后端 pytest 测试 |

## 前端代码导航

`frontend/` 为 React 控制台，源码在 `frontend/src/`：

| 路径 | 作用 |
|---|---|
| `main.tsx` | 应用入口 |
| `app/App.tsx` | 根组件（路由/布局） |
| `features/` | **功能模块** |
| ├─ `runs/` | 任务：列表、创建、详情、阶段时间线（`RunListPage` / `RunDetailPage` / `CreateRunForm` / `StageTimeline`） |
| ├─ `findings/` | 发现：列表（含动态 Receiver 按模块分组）、证据抽屉、复核操作（`FindingsPanel` / `FindingDrawer`） |
| ├─ `assets/` | 资产库与批量扫描（`assets.enabled` 门控） |
| ├─ `reports/` | 漏洞报告面板（`ReportPanel`） |
| └─ `cleanup/` | 产物清理面板（`CleanupPanel`） |
| `lib/` | 通用库：`api.ts`（HTTP 请求）、`types.ts`（类型）、`format.ts`（格式化）、`usePolling.ts`（任务轮询） |
| `ui/` | 通用组件：`Badge` / `Button` / `Drawer` / `StateView` / `AppShell` / `ThemeToggle` |
| `styles.css` | 全局样式：Tailwind 4 + CSS 变量主题（浅色/深色） |

## 文档

完整文档见 [`docs/`](docs/README.md)：项目概述、架构设计、分析流程、规则体系、API 参考、使用/开发指南、风险等级定义、漏洞判定标准。

## 安全与隐私

- 仅监听 `127.0.0.1` 回环地址；任务产物隔离在 `.ai-apk-tracer/runs/<run_id>/`（已 gitignore）
- 默认 `ai.enabled=true`、`ai.allow_external_code=true`：方法级代码切片可能发送到配置的外部 AI 服务，密钥通过 `.env` 配置（已 gitignore）；探索轨（默认关闭）另有独立外发开关 `explorer.allow_external_code`
- 不执行 APK 代码；规则子进程带墙钟/CPU/内存限制

## 测试与校验

```sh
cd backend && python -m pytest        # 后端测试
scripts/check-all.sh                  # 统一校验入口
```

> 注意：`apk/`、`.ai-apk-tracer/`（run 产物与缓存）、`manual-verification-report/` 均为本地内容，不随仓库分发。
