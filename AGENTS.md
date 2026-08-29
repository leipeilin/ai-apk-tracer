# AI-APK-Tracer 工作区指令

本文件是 ZCode 的工作区指令文件（等价于 `.codebuddy/rule/` 中 `alwaysApply: true` 的规则），每次会话自动加载，对所有提交强制生效。

---

# 提交信息编写规范

本规范定义 AI-APK-Tracer 项目的 Git 提交信息（Commit Message）编写标准。所有提交都应遵循本规范，保证提交历史清晰、可追溯、可复盘。


---

## 1. 整体结构

一条完整的提交信息由三部分组成：

```
<标题行>
<空行>
<正文（问题 / 方案 / 改动文件）>
```

- **标题行**：一行概括本次改动，遵循 Conventional Commits 风格。
- **空行**：标题与正文之间必须空一行。
- **正文**：使用简体中文，按"问题 → 方案 → 改动文件"的结构分段说明。

---

## 2. 标题行规范

### 2.1 格式

```
<type>(<scope>): <简体中文摘要>
```

- `type`、`scope`、`:` 均为半角字符，`:` 后跟一个空格。
- 摘要使用**简体中文**，一句话概括本次改动带来的价值或结果，不加结尾标点。
- 标题行建议控制在 72 个字符以内（中文按显示宽度酌情放宽），但必须一眼看清"改了什么"。
- 摘要应描述**结果/价值**，而非罗列动作。
  - 推荐：`探索轨入口级并行——BoundedJobScheduler 提升全量数据采集吞吐`
  - 不推荐：`修改了 orchestrator.py 和 config.py`

### 2.2 type 取值

| type | 含义 |
|:---|:---|
| `feat` | 新增功能 / 新能力 |
| `fix` | 修复缺陷 |
| `refactor` | 重构（不改变外部行为） |
| `perf` | 性能优化 |
| `test` | 仅新增或调整测试 |
| `docs` | 仅文档改动 |
| `chore` | 构建、脚本、依赖等杂项 |
| `style` | 代码格式（不影响逻辑） |

### 2.3 scope 取值

`scope` 为本次改动的主要影响范围，通常是**模块名**，与仓库目录一致（以下为常用值，非穷举）：

- 后端模块：`analysis`（流水线/编排）、`explorer`（探索轨）、`findings`（证据/裁决/聚合）、`evaluation`（评估与门禁）、`reporting`（报告）、`assets`（资产/批量）、`ai`（AI 调用族）、`config`
- 规则与协议：`rules`（规则包）、`taxonomy`（sink taxonomy）、`prompts`（提示词）、`schemas`（JSON Schema）、`dataflow`、`guard` 等规则/协议专题
- 前端：`frontend`、`ui`
- 工程与仓库级：`repo`、`scripts`、`deps`、`lint`
- 文档与评审记录：`docs`、`readme`、`review`、`acceptance`

跨多个模块时，选择改动的**核心承载模块**作为 scope，其余在正文"改动文件"中说明。

---

## 3. 正文规范

正文必须使用**简体中文**，按以下三段式组织。每段用一个显式小标题引导（`问题：` / `方案：` / `改动文件：`），或使用等价表述（如 `问题背景：`、`问题根因：`、`解决方案：`）。

### 3.1 问题（Why）

说明本次改动要解决的问题、触发背景或根因。要求：

- 讲清楚"为什么要改"，而不仅是"改了什么"。
- 尽量给出**可验证的现象或证据**（如具体报错、指标异常、全量 run 数据、错误码）。
  - 范例：`T1-v3 全量 run 探索输出 error 率 50%（输出协议违规，见 docs/analysis/explorer-track/）`
- 对缺陷类提交，推荐进一步拆出**根因**，解释问题是如何产生的。
- 如有关联文档，附上仓库内路径（如 `docs/analysis/` 下的分析报告）。

### 3.2 方案（What / How）

说明采用的解决方案与关键实现要点。要求：

- 先用一句话概括方案的核心思路（如"将探索轨前移至 funnel 之前，validated 候选与规则候选同路复核"）。
- 用无序列表列出关键改动点 / 设计决策，每条聚焦一个要点。
- 说明重要的**边界处理、兼容性、幂等性**等考量。
  - 范例：`api_surface 门控关闭时攻击面注入降级为空、探索轨仍可运行（向后兼容）`
- 如顺带修复了其它问题（如既有编译错误、测试 panic），单列一段说明，避免混入主线方案。

### 3.3 改动文件（Where）

以 `改动文件：` 小标题引导，用无序列表逐条列出主要改动文件及其改动内容。要求：

- 每条格式为 `路径: 改动说明`，路径相对模块或仓库根目录均可，保持一致即可。
- 只列**主要**文件，一句话说明该文件改了什么、为什么改。
- 新增文件应标注 `（新文件）` 或 `（新增）`。
  - 范例：`docs/analysis/rules-review/2026-08-28-p7-p8-e5-e6-fix.md: 方案+验收文档（新文件）`
- 测试文件应说明新增/调整的用例范围。
  - 范例：`backend/tests/test_explorer.py: 新增并行探索与实时落盘用例`

> 说明：`改动文件` 段落对小型改动（单文件、意图明显）可省略；但对多文件、跨层的改动**必须**保留，以便 Review 和后续复盘。

---

## 4. 通用要求

- **语言**：标题摘要与正文统一使用简体中文；`type`、`scope`、文件路径、代码标识符、错误码保持英文原样。
- **技术术语**：首次出现的概念可用"中文（English）"形式标注，如 `探索轨（explorer track）`。
- **客观描述**：陈述事实与技术决策，不写情绪化、口语化表达。
- **可追溯**：涉及具体数值、阈值、配置项、指标时应写明（如 `entry_concurrency（默认4）`、`read_timeout_seconds调整为240`）。
- **原子提交**：一次提交聚焦一件事。主线改动之外的"顺带修复"应单独成段说明，规模较大时应拆分为独立提交。
- **禁止事项**：
  - 禁止空泛标题，如 `update`、`fix bug`、`修改代码`、`提交一下`。
  - 禁止将变量值或动态内容硬编码进标题（与日志规范一致，标题描述结果而非流水账）。
  - 禁止使用 `--no-verify` 跳过钩子后不说明原因。

---

## 5. 模板

```
<type>(<scope>): <简体中文摘要>

问题：<要解决的问题 / 触发背景>
<可选：根因说明，解释问题如何产生，附证据或案例链接>

方案：<核心思路一句话概括>
- <关键改动点 1>
- <关键改动点 2>
- <边界 / 兼容性 / 幂等性等考量>

改动文件：
- <path>: <改动说明>
- <path>: <改动说明>（新文件）
- <path>: <测试用例范围>
```

---

## 6. 完整示例（摘自范例提交 84c7647）

```
feat(rules): P7/P8 补齐 E5/E6 遗漏——SSL/TrustManager 方法体花括号提取（嵌套块可检）+
裸 AES 隐式 ECB + WEAK_CIPHER_ALGORITHM 弱算法族（33→34）

问题：规则集质量评审（docs/analysis/rules-review/2026-08-28-p7-p8-e5-e6-fix.md）
发现 E5/E6 两条遗漏：TRUST_MANAGER_ALL_ACCEPT 只匹配裸 return 形态，方法体
嵌套花括号块内的 return 不可检；"AES" 不带模式参数时 JCA 默认隐式 ECB；
DES/RC4/MD5 等弱算法族无规则覆盖。

方案：共享库先剥离方法体外层花括号再匹配裸 return，使嵌套块可检
- WEAK_CIPHER_ECB 增加 "AES" 无模式参数的隐式 ECB 形态
- 新增 WEAK_CIPHER_ALGORITHM 整词词表规则（severity=medium，33→34 条）
- severity 保持 RULE_META 单源（TestSeveritySingleSource 强制约束）
- 全量 pytest 1370 passed

改动文件：
- rules/shared/detector.py: 花括号剥离提取器 + 弱算法族词表与判定
- rules/crypto/WEAK_CIPHER_ALGORITHM/: 新规则包（rule.yaml + detect.py）（新增）
- rules/crypto/TRUST_MANAGER_ALL_ACCEPT/rule.yaml: 嵌套块可检的口径说明
- rules/crypto/WEAK_CIPHER_ECB/rule.yaml: 裸 AES 隐式 ECB 形态说明
- rules/shared/index_reader.py: 提取器所需索引字段补充
- backend/tests/test_webview_crypto_rules.py: 新增嵌套块/裸AES/弱算法族用例
- backend/tests/test_rule_index_protocol.py: 索引协议断言同步
- docs/04-规则体系.md: 规则清单与说明同步
- docs/analysis/rules-review/2026-08-28-p7-p8-e5-e6-fix.md: 方案+验收文档（新文件）
```
