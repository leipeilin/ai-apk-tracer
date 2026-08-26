# M3/M4 代码审查报告（2026-08-26）

> **审查范围**：`6622493..c3784a1`（M3-1/M3-1 补评审/M3-2/SEED-HOPS/T4.1~T4.4/上轮审查修复，共 9 提交）
> **审查模型**：deepseek-v4-flash（独立只读子代理，98 次工具调用，786s）
> **审查方法**：当前最终态代码级审查 + 上轮 7 项修复落地复核 + 真实产物反查（dc24a077 candidates.json 逐候选核验标注键） + 跨任务链路一致性检查
> **独立验证**（主代理补跑）：全量 pytest **1251 passed / 0 failed**；`sync-ai-protocol.py --check` 退出码 0

## 1. 审查结论摘要

M3 报告链路（门禁/降级/零可执行/防虚构）实现正确、测试充分，可放行；M4 评估机制（runner/gate/report_quality）结构完备且三本账与真实 manifest 逐项一致，但 **explorer_hit 宽松子串匹配在真实数据上产生跨组件假阳**——shop 基线唯一命中实为 QQ SDK 组件误撞合成 case 的泛化键，当前 0.167 基线不可作门槛真值。上轮 7 项修复 6 项落地扎实，唯 4.3 澄清与代码事实不符（报告 AI 请求实无 max_tokens 上限）。

## 2. 上轮修复复核表（7 项）

| 项 | 判定 | 证据 |
|---|---|---|
| 4.1 标注补齐 | **修好（带保留）** | extra-close 标注 hit、键派生合理；真实 dc24a077 候选无 extra_close_url/go2CloseSet（该 run 早于 seed-hops，属预期），MainActivity 源候选与 startActivity sink 候选分属不同候选未串扰；manifest v3/`_HIT_CASES`/分母 12 同步；方案口径修正已记录。保留意见见 R-1 |
| 4.2 基线快照 | **修好（带保留）** | 双快照入库 + gate 可消费；三本账 424/0/20/29/62/486/151 与 manifest 逐项核对一致。保留：README 归因错误（R-2） |
| 4.3 澄清 | **澄清失实** | "None→沿用 context_budget 3000"无代码支撑：orchestrator 显式传参（orchestrator.py:76），report-draft 路由 `create_analyzer()` 无参（routes.py:306）→ `_max_output_tokens=None`（ai.py:148）→ payload 不含 max_tokens（ai.py:1372）。原报告"未传"构成真实风险（R-4） |
| 4.4 chmod | 修好 | `chmod(0o600)` + 双断言测试 |
| 4.5 locations | 修好 | 三桶对齐 + 正负例测试 |
| 4.6 容错误处理 | 修好 | `parser.error`（退出码 2）+ 负例测试 |
| 4.7 to_thread | 修好（按采纳范围） | 落盘包 `to_thread`；get_finding/run_dir 仍同步（处置已声明，本地单用户可接受） |

## 3. 新发现问题清单

| 编号 | 严重度 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| **R-1** | **高** | **explorer_hit 宽松子串假阳——shop 基线唯一命中为跨组件误撞**：router-validation-overwritten（合成 RouterActivity 语义）被 `com.tencent.tauth.AuthActivity`（QQ 登录 SDK）候选命中（source="Intent extras" + sink="WebView.loadUrl"，unverified/failed_hop/自定义 sink）。键 "Intent extras"/"loadUrl" 过泛；extra-close 的 "startActivity" 同样高危（"startActivityForResult" 含该子串）。**后果：m4-shop hit_rate=0.167 实为假阳膨胀；未来修正碰撞归零会被 gate 以"劣化"BLOCK 正确性修复** | candidates.json:495-541、router-validation-overwritten.json:42-48 | hit 判定叠加组件类名限定或词边界；按 validation 状态分层计数；基线收紧后重刷 |
| R-2 | 中 | baselines/README 命中归因与快照事实不符：README 称"remote-aidl-unguarded 被命中"，快照实际 `explorer_hits=["router-validation-overwritten"]`；真实候选全库无 SportApiStub/RemoteService/startSport（逐一反查 0 命中） | README.md:8 vs m4-shop.json | 修正归因；基线快照内嵌 generator 元数据 |
| R-3 | 中 | T4.3 合法 component_kind 集缺 "webview"/"crypto"（真实 shop run 含 webview≥10、crypto≥7 个 finding）——confirmed 的此类报告触发 "component_kind 非法"→全文档 FAIL | report_quality.py:16 | 合法集由 finding 组件域派生（或与 poc.py 共享单一常量）；补用例 |
| R-4 | 中 | 报告 AI 请求无 max_tokens 上限（4.3 澄清失实的实际后果）——报告输出全为 LongText 长文本，逐次人工点击成本无界、无缓存、无预算记账 | routes.py:306、ai.py:1372 | 路由补 `max_output_tokens=settings.context_budget.max_output_tokens`（一行修复） |
| R-5 | 低 | evaluate_runs 聚合漏 read_requests（per_run 含 20，costs_total 不含）——口径不一致 | runner.py:153-157 | 聚合键补 read_requests |
| R-6 | 低 | get_seed_hops 连接管理（`with connect` 事务语义非关闭）+ LIMIT 8 截断不可观测（无截断计数入 metadata） | call_tree.py:177 | 连接显式 close；截断计数入 observation（评估用） |
| R-7 | 低 | _check_poc docstring 称 WARN 级但实现任一 violation 判 FAIL | report_quality.py:92-93 vs :120 | 修 docstring 或分级 |
| R-8 | 低 | 投影的 l2_* 键对旧 schema finding（20260815 用 "verdict" 无 candidate_verdict/harm/flaw_holds）全落 None——端到端测试恰用该老 finding 且断言未覆盖 l2 字段（静默通过）；20260822 新 run 键名正常 | 真实 finding 对比 | l2 投影补 "verdict" 旧键兼容或测试换新 finding |

## 4. 跨任务一致性检查表

| 链路 | 结论 |
|---|---|
| M3 deterministic 27 键 ↔ T4.3 检查口径 | 一致；**例外**：component_kind 域不一致（R-3） |
| T4.1 标注键 ↔ T4.2 evaluate_explorer 消费 | 一致（matches 直调/hop_ids 同口径/conditional 独立 rate） |
| T4.2 成本提取 ↔ manifest stages 字段名 | 一致（逐项吻合） |
| T4.4 gate 白名单 ↔ 基线/报告结构 | aggregate.* 可解析真实基线；结构混用 BLOCK 有效 |
| T4.1 ↔ baselines 数字链 | hit_total=6 与标注集一致；但归因错（R-2）+ 命中假阳（R-1）——**数字自洽但语义失真** |
| M3 降级 ↔ prompt 协议 | evidence_refs 确定性补齐防虚构成立；降级 fallback 可观测但 prompt_version/model 落 None（trace 弱化） |

## 5. 测试盲区清单

1. 无同 finding 并发双请求竞态测试（`write_text` 非原子，双写可撕裂/last-wins）；
2. `_ACTIVE_PROMPT_CASES` 不含 report/explorer/verify/deep-dive——"prompt 必须声明全部 required 字段"的系统性回归防线未覆盖新 prompt（仅手工字符串断言）；
3. gate 测试全合成 fixture，无对 `evaluation/baselines/*.json` 的真实消费性测试；
4. report_quality 无 webview/crypto 组件用例（R-3 即盲区）；
5. explorer_hit 无真实候选快照回归集（R-1 假阳正是无测试拦截的现实碰撞）；
6. golden 标注 CI 依赖本地产物（端到端 skip，合成兜底覆盖弱化）。

## 6. 独立验证结果（主代理补跑）

- 全量 pytest：**1251 passed / 0 failed**（39.7s）
- `sync-ai-protocol.py --check`：退出码 0（registry/schema/模板哈希全一致）

## 7. 放行建议

- **M3：可放行（附条件）**——条件：修 R-4（一行传参）与 R-2 文档更正后合入；
- **M4：机制放行、指标采信暂缓**——R-1 假阳使 m4-shop 基线 0.167 不可作为优化门槛真值（会把正确性修复判为劣化）；建议收紧匹配键（组件限定/词边界）、修 R-3、重刷基线后，再让 T4.4 承担守门职责。R-5~R-8 随批处理。

---

## 8. 被审查方处置意见（主代理回填，2026-08-26）

**总体**：R-1~R-8 全部认同（R-1/R-3 已独立复验：假阳候选 1 个实证在库；真实组件域为 activity/crypto/manifest/provider/receiver/service/webview——合法集缺 crypto/manifest/webview 三个）。上轮 4.3 澄清失实确认——主代理上轮的"默认路径"判断错误，接受 R-4 批评。

| 编号 | 认同度 | 处置 | 修复方案 |
|---|---|---|---|
| R-1 | **完全认同**（复验：假阳候选 source="Intent extras" 实证在库） | 修复 | ①`matches()` 加词边界（regex `\b`——"startActivity" 不再匹配 "startActivityForResult"；method_id 通道 `startActivity:33` 仍命中）②标注键收紧（router-validation 删泛键 "Intent extras"；extra-close 的 "startActivity" 依赖词边界精确化）③真实假阳回归测试（AuthActivity 候选不命中）④基线重刷（假阳归零后如实）⑤validation 分层计数记 backlog（不本轮做——复杂度收益比不佳） |
| R-2 | 完全认同 | 修复 | README 归因修正（随 R-1 重刷后按实际命中清单如实记录） |
| R-3 | 完全认同（复验：组件域多 crypto/manifest/webview） | 修复 | 合法集改为 finding 组件域全集（activity/service/receiver/provider/webview/crypto/manifest/other）+ 与 poc.py 兜底映射共享常量 + webview/crypto 用例 |
| R-4 | 完全认同（上轮澄清错误确认） | 修复 | routes.py 补 `max_output_tokens=settings.context_budget.max_output_tokens`（一行，对齐 orchestrator.py:76 先例） |
| R-5 | 完全认同 | 修复 | costs_total 聚合键补 read_requests |
| R-6 | **部分认同** | 部分修复 | 连接管理修（显式 close——`with` 确为事务语义非关闭）；**截断观测记 backlog**（LIMIT 截断是设计取向，COUNT 查询翻倍成本不值——子代理亦标注"可接受"） |
| R-7 | 完全认同 | 修复（修实现而非 docstring） | _check_poc 按 T4.3 原方案分级：executable 非法/kind 非法 → FAIL；命令占位符/notes → WARN（实现当初偷懒全 FAIL，违背原设计） |
| R-8 | 完全认同 | 修复 | 投影 l2_verdict 兼容旧键 `verdict`（20260815 产物）；harm/flaw_holds 旧 schema 无对应留 None（诚实） |

测试盲区 6 项：本轮随修复覆盖 2 项（真实假阳回归=R-1；webview/crypto 用例=R-3）；并发竞态/_ACTIVE_PROMPT_CASES 扩展/gate 真实基线消费测试/golden CI 兜底 4 项记 backlog（独立任务规模）。
