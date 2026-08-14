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
