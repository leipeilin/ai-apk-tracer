# AI-APK-Tracer 项目文档

> Android APK 安全自动化分析工具 — 确定性静态分析 + 受严格协议约束的 AI observation

## 文档索引

| 文档 | 内容 | 适用读者 |
|---|---|---|
| [项目概述](./01-项目概述.md) | 产品定位、核心能力、适用场景 | 所有读者 |
| [架构设计](./02-架构设计.md) | 技术栈、模块关系、数据流 | 开发者 / 架构师 |
| [分析流程](./03-分析流程.md) | 扫描流水线、证据模型、置信度体系 | 安全分析人员 / 开发者 |
| [规则体系](./04-规则体系.md) | 29 条内置规则、规则协议、扩展开发 | 规则开发者 |
| [API 参考](./05-API参考.md) | 全部 HTTP 端点、请求/响应格式 | 前端开发 / 集成方 |
| [使用指南](./06-使用指南.md) | 安装、配置、日常使用、故障排除 | 最终用户 |
| [开发指南](./07-开发指南.md) | 开发环境、测试、构建、贡献规范 | 开发者 |
| [Pipeline v2 权威设计](./08-L1-AI分诊与语义复核优化设计.md) | L1 分诊、L2 复核、证据与决策边界、当前实施状态 | 安全分析人员 / 开发者 |
| [风险等级定义](./09-风险等级定义.md) | 6 档等级标准、CVSS/OWASP 映射、人工复核判据 | 安全分析人员 / 开发者 |
| [漏洞判定标准](./10-漏洞判定标准.md) | 漏洞四要素、23 条红线、verdict 映射、红线 23 约束、可达性分级 | 安全分析人员 / 开发者 |

## 快速了解

AI-APK-Tracer 是一个本地单用户的 Android APK 安全分析工具，采用分层架构：

```text
APK 上传 → 结构校验 → JADX 反编译/索引 → 规则预筛选
→ candidate funnel（确定性路由、chain identity、精确去重）
→ 方法级切片 → AI preflight → L1 triage / L2 review
→ 必要时 finalization observation → evidence integrity → decision
→ 保守聚合 → 人工复核/报告
```

核心特点：

- **29 条内置规则**覆盖 Activity、Service、ContentProvider、BroadcastReceiver 四大组件及本地配置、WebView、密码学/证书校验攻击面；动态 Receiver 规则可按证据闭合程度输出 L1 暴露或 L2 静态链
- **共享只读 SQLite 索引**通过版本化 descriptor 传给规则；规则输出候选及可选组件级 diagnostics
- **Candidate funnel**先做确定性 disposition、chain identity 和精确去重，再选择 L1/L2 AI 代表项
- **双轨 AI**只做 strict observation：L1 triage 提出潜在链，L2 review 复核既有静态链；finalization 也只是建议，不写确定性事实或最终状态
- **确定性边界**由规则、evidence validator 与 decision engine 负责 dataflow、Guard、授权、证据完整性和最终 evidence/review 状态
- **方法级上下文切片**支持有界扩片；输入 token 是按 UTF-8 字节估算的近似值，请求预算按一次逻辑 AI 调用计数
- **Prompt/Schema 同步门禁**校验版本、模板/Schema hash 与 strict Pydantic 模型，不一致时拒绝加载
- **FastAPI + React 19 前端**仅监听回环地址；默认启用 AI 且允许将选中候选的代码切片发往配置的外部 provider

## 版本与验证

- 当前版本：v0.1.0
- 测试数量会随实现变化；请以 `scripts/check-all.sh`、相关测试命令的退出码和本次实际收集结果为准
- 设计目标、当前实现与尚未汇总的 API/UI 字段边界以[文档 08](./08-L1-AI分诊与语义复核优化设计.md)为准
