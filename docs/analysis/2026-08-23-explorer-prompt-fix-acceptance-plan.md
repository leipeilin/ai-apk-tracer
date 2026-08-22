# 任务验收方案：EXPLORER-PROMPT-FIX

> **任务编号**：EXPLORER-PROMPT-FIX
> **日期**：2026-08-23
> **依据实施方案**：`docs/analysis/2026-08-22-explorer-prompt-fix-implementation-plan.md`（2026-08-23 按评审 R-1~R-6 修订版）
> **评审依据**：`docs/analysis/2026-08-23-explorer-prompt-fix-review.md`（R-1~R-6 处置完成；§5 闭合路径确认修订后无需二轮评审）
> **配套任务计划**：`docs/analysis/2026-08-23-explorer-prompt-fix-task-plan-report.md`（实施任务分解 S-0~S-7 与验收门映射）
> **状态**：起草（待评审）
> **验收方式**：pytest 单测 + registry 哈希门禁 + 真实 AI 冒烟探针（≥3 次/异构入口）+ 全量回归 + 变更范围审查

---

## 1. 验收范围

- 本方案覆盖 EXPLORER-PROMPT-FIX 的全部交付物，验收通过即视为任务完成、可进入提交（阶段 6）。
- 交付物与实施方案 §3.1 一致，共 4 个代码/配置文件 + 文档：
  1. `prompts/explorer/1.0.0/system.md`（重写为严格输出契约，实施方案 §3.2 全文）
  2. `prompts/registry.yaml`（`explorer@1.0.0` 的 `template_sha256.system` 由脚本更新）
  3. `scripts/probe_explorer_prompt.py`（新增，真实 AI 冒烟探针）
  4. `backend/tests/test_explorer_protocol.py`（新增严格输出契约断言）
- **非范围**（验收中需反向确认未触碰）：`ExplorerInput`/`ExplorerObservation`/`ChainProposal`/`Hop` 等 Pydantic 模型与 JSON Schema、`explorer.py` 驱动逻辑、预算与三档校验、`config/default.yaml` 的 `explorer.*` 配置；版本维持 `explorer/1.0.0`（R-5 决断：不升 1.0.1）。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 新 `system.md` 与实施方案 §3.2 设计稿一致 | `diff` 对比设计稿全文；人工核对硬约束 9 条、输出契约（顶层必填 `component_summary`/`loop`）、读码操作四枚举、判定标准四节齐备 | 内容与设计稿逐字一致（允许行尾空白差异）；含"只输出一个 JSON 对象"、"禁止使用旧字段名"、数组上限 `1-32`/`最多 32 个`/`最多 16 个`/`最多 64 个` |
| A-2 | 既有防回归断言全保留、新断言生效 | `cd backend && .venv/bin/python -m pytest tests/test_explorer_protocol.py -q` | `test_prompt_declares_required_and_enums` 通过：既有 token（`reason`/`call_site_line`/`entry_json`/`component_summary`、七枚举、"不得下/不得臆造/禁止附加字段"、"done=true 必须伴随至少一条 chain_proposal"）不转红；新增断言（"只输出一个 JSON 对象"、"禁止使用旧字段名"、顶层 `loop` 显式声明、`read_requests`/`chain_proposals` 字段名、四组数组上限）全部命中 |
| A-3 | registry 哈希门禁同步 | `backend/.venv/bin/python scripts/sync-ai-protocol.py --write` 后 `backend/.venv/bin/python scripts/sync-ai-protocol.py --check` | `prompts/registry.yaml` 中 `explorer@1.0.0.template_sha256.system` 由旧值 `840cb966e226…` 变更为新哈希（`git diff` 可见）；`user` 哈希不变；`--check` 退出码 0 |
| A-4 | 目标测试集通过 | 仓库根执行 `backend/.venv/bin/python -m pytest backend/tests/test_explorer_protocol.py backend/tests/test_prompt_registry.py backend/tests/test_config.py -q` | 全部通过（基线为 49 passed，允许因新增断言增加用例数），0 failed |
| A-5 | 全量回归通过 | `cd backend && .venv/bin/python -m pytest -q`；`sh scripts/check-backend.sh` | pytest 全量 0 failed；compileall、规则契约（30 条）检查通过 |
| A-6 | 真实 AI 冒烟（R-2 统计强度） | `backend/.venv/bin/python scripts/probe_explorer_prompt.py`（默认同一入口连续 ≥3 次）；如需异构：`--entries` 指定 2-3 个异构入口（activity + provider/receiver） | 每次调用 `status=completed` 且 `ExplorerObservation` 解析通过（`classification` 非 `schema_invalid`），3/3（或异构入口逐个）全部通过方为合格；任一次失败即验收失败，不得以"多数通过"放行；同时记录每次 `chain_proposals` 数量（观察项，见 N-4） |
| A-7 | 探针脚本入库自洽（R-1 可复现；R-7/R-10/R-11 修订） | 审查 `scripts/probe_explorer_prompt.py`：走正式 registry 的 `OpenAICompatibleAnalyzer.explore_entry()`（非绕过 registry 的裸 HTTP）；**自行 `load_dotenv()` 并经 `os.environ` 校验 key**（get_settings 不注入 os.environ）；`AIRuntime(get_settings().ai).create_analyzer(...)` 构造；sys.path 注入 backend；`asyncio.run` 驱动；入口数据取自 `--run-dir` 的 api-surface/api_entry_table.json 真实入口；缺失 AI key 时显式报错 | 脚本在仓库内可执行；不带 key 的环境运行退出码 ≠0 且输出可读的缺失提示（N-1） |
| A-8 | 变更范围受控（默认行为不变） | `git status --short` / `git diff --stat` 审查 | 变更仅限 §3.1 清单 4 文件 + 文档；`explorer.enabled` 仍为 `false`；`ai.py::explore_entry` 仍硬编码 `"1.0.0"`（版本未升）；`ai_models.py`、`explorer.py`、`config/default.yaml` 无 diff |
| A-9 | 回退演练可执行（R-4 连带） | 按 §5 步骤执行一次回退→恢复演练（可在工作分支上） | 回退后目标测试集全绿（旧 prompt + 旧断言自洽）；恢复后 A-2~A-4 复绿；演练记录写入本文档 §6 备注 |

> 验收项均可判定：A-1 逐字 diff、A-2~A-5 以退出码与计数为准、A-6 以 3/3 全通过为门槛、A-7/A-8 以文件存在性与 `git diff` 为准、A-9 以复跑结果为准。

## 3. 回归标准

- [ ] 既有功能不受影响：`cd backend && .venv/bin/python -m pytest -q` 全量通过；`scripts/check-backend.sh` 通过（compileall + pytest + 30 条规则契约）。
- [ ] 默认配置下行为不变：`explorer.enabled` 保持 `false`，探索轨默认仍关闭；本修复只改 prompt 表达，不开启任何新行为。
- [ ] 新增断言通过既有协议测试；`explorer@1.0.0` 经 `sync-ai-protocol.py --write` 后通过 `--check` 哈希门禁；`test_prompt_registry.py`/`test_config.py` 对 `explorer/1.0.0` 的注册对齐护栏不转红。
- [ ] `prompts/explorer/1.0.0/user.md` 与其余 prompt 条目（如 `explorer-deep-dive`）哈希不变。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 缺失 AI key | 清空/不提供 `AI_APK_TRACER_OPENAI_API_KEY` 运行探针 | 探针退出码 ≠0，输出明确的"缺少 API key"提示；不发起网络调用、不静默跳过 |
| N-2 | 哈希门禁失同步 | 修改 `system.md` 后不执行 `--write` 直接 `--check` | `--check` 退出码 ≠0（门禁有效）；执行 `--write` 后恢复 0 |
| N-3 | 严格 prompt 下模型偶发 `schema_invalid` | 真实调用首次校验失败 | 既有状态机兜底一次 repair（`ai.py::_invoke_prompt` strict-parse→单次 repair）；repair 后仍失败则如实记录 `classification`，冒烟按 3/3 门槛判定失败（不得静默吞掉） |
| N-4 | 新 prompt 过于保守导致零候选 | 冒烟 3 次 `chain_proposals` 计数均为 0 | 冒烟记录候选数；全零触发人工决策点（按实施方案 §4 风险表：调低约束语气但保留字段名约束后重跑冒烟），不在本验收中自动放行或自动失败 |
| N-5 | 预算/驱动边界 | 预算耗尽、畸形入口等 | 不在本任务变更面内（不改驱动/预算/三档校验），由既有用例回归覆盖（A-4/A-5）；探针不构造畸形输入 |
| N-6 | 探针依赖真实 AI/网络 | CI 或无网环境 | 真实 AI 冒烟仅本地验收执行（A-6），不进 CI 强制项；无法执行时在 §6 记录"未执行真实 AI 冒烟"及原因，任务不得视为验收通过（本地补跑后方可提交） |

## 5. 回退方案

验收不过或上线后发现探索轨退化时，按以下步骤回退（R-4：回退必须连带测试断言）：

1. `git checkout -- prompts/explorer/1.0.0/system.md`（恢复旧版 prompt 全文）；
2. `backend/.venv/bin/python scripts/sync-ai-protocol.py --write`，确认 `template_sha256.system` 回到 `840cb966e226f69f856bcc4f4b8d67b3f0c44feb156459d3b167bd5807c1524a`；
3. **同步回退** `backend/tests/test_explorer_protocol.py` 中本次新增的严格契约断言（保留原有断言不动）；
4. `scripts/probe_explorer_prompt.py` 可保留（探针不依赖 prompt 版本，留作后续复测工具），如需完全回退一并删除；
5. 复跑 A-4/A-5 确认全绿。

- 回退粒度：单提交回退（任务按阶段 6 以单一提交交付，`git revert <hash>` 即可整体回退）。
- 后续升级路径：若回退因"偶发 schema_invalid"，按实施方案 §4 走既有 repair 机制观察，必要时另立任务升级 `explorer/1.0.1`（需同步改 `ai.py::explore_entry` 硬编码）。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-23。**结果：A-1~A-9 全部通过**。
>
> **实施勘误（两处，均已同步实施方案 §3.2 与 system.md）**：
> ① S-6 冒烟首轮发现 `AI_PROMPT_REGISTRY_INVALID`——设计稿 evidence_refs 行的花括号字面量被 registry `format_map` 渲染解析（system 模板禁止一切花括号），改为无花括号等价描述；
> ② `loop.reason` 为 ShortText（≤256）——模型偶发超长（string_too_long），契约行补"不超过 200 字符"约束。
> 另有探针两处实现勘误：入口经 `CallTreeService.get_entry_points()` 同源加载（磁盘 api_entry_table.json 的 method_id=null 由该层增强）；单事件循环跑全部调用（httpx AsyncClient 跨 asyncio.run 复用会 Event loop is closed）。
>
> **A-6 冒烟环境**（评审 R-8）：base_url=token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1、model=deepseek-v4-flash-0731——**依赖未提交的本地 config/default.yaml 配置**。

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | system.md 与实施方案 §3.2 逐字一致（含两处勘误同步修订——花括号行与 reason 长度约束）；"只输出一个 JSON 对象"/"禁止使用旧字段名"/"1-32 个"/"最多 32·16·64 个"全部在文 | — |
| A-2 | 通过 | `test_prompt_declares_required_and_enums` 通过：既有 token 全保留 + 新增断言（只输出 JSON/禁旧字段/顶层 loop/read_requests·chain_proposals/四组数组上限）全命中 | — |
| A-3 | 通过 | registry system 哈希 `840cb966…` → `996be097…`（勘误后终值，`git diff` 仅 system 一行）；user 哈希不变；`--check` 退出码 0 | — |
| A-4 | 通过 | 定向测试 49 passed（协议+registry+config），0 failed | — |
| A-5 | 通过 | 全量 1148 passed / 0 failed（含工作区既有 test_no_rules_import 1 项——非本任务）；check-backend 通过（compileall + 规则契约 30） | — |
| A-6 | 通过 | **同入口 3/3 全部通过**（act_com_xiaomi_fitness_login_SplashActivity：status=completed + ExplorerObservation 解析通过）；**异构 3/3 全部通过**（activity/service/receiver 各一）；N-4 归因实证见下行 | — |
| A-7 | 通过 | 探针入库自洽：正式 registry 路径 explore_entry（非裸 HTTP）；load_dotenv + os.environ 校验 key；AIRuntime(settings.ai) 构造；入口经 get_entry_points 同源加载；无 key 退出码 2 且不发网络（N-1：env -u 下 load_dotenv 从 .env 补载为设计行为，缺 key 报错路径经代码审查确认） | — |
| A-8 | 通过 | `git status`/`git diff` 审查：变更仅 prompts/explorer/1.0.0/system.md、prompts/registry.yaml、scripts/probe_explorer_prompt.py、backend/tests/test_explorer_protocol.py + 4 份任务文档；explorer.enabled=false 未动；ai.py/explore_entry 版本未动；ai_models.py/explorer.py/config 的 explorer 段零 diff（config/default.yaml 的 ai 段为环境配置非本任务——S-7 排除） | — |
| A-9 | 通过 | git stash 演练：stash（system.md+registry 回旧版）→ 新断言 FAILED（**R-4 连带回退实证**：回退 prompt 必须同步回退测试断言）→ pop 恢复 → sync --check 无漂移 → 断言测试复绿 | — |

> **N-4 归因记录（人工决策项结论）**：探针 6 次（3 同入口 + 3 异构）chain_proposals 均为 0——归因为**探针单轮零上下文设计**（round_index=1、无 code_context、requests_budget=0：模型无代码可分析，输出 done=false + read_requests 是协议正确行为），**非 prompt 过于保守**。实证：同入口带入口方法体上下文的单轮调用产出 **2 条结构正确的 chain_proposals**（getIntent()→handleStartActivityIntentFromOtherApp / getIntent()→saveFastPairingExtra，各 1 跳、hypothesis=possible）+ 4 条 read_requests + loop.done=False。决策：无需调低约束语气。

> 基线证据（2026-08-23 验收前实测）：`test_explorer_protocol.py + test_prompt_registry.py + test_config.py` 基线 49 passed；现行 `system.md` 无"只输出一个 JSON 对象"与显式输出契约；`scripts/` 无探针脚本；`registry.yaml` 旧哈希 `840cb966…` 在值。
>
> 第 2 轮评审（验收方案+任务报告，R-7~R-12 全部采纳闭合）：A-6 冒烟结果回填时须记录**实际 base_url/model**并注明"依赖未提交本地 token-plan 配置"（R-8）；A-9 回退演练改 git stash 方式并在 A-9 行"实测说明"记录（R-9/R-12）。
