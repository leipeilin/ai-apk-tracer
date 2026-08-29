# 代码审查报告：track-progress-console（实现审查）

> **任务编号**：track-progress-console
> **审查日期**：2026-08-29
> **审查对象**：本轮代码变更（`git diff`，含未跟踪新文件 `backend/app/runs/progress.py`、`backend/tests/test_rule_runner.py`、`backend/tests/test_run_progress.py`、`frontend/src/features/runs/TrackProgress.tsx`）+ 依据实施方案 `docs/analysis/console-ui/2026-08-29-track-progress-console-implementation-plan.md` + 验收方案 `docs/analysis/console-ui/2026-08-29-track-progress-console-acceptance-plan.md`
> **审查模型**：glm-5.3-flash（独立子代理，只读审查）
> **状态**：第 1 轮（待处置 / 已闭合）

---

## 1. 审查结论摘要

- **结论：实现忠实落地方案 §3 模块 A~F，方案评审 R-1~R-7 全部落实到位，测试覆盖齐备，可进入提交（建议顺手处置 C-1 中等意见）。** 未发现关键/高危缺陷；唯一实质性缺口是探索轨 gating 判据未把 R-1 新增的 manifest 顶层 `explorer_total_count` 纳入信号——探索轨运行初期（首条 partial 落盘前）progress.explorer 被 gating 抑制为 null，前端显示"探索轨未启用或未记录"，与运行事实相悖（方案残留矛盾，非实现走样，见 C-1）。其余为低危建议。
- **审查方法**：方案 §3 各模块逐条对照 diff 读码核验；rule_runner as_completed 改造的聚合顺序/异常语义逐一推演并与 HEAD 版本比对；explorer partial/observations 产物语义（`explorer.py:159-272`）与 gating 窗口实测推演；新增测试逐条映射验收 A-x/N-x；独立复跑全量 pytest（1386 collected、两轮 exit 0）、ruff（8 个改动文件，4 条报错全部经 `git show HEAD` 证实既有）、`tsc --noEmit`（app/node 两配置均过）。

## 2. 方案落地对照

| 方案项（§条目/A-x） | 实现（文件:行号） | 结论 |
|---|---|---|
| §3.3 模块 A：`build_run_progress` 双轨计算与多级降级 | `backend/app/runs/progress.py:48-153`（rules :48-74、explorer :104-144、聚合 :147-153） | ✅（C-1/C-2 两处见问题清单） |
| 模块 A rules.total 降级链：summary → manifest 顶层 → null | `progress.py:57-60` | ✅ |
| 模块 A rules.processed：glob 计数排除 `RULE_ARTIFACT_KEYS`（评审 R-2） | `progress.py:16,62-66`（`from app.analysis.rule_runner import RULE_ARTIFACT_KEYS` + `path.stem not in RULE_ARTIFACT_KEYS`） | ✅ |
| 模块 A explorer.total 降级链：summary.entry_count → 顶层 explorer_total_count → null（评审 R-1，无 api_entry_table JSON 计数） | `progress.py:126-128`（全文件无 api_entry_table 解析） | ✅ |
| 模块 A explorer.explored 降级链：summary → partial jsonl 校验行数 → observations.json → null | `progress.py:130-138`（`_count_valid_records` :77-92 逐行 JSON 校验，坏行整字段降级） | ✅ |
| 模块 A gating：explorer stage 不存在且无产物 → None | `progress.py:116-124` | ⚠️ 字面忠实方案，但与 R-1 顶层键存在方案内残留矛盾（C-1） |
| 模块 A 畸形产物按字段降级不抛异常 | `progress.py:91-92,98-99`（OSError/JSONDecodeError/UnicodeDecodeError 全捕获）；`_manifest_int` :42-45 排除 bool | ✅ |
| §3.3 模块 B：get_run 接线，try 前初始化 null（评审 R-6），except Exception 兜底 + warning | `backend/app/api/routes.py:148`（初始化）、`:159-165`（内层 try/except Exception + `LOGGER.warning(..., exc_info=True)`） | ✅（内层兜底优于方案"外层"表述：progress 失败不会牵连 manifest/stages 置空） |
| §3.3 模块 C：rule_prescan 前提前写 `rule_total_count` | `backend/app/analysis/orchestrator.py:166-170`（`_stage` 后、`run_all` 前 `update_manifest(run_id, rule_total_count=len(self.rule_runner.discover()))`） | ✅（C-3：discover() 每 scan 3 次调用） |
| 模块 C：`explore_all` 前提前写 `explorer_total_count=len(effective)`（R-1） | `orchestrator.py:1196-1200`（`effective` 排序后、`ExplorerOrchestrator` 构造前，与终态 summary `entry_count` :1267 同源同列表） | ✅ |
| §3.3 模块 D：run_all 逐条落盘（串行路径） | `backend/app/analysis/rule_runner.py:104-110`（循环内 `_run_one` 后立即 `_persist_result`） | ✅ |
| 模块 D：进程池 `executor.map` → `submit + as_completed`，索引排序还原原序 | `rule_runner.py:119-144`（`future_to_index` :128-138、`as_completed` 逐条落盘 :139-143、`sorted(indexed_results)` :144） | ✅ |
| 模块 D：post-loop 聚合逻辑不变，仅移除重复落盘 | `rule_runner.py:145-167`（candidates/failures/coverage_gaps 与 HEAD 版本逐行等价；`_persist_result` :169-177 即原落盘两行） | ✅ |
| 模块 D 异常语义：与 `list(map)` 一致（仅资源类异常传播） | `rule_runner.py:139-143`（`future.result()` 传播）+ `_run_one` 归一未变（:278-356）；`with ProcessPoolExecutor` 退出 shutdown(wait=True) 语义不变 | ✅ |
| §3.3 模块 E：轨切换默认 rules、互斥渲染、轮询条件不变 | `frontend/src/features/runs/RunDetailPage.tsx:30`（`useState<TrackId>('rules')`）、`:103-128`（tablist）、`:130-138`（互斥渲染，`&&` 守卫移除）、`:31-52`（三路 usePolling 未动） | ✅ |
| 模块 E：进度摘要徽标（null 显 "—"） | `RunDetailPage.tsx:22-26`（`trackBadge`）+ `:113/:125` | ✅ |
| §3.3 模块 F：TrackProgress 计数与降级口径 | `frontend/src/features/runs/TrackProgress.tsx:46-96`（completed = processed - (failed ?? 0) 钳 0 :51-53；remaining = total - processed 钳 0 :54-56；failed>0 才追加 :50,66；total null 隐藏进度条 :11-15,35；轨级 null → "未记录"文案 :49,72） | ✅ |
| §3.1 types.ts：遗留 `progress?: number` **替换**为 `RunProgress`（评审 R-4） | `frontend/src/lib/types.ts:32-51`（三个新类型）+ `:71-72`（`progress?: RunProgress \| null` 替换 :50 原字段）；全仓 grep 复核无其他 run 级 progress 消费方（仅上传进度本地 state 与样式类名同名异义） | ✅ |
| §3.1 styles.css：`.track-switcher` 分段按钮 + 进度摘要样式 | `frontend/src/styles.css:361-375`（复用 `var(--…)` 令牌与 `glass-panel`；进度条复用既有 `.progress-track` :35） | ✅ |
| §3.5 测试设计 6 用例 + manifest None | `backend/tests/test_run_progress.py`（8 用例，映射见 §5） | ✅ |
| §3.5 test_api 端到端断言 | `backend/tests/test_api.py:108-129`（`test_get_run_returns_track_progress`：total≥1、processed==total（R-2 成立条件）、explorer is None） | ✅ |
| §3.5 test_rule_runner 新增（评审 R-5） | `backend/tests/test_rule_runner.py:52-71`（串行 max_concurrency=1 / 进程池 =2 双路径，成功+失败一一落盘） | ✅ |
| §3.4 口径说明写进代码注释（评审 R-3） | `progress.py:107-113`（"唯一偏差源为 worker 异常条目——终态计入而 partial 缺行，运行中略小"）；skipped_max_cap 两侧不记录口径一致 | ✅ |
| in-scope 外无私自扩展 | diff 范围 = 方案 §3.1 清单 11 文件 ± 行号漂移；工作区其余改动（config/default.yaml、docs/todo、docs/01/02/03/08）为既有未提交内容，与本任务无关（见 §6） | ✅ |

## 3. 正确性核验

> 抽查关键实现的正确性：边界条件、错误处理路径、并发/状态安全、与现有代码的交互。

- **as_completed 改造的聚合等价性**：`rules = sorted(self.discover(), key=id)`（`rule_runner.py:98`）与结果还原 `sorted(indexed_results, key=lambda pair: pair[0])`（:144）保证 `zip(rules, results)`（:145）按规则原序聚合，candidates/failures 顺序与 HEAD `executor.map` 版本一致（既有回归 `test_rule_index_protocol.py` 串行/并行指纹一致性通过佐证）。异常路径：`future.result()`（:141）传播时 `with` 退出 shutdown(wait=True) 等待剩余任务，与原 `list(map)` 语义相同；差异仅在于新代码中异常前已完成的规则 result 已落盘（原代码异常时零落盘）——属增强而非回归。`_run_one`（:278-356）将超时/输出超限/非零退出/协议错误全部归一为失败 dict 未改动，预期内无新异常源。
- **落盘原子性与 glob 无污染**：`_persist_result`（:169-177）复用 `_write_result`（:179-192，tmp `.{name}.{uuid}.tmp` + `os.replace`）；tmp 文件名以 `.` 开头且以 `.tmp` 结尾，不匹配 `*.json` glob（`progress.py:66`），并发轮询读目录不会读到半写文件。失败规则与成功规则同样落盘（:107-110/:143 无 status 过滤），与 HEAD post-loop 行为一致（`test_rule_runner.py:63-69` 断言）。
- **explorer 运行中口径（R-3 修订语义）实测核对**：partial append 仅在 worker 正常返回路径执行（`explorer.py:188-193`）；worker 异常条目由 scheduler 记 FAILED、终态 `entries_explored += 1`（:227）但无 partial 行——运行中 explored ≤ 终态，与 `progress.py:110-113` 注释一致；skipped_max_cap 提前 return（:187）两侧均不记录（终态 continue :208-209）。partial 为单行 O_APPEND 小记录（:269-270），并发读下若遭遇撕裂行，`_count_valid_records` 整体降级 None（保守不显示可疑数字），下轮 2s 轮询自愈——可接受。
- **gating 与降级链边界**：`_manifest_int` 排除 bool（`progress.py:43`）避免 `True` 计入；`_last_stage` 取同名 stage 最后一条（:21-33）容错 stages 结构不符；manifest 为 None 时 `_rule_progress` 仍可由 rule-results 目录给出 processed（:100-105 测试覆盖），explorer 返回 None。`unexplored` 回退钳位 `max(total - explored, 0)`（:142）与方案"非负才回退否则 null"存在字面偏差（C-2，防御性方向合理）。
- **get_run 接线的容错与兼容**：`run["progress"] = None` 在 try 前初始化（`routes.py:148`），manifest 读取失败走既有 except（:167-169）时 progress 保持 null（N-4 字面满足，R-6 落实）；progress 计算置于 index manifest 解析**之前**，后者抛错不会丢 progress；`repository.get_run` 每次返回新 dict（`repository.py:474-476`），原地写 `run["progress"]` 无共享状态风险；`_public_run`（:43-51）对 progress 透传。`storage.run_dir()` 默认 must_exist，异常被内层 except 兜底——progress 永不阻塞 run 响应。
- **manifest 顶层新键对既有消费方透明**：报告统计读的是 rule_prescan **stage summary** 而非 manifest 顶层（`app/findings/report.py:611,616`），新键零影响；`update_manifest` 原子替换（`storage.py:126-142`）与轮询读并发安全；`explorer_total_count` 仅在 `explorer.enabled` 分支内写入（`orchestrator.py:229-234` 门禁），未启用 run 的 manifest 无此键。
- **前端轮询与状态安全**：progress 随既有 getRun 2s 轮询返回，无新增轮询循环（`RunDetailPage.tsx:31-52` 仅三路 usePolling，N-7 的 SQLite 读锁约束守住）；TrackProgress 为纯函数组件，轨切换仅本地 state（:30），findings/explorerQueue 数据由页面层轮询持有，切换零请求零闪烁；FindingsPanel 切换卸载导致筛选态重置为方案 §3.3/§4 已声明的取舍。
- **I/O 量级**：每 2s 轮询触发 `build_run_progress` = rule-results glob（34 量级）+ partial jsonl 全文件逐行校验（每行 ~200B×数百）——与方案 §4 评估一致，无额外 DB 查询。

## 4. 问题清单（按严重度排序）

**【C-1】【中】探索轨 gating 未纳入 manifest 顶层 `explorer_total_count` 信号：探索轨运行初期（首条 partial 落盘前）progress.explorer 被抑制为 null，前端显示"探索轨未启用或未记录"，与运行事实相悖**
证据：`backend/app/runs/progress.py:116-124`——gating 仅判 `_last_stage(manifest, "explorer")` 与 explorer 目录三产物文件存在性；而 `_stage()` 只更新顶层 `stage` 字段、不写 stages 列表（`orchestrator.py:1551-1553`），`_record_stage` 在阶段结束才 append（`orchestrator.py:1266`，探索阶段运行中 stages 无 explorer 条目）；`explorer_total_count` 已在 `explore_all` 前写入顶层（`orchestrator.py:1200`），但首条 partial 行要等第一个入口完成（含 AI 调用，`explorer.py:188-193`）才落盘——该窗口内四项条件"轨在运行/总量已知"与展示"未启用或未记录"矛盾，且 tab 徽标显示"—/—"。方案侧根因：R-1 修订为模块 C 增加了顶层键，但模块 A 的 gating 判据文字未同步更新，实现按方案字面落地。
修订建议：`_explorer_progress` 的 gating 增加 manifest 顶层键判定，例如将 `progress.py:123` 改为 `if not stage_exists and not has_products and _manifest_int(manifest if isinstance(manifest, dict) else None, "explorer_total_count") is None: return None`（历史 run 无该键不受影响）；同步在 `test_run_progress.py` 补一条"仅顶层 explorer_total_count、无 stage 无产物 → explorer={total:N, explored:null, …}"用例。若主代理认为该窗口可接受，则应在方案 §3.3 模块 A 补记该取舍。

**【C-2】【低】`unexplored` 回退钳位与方案字面偏差：方案要求"两者均可算且非负"否则 null，实现将负差钳为 0**
证据：`backend/app/runs/progress.py:141-142`（`unexplored = max(total - explored, 0)`）vs 实施方案 §3.3 模块 A（"`fallback total - explored`（两者均可算且非负）→ `null`"）。
修订建议：二选一保持文档-代码一致——(a) 代码改为负差时置 null；(b) 方案 §3.3 补记"负差防御性钳位为 0"口径（推荐，钳位比显示 "—" 更合理）。无论哪种，不影响验收结论。

**【C-3】【低】单次 scan 内 `discover()` 被调用 3 次，每次全量 glob + 逐个 `yaml.safe_load`**
证据：`backend/app/analysis/orchestrator.py:170`（本轮新增）、`rule_runner.py:98`（run_all 内部，既有）、`orchestrator.py:193`（终态 summary，既有）。34 条规则 × 3 次 YAML 解析，单次 scan 毫秒级，属可忽略浪费；且三次调用间若 rules 目录变化（现实中不会）存在总量口径漂移的理论窗口。
修订建议：`orchestrator.py:170` 复用一次 discover 结果（如 `discovered = self.rule_runner.discover()` 后 `update_manifest(rule_total_count=len(discovered))`），终态 summary :193 同样复用或保持现状均可；纯节省项，不阻塞提交。

**【C-4】【低】轨切换 tablist 的 ARIA 关联不完整：`role="tab"` 无 `aria-controls`/tabpanel 配对，也无方向键导航**
证据：`frontend/src/features/runs/RunDetailPage.tsx:103-128`（`role="tablist"` + 两个 `role="tab"`，面板区 :130-138 未标 `role="tabpanel"`/id）。按钮原生可键盘聚焦激活，无功能性障碍，仅语义层欠完整（A-4 验收记录以 [selected] 实测为准）。
修订建议：为两个 tab 加 `id` 与 `aria-controls="track-panel"`、面板容器加对应 `id` + `role="tabpanel"`；或简化为 `aria-pressed` 切换按钮组，去掉 tablist 语义。建议性修正，可与后续 UI 打磨合并。

> 严重度定义：关键（验收落空/引入回归）/ 高（明显缺陷须修复）/ 中（不完整应补充）/ 低（建议性）。

## 5. 测试覆盖核验

| 覆盖点 | 测试（函数名） | 结论 |
|---|---|---|
| A-1 / §3.5 用例 1 全信号精确 | `test_full_signals_precise`（test_run_progress.py:24-37） | ✅ |
| A-1 / §3.5 用例 2 运行中降级（R-1 顶层键 + partial jsonl，含空行跳过） | `test_running_falls_back_to_manifest_counts_and_partial_jsonl`（:40-53，造数含尾随空行 :47） | ✅ |
| N-1 / §3.5 用例 3 历史 run（observations.json 降级、total 不伪造） | `test_historical_run_uses_observations`（:56-65） | ✅ |
| N-3 / §3.5 用例 4 探索轨未启用 | `test_explorer_disabled_returns_none`（:68-71） | ✅ |
| N-2 / §3.5 用例 5 畸形产物按字段降级 | `test_malformed_products_degrade_per_field`（:74-81） | ✅ |
| R-2 / §3.5 用例 6 产物词干排除 + 缺目录计 0 | `test_rule_results_excludes_artifact_stems`（:84-92）、`test_rule_results_missing_dir_counts_zero_when_total_known`（:95-97） | ✅ |
| N-4 / manifest None | `test_manifest_none_degrades`（:100-105） | ✅ |
| A-2 getRun 端到端（终态 processed==total、explorer null） | `test_get_run_returns_track_progress`（test_api.py:108-129） | ✅ |
| A-3 / N-5 run_all 成功+失败一一落盘（串行+进程池双路径、失败归一、candidates 仅成功） | `test_run_all_persists_every_rule_result_including_failures`（test_rule_runner.py:52-71） | ✅ |
| A-3 既有 run_all 回归（串行/并行一致性、重复执行、失败归一） | `test_rule_index_protocol.py`（串行并行指纹一致、`test_rule_runner_can_repeat_same_run_without_stale_workdir` 等）、`test_manual_review_regressions.py` | ✅（全量套件通过） |
| R-1 落实回归（explorer_total_count 与终态 entry_count 同源） | `test_explorer_stage_normalizes_validated_into_main_candidates` 新增断言（test_explorer.py:588-590） | ✅ |
| N-6 运行中近似 ≤ 终态 | 无直接单测（acceptance 已声明以代码走查替代；本审查 §3 独立走查 `explorer.py:187/:193/:208-209/:227` 结论一致） | ✅*（与验收记录同口径） |
| A-4/A-5/A-6/A-8 浏览器实测项 | 验收 §6 已记录；本只读审查以前端代码走查佐证无矛盾（互斥渲染/轮询结构/降级文案均与记录一致） | ✅*（采信记录） |

- 无空断言/恒真测试：8+1+1 条新增用例均有实质断言（精确 dict 相等、文件存在性、status 归一、双路径参数化）。
- 未覆盖的小缺口（不单列问题）：gating 对顶层 `explorer_total_count` 的独立信号（C-1 修订建议中一并补）、`unexplored` 负差钳位（C-2）、`_last_stage` 同名取末条——均随 C-1/C-2 处置补齐即可。

## 6. 回归核验

- **测试基线**：独立复跑 `cd backend && .venv/bin/python -m pytest` 两轮，exit code 0 / 0 failed；`pytest --collect-only` 实测累计 **1386**（基线 1376 + 新增 10：test_run_progress 8 + test_rule_runner 1 + test_api 1），与验收 §6 记录一致，只增不减。✅
- **lint**：`backend/.venv/bin/ruff check` 8 个改动文件共 4 条报错，**全部为 HEAD 既有**（经 `git show HEAD` 对照证实）：`rule_runner.py:19 UP035`、`rule_runner.py:314 PLW1509`、`test_api.py:1 I001`、`test_explorer.py:8 I001`；新增文件（progress.py / test_run_progress.py / test_rule_runner.py）与 routes.py / orchestrator.py 全部干净。两处说明：① 验收 §6 记录"仅 2 条既有报错"系其 lint 文件清单（§3 约定的 5 文件）未含两个测试文件，其 I001 同为既有，非本轮引入；② 本轮 rule_runner 的 import 合并实际上**修掉了** HEAD 既有的 rule_runner I001。本轮零新增 lint 债务。✅
- **前端类型门禁**：`tsc --noEmit -p tsconfig.app.json` 与 `-p tsconfig.node.json` 独立复验通过（A-7 的 tsc 部分；vite 产包采信验收记录）。
- **check-backend.sh 规则契约断言**：脚本硬编码 30（`scripts/check-backend.sh:13`），rules/ 实际 34 个 rule.yaml——既有漂移，与本任务无关（验收 §6 已声明），建议另行修正。
- **默认行为兼容**：get_run 响应仅新增 `progress` 键（`_public_run` 透传，其余构造逻辑未动）；manifest 顶层两新键经核无既有消费方读取（报告统计走 stage summary，`report.py:611-616`）；不开启 explorer 的 run `progress.explorer is None`（A-2 断言）；历史 run 走 summary/observations.json 降级链（N-1）；`test_api.py` 既有断言全过。✅
- **回归风险点走查**：rule_runner 改造仅动执行编排与落盘时机，规则协议/资源限制/归一逻辑零改动；`run_all` 重复执行场景（先写坏 result 再跑）由既有 `test_rule_runner_can_repeat_same_run_without_stale_workdir` 守护，逐条落盘不改变最终文件内容。
- **工作区范围提示**：当前工作区另有未提交的既有改动 `config/default.yaml`、`docs/todo/2026-08-27-exploration-validation-todo.md`、`docs/01-项目概述.md`、`docs/02-架构设计.md`、`docs/03-分析流程.md`、`docs/08-L1-AI分诊与语义复核优化设计.md`（内容为规则 33→34、探索/核验/资产轨的文档化，早于本任务），均不属于本任务范围——提交时注意与本任务 11 个文件分离，避免混入。

---

## 7. 处置记录（主代理回填，2026-08-29）

> 主代理逐条独立复核：C-1 经代码走查证实（`orchestrator.py` `_stage` 仅更新顶层 stage 字段、`_record_stage` 阶段结束才 append stages 列表；`explorer_total_count` 在 `explore_all` 前已写顶层，而首条 partial 依赖首个入口完成——窗口真实存在）；C-2/C-3/C-4 与代码事实一致。**四条全部采纳，已完成修订并复验。**

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| C-1 | 中 | **采纳**：`_explorer_progress` gating 增加顶层 `explorer_total_count` 信号（`has_top_level_total`，历史 run 无该键不受影响）；补单测 `test_explorer_running_early_window_uses_top_level_total`（仅顶层键、无 stage 无产物 → explorer={total:12, explored:null, unexplored:null}）；方案 §3.3 模块 A gating 文字同步修订 | `backend/app/runs/progress.py`、`backend/tests/test_run_progress.py`、实施方案 §3.3 模块 A |
| C-2 | 低 | **采纳**（采审查建议 b）：保留防御性钳位实现，方案 §3.3 模块 A 补记"负差防御性钳位为 0"口径，文档-代码一致 | 实施方案 §3.3 模块 A |
| C-3 | 低 | **采纳**（部分）：orchestrator 内 `rule_total_count = len(self.rule_runner.discover())` 一次计算，提前写入（:170）与终态 summary（:193）共用；`run_all` 内部 discover() 调用不动（避免改 run_all 接口，收益微小） | `backend/app/analysis/orchestrator.py` |
| C-4 | 低 | **采纳**：两个 tab 加 `id`（track-tab-rules/track-tab-explorer）与 `aria-controls="track-panel"`；面板容器加 `role="tabpanel"` + `aria-labelledby` 动态指向当前 tab | `frontend/src/features/runs/RunDetailPage.tsx` |

**闭合结论**：C-1~C-4 全部采纳并修订完成；复验结果——全量 pytest **1387 passed / 0 failed**（新增 C-1 单测 1 条，基线 1376 + 11）、`npm run build` 通过、浏览器复测切换功能正常（ARIA 配对生效）。无未处置的关键/高意见；无开放决策项。**达到提交门槛：验收 §2 全部 A-x 通过、基线只增不减（1387 ≥ 1376）、改动文件无新增 lint 债务（4 条报错均经 git show HEAD 证实为 HEAD 既有）、代码审查处置全部闭合。**
