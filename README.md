# AI-APK-Tracer

本地单用户的 Android APK 安全自动化静态分析工具：确定性规则与索引发现攻击面，方法级切片向 AI 提供受控上下文，AI 只返回 strict observation，证据回查与最终判定由确定性代码和人工复核负责。

核心目标：**提高真实漏洞检出准确率，降低误报率与人工复核成本。**

## 架构

```
APK → basic_check → decompiling + index → rule_prescan → candidate_funnel
    → code_slicing → ai_analysis（L1 triage / L2 review）→ evidence validation + decision
    → aggregation → manual review
```

- **规则层**（确定性）：29 条内置规则覆盖 Activity / Service / ContentProvider / BroadcastReceiver 四大组件及 WebView、密码学、Manifest 攻击面
- **Candidate funnel**：确定性路由、chain identity 精确去重、AI 预算选择
- **切片层**：受预算约束的方法级上下文，结构化扩片请求
- **AI 层**：L1 triage / L2 review / finalization 只输出 strict observation（Pydantic strict + JSON Schema + Prompt 版本/SHA 门禁）
- **证据/决策层**：事实回查、dataflow、Guard、授权与最终 decision 由确定性代码独立裁决

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 + FastAPI + uvicorn + Pydantic strict（`backend/app/`） |
| 前端 | React 19 + Vite 7 + Tailwind 4（`frontend/`） |
| 外部工具 | JADX 反编译、Android SDK platform-tools、OpenAI-compatible AI provider |
| 规范 | 19 个 JSON Schema、版本化 Prompt registry（`prompts/`）、SQLite 代码索引 |

## 目录导航

| 目录 | 作用 | 说明 |
|---|---|---|
| [`backend/`](backend/) | 后端实现 | FastAPI 应用（`app/`）、pytest 测试（`tests/`）、依赖锁定（`requirements.txt` / `pyproject.toml`） |
| [`frontend/`](frontend/) | 前端实现 | React 19 + Vite 7 + Tailwind 4 控制台 |
| [`rules/`](rules/) | 规则包 | 29 条内置确定性规则：Activity / Service / ContentProvider / BroadcastReceiver + WebView / 密码学 / Manifest（`shared/` 为共享检测逻辑） |
| [`prompts/`](prompts/) | AI 提示词 | 版本化 prompt registry：preflight / l1-triage / l2-review / repair / finalization |
| [`schemas/`](schemas/) | JSON Schema | AI 输入输出与产物结构规范（19 个） |
| [`config/`](config/) | 配置 | 默认配置 `default.yaml`（funnel / AI / 上下文预算 / 清理） |
| [`scripts/`](scripts/) | 脚本 | 开发与统一校验入口（如 `check-all.sh`、`dev-backend.sh`） |
| [`tools/`](tools/) | 工具 | POC 与验证工具（commonbase-activity-poc、sportxms-poc） |
| [`evaluation/`](evaluation/) | 评估 | golden 集与指标（`golden/`） |
| [`tests/`](tests/) | 测试 | 根级测试 |
| [`doc/`](doc/) | 系统记录文档 | 过程性工作记录：AI 基线、JADX 诊断、人工复核进度、漏洞分析等 |
| [`docs/`](docs/) | 系统说明文档 | 编号体系：项目概述、架构、分析流程、规则体系、API 参考、使用/开发指南、风险等级、漏洞判定标准 |
| [`docs/analysis/`](docs/analysis/) | 分析方案 | 问题分析报告与实现方案（如 receiver 282 候选、informational 治理） |
| [`docs/updates/`](docs/updates/) | 系统变更记录 | 每次代码/配置/提示词/规则更新的独立 MD 文档（按日期命名） |
| [`apk/`](apk/) | APK 样本 | 本地内容，不随仓库分发 |
| [`manual-verification-report/`](manual-verification-report/) | 人工验证报告 | 本地内容，不随仓库分发 |
| [`.ai-apk-tracer/`](.ai-apk-tracer/) | run 产物与缓存 | 任务隔离目录 `<workspace>/.ai-apk-tracer/runs/<run_id>/`，已 gitignore |

## 文档

完整文档见 [`docs/`](docs/README.md)：项目概述、架构设计、分析流程、规则体系、API 参考、使用/开发指南、风险等级定义、漏洞判定标准。

## 安全与隐私

- 仅监听 `127.0.0.1` 回环地址；任务产物隔离在 `.ai-apk-tracer/runs/<run_id>/`（已 gitignore）
- 默认 `ai.enabled=true`、`ai.allow_external_code=true`：方法级代码切片可能发送到配置的外部 AI 服务，密钥通过 `.env` 配置（已 gitignore）
- 不执行 APK 代码；规则子进程带墙钟/CPU/内存限制

## 测试与校验

```sh
cd backend && python -m pytest        # 后端测试
scripts/check-all.sh                  # 统一校验入口
```

> 注意：`apk/`、`.ai-apk-tracer/`（run 产物与缓存）、`manual-verification-report/` 均为本地内容，不随仓库分发。
