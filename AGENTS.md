# AI-APK-Tracer 工作区指令

本文件是 ZCode 的工作区指令文件（等价于 `.codebuddy/rule/` 中 `alwaysApply: true` 的规则），每次会话自动加载，对所有提交强制生效。

---

# 提交信息编写规范

本规范定义 AI-APk-Tracer 项目的 Git 提交信息（Commit Message）编写标准。所有提交都应遵循本规范，保证提交历史清晰、可追溯、可复盘。


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
  - 推荐：`资源池级（pool level）配额分配，解决多pool场景下配额公平性问题`
  - 不推荐：`修改了 task_dispatcher.go 和 config.go`

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

`scope` 为本次改动的主要影响范围，通常是**模块名**，与仓库目录一致：

- 模块：`pool_mgr`、`disk_mgr`、`buss_constraint_mgr`、`package_mgr`、`placement_mgr`、`migration_plan_mgr`、`schedule_controller`、`sql_query_proxy`
- 公共库：`common`、`pkg`、`proto`、`errors`、`config` 等

跨多个模块时，选择改动的**核心承载模块**作为 scope，其余在正文"改动文件"中说明。

---

## 3. 正文规范

正文必须使用**简体中文**，按以下三段式组织。每段用一个显式小标题引导（`问题：` / `方案：` / `改动文件：`），或使用等价表述（如 `问题背景：`、`问题根因：`、`解决方案：`）。

### 3.1 问题（Why）

说明本次改动要解决的问题、触发背景或根因。要求：

- 讲清楚"为什么要改"，而不仅是"改了什么"。
- 尽量给出**可验证的现象或证据**（如具体报错、指标异常、线上案例、可用区名称、错误码）。
  - 范例：`高性能资源池长期无任务下发（南京一区premium pool连续3天scheduled=0）`
- 对缺陷类提交，推荐进一步拆出**根因**，解释问题是如何产生的。
- 如有关联文档 / 单据，附上链接（如 `iwiki.woa.com/p/xxxx`）。

### 3.2 方案（What / How）

说明采用的解决方案与关键实现要点。要求：

- 先用一句话概括方案的核心思路（如"将两层配额改为三层配额结构"）。
- 用无序列表列出关键改动点 / 设计决策，每条聚焦一个要点。
- 说明重要的**边界处理、兼容性、幂等性**等考量。
  - 范例：`单pool场景自动退化为改造前行为（向后兼容）`
- 如顺带修复了其它问题（如既有编译错误、测试 panic），单列一段说明，避免混入主线方案。

### 3.3 改动文件（Where）

以 `改动文件：` 小标题引导，用无序列表逐条列出主要改动文件及其改动内容。要求：

- 每条格式为 `路径: 改动说明`，路径相对模块或仓库根目录均可，保持一致即可。
- 只列**主要**文件，一句话说明该文件改了什么、为什么改。
- 新增文件应标注 `（新文件）` 或 `（新增）`。
  - 范例：`doc/pool_quota_allocation_design.md: 设计文档（新文件）`
- 测试文件应说明新增/调整的用例范围。
  - 范例：`app/service/task_dispatcher_test.go: 新增28+测试用例`

> 说明：`改动文件` 段落对小型改动（单文件、意图明显）可省略；但对多文件、跨层的改动**必须**保留，以便 Review 和后续复盘。

---

## 4. 通用要求

- **语言**：标题摘要与正文统一使用简体中文；`type`、`scope`、文件路径、代码标识符、错误码保持英文原样。
- **技术术语**：首次出现的概念可用"中文（English）"形式标注，如 `资源池级（pool level）`。
- **客观描述**：陈述事实与技术决策，不写情绪化、口语化表达。
- **可追溯**：涉及具体数值、阈值、配置项、指标时应写明（如 `min_pool_quota（默认10）`、`urgent_quota_ratio调整为0.7`）。
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

## 6. 完整示例（摘自范例提交）

```
feat(migration_plan_mgr): 资源池级（pool level）配额分配，解决多pool场景下配额公平性问题

问题：SSD资源池的plan数量碾压高性能pool，按plan遍历先到先得导致
高性能资源池长期无任务下发（南京一区premium pool连续3天scheduled=0）

方案：将DispatchTasksForZone从zone→plan两层改为zone→pool→plan三层配额结构
- 每个有待下发任务的pool至少获得min_pool_quota（默认10）个保底配额
- 剩余配额按pendingTasks加权分配（零额外DB查询）
- 紧急通道下沉到pool级别，不跨pool挤占
- 二轮surplus再分配：消耗完配额的hungry pool获得其他pool让出的剩余配额
- 单pool场景自动退化为改造前行为（向后兼容）

改动文件：
- constant/constant.go: 新增MIN_POOL_QUOTA=10
- config/config.go: 新增MinPoolQuota配置+校验+getter
- config/migration_plan_mgr.toml: urgent_quota_ratio调整为0.7，新增min_pool_quota
- app/service/task_dispatcher.go: 核心重构（allocateQuotaByPool/dispatchTasksForPool/redistributeSurplus）
- pkg/metrics/metrics.go: 新增5个pool级监控指标
- app/service/task_dispatcher_test.go: 新增28+测试用例（配额分配11+再分配6+端到端4+）
- test/integration-checklist.md: v1.9.0新增TC-092~TC-095
- doc/pool_quota_allocation_design.md: 设计文档（新文件）
```
