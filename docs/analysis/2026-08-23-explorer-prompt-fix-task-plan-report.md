# 实施任务计划报告：EXPLORER-PROMPT-FIX（Explorer Prompt 严格输出契约修复）

> **任务编号**：EXPLORER-PROMPT-FIX
> **日期**：2026-08-23
> **依据实施方案**：`docs/analysis/2026-08-22-explorer-prompt-fix-implementation-plan.md`（2026-08-23 按评审修订版）
> **评审记录**：`docs/analysis/2026-08-23-explorer-prompt-fix-review.md`（R-1~R-6 全部处置完成）
> **验收方案**：`docs/analysis/2026-08-23-explorer-prompt-fix-acceptance-plan.md`（A-1~A-9）
> **工作流阶段**：阶段 1（实施方案）✅ → 阶段 2（验收方案）✅ → 阶段 3（验收方案评审）✅（R-7~R-12 全部采纳闭合，见评审文档 §3）→ 阶段 4（讨论闭合）✅ → 阶段 5（实施与验收）⏳ → 阶段 6（提交）⏳
> **总体状态**：未开始实施（代码侧零改动，锚点见 §2）

---

## 1. 报告目的与任务摘要

- **目标**：修复 `explorer/1.0.0` 探索 Agent 在真实 AI 调用中持续返回 `schema_invalid` 的问题（根因：现行 `system.md` 缺"只输出一个 JSON 对象"约束与显式输出契约，模型回退旧字段 `component_id`/`explorer_state`/`hypotheses`/顶层 `evidence_refs`），使探索轨能产出有效 `ExplorerObservation`，为 M2 双 APK 验收铺路。
- **改动面**：仅 4 个代码/配置文件（`system.md` 重写 + `registry.yaml` 哈希同步 + 探针脚本新增 + 协议测试断言扩展），不改 Pydantic 模型/驱动/预算/配置，版本维持 `explorer/1.0.0`。
- **评审闭环**：评审 6 项意见全部处置——R-1 探针固化入库、R-2 验收样本量提至 ≥3 次或异构入口、R-3 契约补四组数组上限、R-4 回退连带测试断言、R-5 版本决断（部分采纳：采纳"二选一并写理由"，决断维持 1.0.0——`ai.py::explore_entry` 硬编码 `"1.0.0"`，升版扩大改动面，且 1.0.0 无已验收/已发布 run，哈希门禁足以区分新旧模板）、R-6 证据摘录已内联实施方案 §2。
- **开放决策项**：无。唯一人工决策点为冒烟中的"零候选观察项"（验收方案 N-4），实施时按记录触发，不阻塞流程。

## 2. 现状锚点（实施前基线，2026-08-23 实测）

| 锚点 | 现状 | 含义 |
|---|---|---|
| `prompts/explorer/1.0.0/system.md` | 旧版：硬约束 7 条，无"只输出一个 JSON 对象"、无显式输出契约节 | 修复未落地 |
| `prompts/registry.yaml` | `explorer@1.0.0.template_sha256.system = 840cb966e226f69f856bcc4f4b8d67b3f0c44feb156459d3b167bd5807c1524a` | 旧模板哈希，回退对照值 |
| `scripts/` | 无 `probe_explorer_prompt.py` | R-1 探针未入库 |
| `backend/tests/test_explorer_protocol.py::test_prompt_declares_required_and_enums` | 仅既有 token 断言（:190-203），无严格契约断言 | 防回归未扩展 |
| 基线测试 | `test_explorer_protocol.py + test_prompt_registry.py + test_config.py` 49 passed | 改动前基线绿，改动后不得转红 |
| 工作区状态 | `git status`：`config/default.yaml` 已修改、若干未跟踪文档/测试文件（含 m2 系列） | **非本任务改动，阶段 6 提交时严禁夹带** |
| 环境 | `backend/.venv/bin/python`（3.12）可用；`.env` 含 `AI_APK_TRACER_OPENAI_API_KEY` | 满足真实 AI 冒烟前置 |

## 3. 任务分解（S-0 ~ S-7）

> 执行顺序：S-0 → S-1 → S-2 → S-3 → S-4 → S-5 → S-6 → S-7，严格串行（依赖见 §4）。每步完成后在 §8 跟踪表回填状态。

### S-0 前置检查与环境确认
- **动作**：确认 `backend/.venv/bin/python`（3.12）可用、`.env` 含 key；跑基线 `backend/.venv/bin/python -m pytest backend/tests/test_explorer_protocol.py backend/tests/test_prompt_registry.py backend/tests/test_config.py -q`（预期 49 passed）；`git status` 记录既有无关改动清单（提交时排除）。
- **产出**：基线确认记录（写入验收方案 §6 备注）。
- **出口条件**：基线全绿；依赖确认——本任务无前置任务依赖。

### S-1 新增真实 AI 冒烟探针 `scripts/probe_explorer_prompt.py`（对应实施方案 §3.4 步骤 1；闭环 R-1/R-7/R-10/R-11）
- **动作**：实现探针，实现约束（评审 R-7/R-10/R-11 修订）：
  - 顶部 `sys.path.insert(0, backend)`（对齐 sync-ai-protocol.py 先例）；
  - **密钥加载**：`from dotenv import load_dotenv; load_dotenv()` 后经 `os.environ` 校验 `AI_APK_TRACER_OPENAI_API_KEY`（`get_settings()` 的 dotenv 来源不注入 os.environ，而 `ai.py` 经 os.environ 读 key——评审 R-7）；
  - 分析器构造：`AIRuntime(get_settings().ai).create_analyzer(cache_dir=..., max_output_tokens=..., budget_policy={})`（评审 R-10——传 AI 子配置非全量 Settings）；
  - 调用正式 registry 路径的 `await analyzer.explore_entry(ExplorerInput)`（`asyncio.run` 驱动；禁止绕过 registry 的裸 HTTP）；
  - 入口数据：默认取 `--run-dir`（默认 `20260822T124055Z_2a80fc5a8735_34aedd85` 的既有 run）的 `api-surface/api_entry_table.json` 首个含 method_id 的入口，`entry_json=json.dumps(entry)`（对齐 explorer.py:154-162 驱动先例）；
  - 默认同一入口连续 ≥3 次，`--entries` 支持 2-3 个异构入口；每次输出 `status`/`classification`/解析结果/`chain_proposals` 计数；缺 key 显式报错退出（≠0）；任一失败整体退出码 ≠0。
- **产出**：`scripts/probe_explorer_prompt.py`（入库）。
- **验证**：无 key 空跑验证显式报错（验收 N-1）；本步不要求跑通真实 AI（prompt 尚未替换）。
- **映射验收项**：A-7。

### S-2 替换 `prompts/explorer/1.0.0/system.md`（§3.4 步骤 2）
- **动作**：以实施方案 §3.2 设计稿全文替换现行 `system.md`（逐字落稿，不二次改写）。
- **产出**：新 `system.md`。
- **验证**：与 §3.2 逐字 diff；人工核对既有断言 token 全保留。
- **映射验收项**：A-1。

### S-3 同步 registry 哈希（§3.4 步骤 3）
- **动作**：`backend/.venv/bin/python scripts/sync-ai-protocol.py --write`，随后 `--check` 确认退出码 0。
- **产出**：`prompts/registry.yaml` 中 `explorer@1.0.0.template_sha256.system` 更新（`user` 哈希不变，其余条目不动）。
- **验证**：`git diff prompts/registry.yaml` 仅 system 哈希一行变化；`--check` 通过（验收 N-2 的反向佐证）。
- **映射验收项**：A-3。

### S-4 扩展协议测试断言（§3.4 步骤 4 前置、§3.3；闭环 R-3/R-4）
- **动作**：在 `test_prompt_declares_required_and_enums` 追加断言：含"只输出一个 JSON 对象"；含"禁止使用旧字段名"；顶层 `loop` 显式声明（`component_summary` 已有）；`read_requests`/`chain_proposals` 字段名；数组上限 `1-32`（hops）、`最多 32 个`（arg_positions）、`最多 16 个`（call_tree_refs）、`最多 64 个`（evidence_refs）。
- **产出**：扩展后的 `backend/tests/test_explorer_protocol.py`。
- **验证**：`cd backend && .venv/bin/python -m pytest tests/test_explorer_protocol.py -q` 通过（既有 + 新增断言全绿）。
- **映射验收项**：A-2。

### S-5 定向测试与全量回归（§3.4 步骤 4、6）
- **动作**：`backend/.venv/bin/python -m pytest backend/tests/test_explorer_protocol.py backend/tests/test_prompt_registry.py backend/tests/test_config.py -q` → 全量 `cd backend && .venv/bin/python -m pytest -q` → `sh scripts/check-backend.sh`。
- **产出**：回归结果记录。
- **出口条件**：全部 0 failed；任一失败先修复再复验，不得带红进入 S-6。
- **映射验收项**：A-4、A-5。

### S-6 真实 AI 冒烟（§3.4 步骤 5；闭环 R-2）
- **动作**：`backend/.venv/bin/python scripts/probe_explorer_prompt.py` 默认模式（同一入口 ≥3 次）；如需异构覆盖，追加 `--entries` 指定 2-3 个异构入口逐个完整通过。记录每次 `status`/`classification`/候选数。
- **出口条件**：3/3（或异构逐个）`status=completed` 且 `ExplorerObservation` 解析通过，无 `schema_invalid`；任一次失败即本步失败（不得以多数通过放行）。候选数为观察项：全零时按验收方案 N-4 触发人工决策（调低约束语气但保留字段名约束，回到 S-2 重走）。
- **映射验收项**：A-6、N-3、N-4。

### S-7 验收记录回写、变更范围审查与提交（阶段 6）
- **动作**：
  1. 逐项回填验收方案 §6 验收记录（含 A-9 回退演练——**演练方式按评审 R-9 修订**：`git stash`（stash 探针外全部改动）→ `git checkout -- prompts/...system.md prompts/registry.yaml`（旧版演练态）→ 复跑定向测试确认旧自洽 → `git stash pop` 恢复 → **复跑 A-1 逐字 diff + S-3 `--write` + 定向测试**（防 stash 恢复引入文本漂移）；演练记录写入 A-9 行"实测说明"列——评审 R-12）；
  2. `git status` 确认暂存区**仅含**本任务文件：`prompts/explorer/1.0.0/system.md`、`prompts/registry.yaml`、`scripts/probe_explorer_prompt.py`、`backend/tests/test_explorer_protocol.py` + 本任务文档（实施方案、评审、验收方案、本报告）；**排除** `config/default.yaml`、`backend/tests/test_no_rules_import.py`、m2 系列文档等无关改动；
  3. 提交信息遵循仓库规范 `type(scope): 中文描述`，建议：`fix(prompts): explorer prompt 严格输出契约修复（EXPLORER-PROMPT-FIX）`，正文列交付物要点与冒烟结果（冒烟结果注明实际 base_url/model——评审 R-8：依赖未提交本地 token-plan 配置）；
  4. 提交后汇报：任务、文档产物、验收结果摘要、commit hash。
- **映射验收项**：A-8、A-9。

## 4. 执行顺序与依赖

```
S-0 前置检查
 └─ S-1 探针脚本（不依赖 prompt 变更，可先行）
     └─ S-2 替换 system.md（§3.2 全文）
         ├─ S-3 sync-ai-protocol --write/--check（依赖 S-2 文本定稿）
         └─ S-4 测试断言扩展（断言对象为 S-2 新 prompt）
             └─ S-5 定向测试 + 全量回归（依赖 S-3 哈希、S-4 断言）
                 └─ S-6 真实 AI 冒烟（依赖 S-2/S-3 生效）
                     └─ S-7 验收回写 + 提交（依赖 A-1~A-9 全绿）
```

- 关键依赖说明：S-3 必须在 S-2 文本定稿后执行（避免哈希二次漂移）；S-6 必须在 S-5 全绿后执行（真实 AI 调用成本高，不替单测兜底）。
- 估算（供排期参考）：S-0/S-3 分钟级；S-1/S-4 小时级以内；S-2 分钟级（落稿）；S-5 分钟级；S-6 取决于真实 AI 时延（3 次连续调用）；S-7 分钟级。整体约半天内可闭环。

## 5. 验收门映射（任务 ↔ 验收项）

| 任务 | 交付物 | 验收门 |
|---|---|---|
| S-1 | `scripts/probe_explorer_prompt.py` | A-7（入库自洽、正式 registry、缺 key 显式报错） |
| S-2 | 新 `system.md` | A-1（与 §3.2 逐字一致） |
| S-3 | `registry.yaml` 哈希 | A-3（哈希变更 + `--check` 0） |
| S-4 | 扩展断言 | A-2（新旧断言全绿） |
| S-5 | 回归结果 | A-4、A-5 |
| S-6 | 冒烟记录 | A-6（3/3 或异构逐个全通过）、N-3、N-4 |
| S-7 | 提交 | A-8（范围受控）、A-9（回退演练） |

## 6. 前置检查清单（开工前逐项确认）

- [ ] `backend/.venv/bin/python --version` = 3.12.x
- [ ] `.env` 存在且含 `AI_APK_TRACER_OPENAI_API_KEY`（真实 AI 冒烟用）
- [ ] 基线定向测试 49 passed（S-0 实测记录）
- [ ] `git status` 记录在案：`config/default.yaml` 等无关改动**不**随本任务提交
- [ ] 已确认无前置任务依赖（实施方案 §5：前置任务无）
- [ ] 验收方案已通过阶段 3 评审并闭合（当前状态：待评审）

## 7. 风险与应对（摘自实施方案 §4，含评审修订）

| 风险 | 应对 | 回退 |
|---|---|---|
| 严格 prompt 下模型仍偶发 `schema_invalid` | 保留既有 strict-parse→单次 repair 状态机（`ai.py::_invoke_prompt`）；冒烟 3/3 门槛暴露偶发失败 | 验收方案 §5 回退流程（含断言连带回退，R-4） |
| 新 prompt 过于保守致零候选 | 冒烟记录候选数（观察项）；全零则调低约束语气、保留字段名约束后重跑 | 同上 |
| 探针依赖真实 AI key/网络 | 仅本地验收执行，不进 CI；缺 key 显式报错；无法执行时记录原因，任务不得视为验收通过 | 补跑后回填 |
| registry 哈希遗漏同步 | S-3 强制 `--write` + `--check` 双动作 | 重新 sync |
| 新增断言过强误报 | 断言限定字段名/枚举/禁止旧字段/数组上限等稳定契约（均已在 §3.2 设计稿中逐字存在） | 调整断言粒度 |
| 提交夹带无关改动 | S-7 第 2 步显式核对暂存区清单 | 重置暂存区后重新提交 |

## 8. 进度跟踪表（实施中回填）

| 任务 | 状态 | 完成时间 | 执行人/备注 |
|---|---|---|---|
| S-0 前置检查 | ✅ | 2026-08-23 | Python 3.12.13；.env 含 key；基线定向 49 passed；git status 无关改动清单在案 |
| S-1 探针脚本 | ✅ | 2026-08-23 | 入口经 get_entry_points 同源加载（629 条含 method_id）；单 loop 全调用（勘误）；N-1 报错路径确认 |
| S-2 system.md 替换 | ✅ | 2026-08-23 | §3.2 全文落稿 + 两处实施勘误（花括号渲染阻断 / reason 长度约束——见验收 §6） |
| S-3 哈希同步 | ✅ | 2026-08-23 | system 哈希 840cb966… → 996be097…；--check 0 |
| S-4 测试断言扩展 | ✅ | 2026-08-23 | 六组新断言追加，定向测试全绿 |
| S-5 定向 + 全量回归 | ✅ | 2026-08-23 | 全量 1148 passed / 0 failed；check-backend 通过 |
| S-6 真实 AI 冒烟 | ✅ | 2026-08-23 | 同入口 3/3 + 异构 3/3 全通过；N-4 归因实证（带上下文产出 2 条链——prompt 不保守） |
| S-7 验收回写 + 提交 | ✅ | 2026-08-23 | A-1~A-9 全通过（A-9 stash 演练含 R-4 连带实证）；单一提交、范围受控 |

## 9. 完成定义（DoD）

1. A-1~A-9 全部"通过"并回填验收记录（A-6 含真实 AI 3/3 或异构逐个通过的实测数据）；
2. `git log` 存在单一提交，暂存区仅含 §3.1 清单文件与本任务文档，提交信息符合 `type(scope): 中文描述` 规范；
3. 汇报包含：任务编号、文档产物清单、验收结果摘要、commit hash；
4. 探索轨具备产出有效 `ExplorerObservation` 的能力，M2 双 APK 完整验收解除本阻塞项（完整验收为后续任务，不在本任务范围）。

## 10. 后续流程提示（方案驱动工作流）

- 本报告与验收方案属阶段 2 产物，进入阶段 5（编码）前须先经阶段 3 独立评审（只读评审子 agent，deepseek-v4-flash）并按阶段 4 闭合；评审通过前不动任何代码文件。
- 实施方案已完成一轮评审且 R-1~R-6 处置闭合（评审文档 §5 确认无需二轮），本轮评审对象为验收方案与本报告。
