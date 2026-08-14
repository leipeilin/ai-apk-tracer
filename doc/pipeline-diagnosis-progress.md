# 管线效能排查进度

> 建立时间：2026-08-06
> 触发原因：实测发现 AI 分析占总耗时 99.2%（26188s / 26387s），疑似对最终定级无贡献
> 排查原则：先证伪再优化；每项结论必须有代码或数据支撑，不接受推测

## ⚠️ 重要前提修正（排查过程中发现）

**最新 run（20260802T102857Z）的产物是 8月2日跑的，而决策相关代码在 8月4日被修改过：**

| 文件 | 最后修改 |
|---|---|
| `findings/aggregate.py` | 2026-08-04 23:42 |
| `findings/decision.py` | 2026-08-04 10:54 |
| `findings/review_state.py` | 2026-08-04 01:42 |
| **run 创建时间** | **2026-08-02 10:28** |

**因此："AI 贡献为 0"这一结论建立在过期产物上，不成立。** 所有基于现有 run 的价值判断都必须重跑后再评估。这是本次排查最重要的发现。

## 基线数据（20260802T102857Z run，com.mi.health，⚠️ 旧代码产物）

| 指标 | 实测值 |
|---|---|
| 总耗时 | 26387s ≈ 7.33 小时 |
| ai_analysis | 26188s（99.2%），单候选均 115s |
| 确定性阶段合计 | 199s（0.8%） |
| 候选 → Finding | 242 → 136 |
| AI 状态 | ai_completed 92 / ai_partial 31 / ai_incomplete 13 |
| evidence_level | L1 = 121，**L2 = 15** |
| severity | informational 121 / pending 15 |
| review_status | pending_ai 123 / **ai_false_positive 5** / pending_manual 4 / manual_false_positive 3 / **confirmed 1** |

## 排查清单

| # | 优先级 | 方向 | 状态 |
|---|---|---|---|
| D1 | P0 | `evidence_decision` 全为 None——决策链路是否断裂 | ✅ 已完成 |
| D2 | P0 | severity 全部 informational/pending——定级是否异常 | ✅ 已完成 |
| D3 | P0 | CONTEXT_EXPANSION_STALLED 根因 | ✅ 已完成 |
| D4 | P0 | **用当前代码重跑一次，重新评估 AI 价值** | ⬜ 未开始（阻塞后续判断） |
| D5 | P1 | 单候选 115s 的构成——并发/缓存/轮次 | ✅ 已完成 |
| D6 | P2 | 候选 242 → Finding 136 的收敛合理性 | ⬜ 未开始 |

## 已实施的修复（2026-08-06）

### F1 提高 AI 并发度（对应 D5 措施 1）

**改动**：`config/default.yaml`

| 参数 | 原值 | 新值 | 上限 |
|---|---|---|---|
| `max_concurrent` | 6 | **20** | 64 |
| `candidate_concurrency` | 4 | **12** | 32 |
| `provider_max_in_flight` | 4 | **12** | — |

同时补充注释说明三者协同关系：`provider_max_in_flight` 是同 base_url/model 的在途请求闸门（实际瓶颈），`max_concurrent` 为进程级总闸门须 >= 前两者；若频繁 429 应优先下调 `provider_max_in_flight` 而非 `candidate_concurrency`，以保留候选级并行度。

**预期**：228 候选 / 并发 12 = 19 批（原 57 批），耗时约 **7.3h → 2.4h**（未含超时重试改善）。

### F2 平台事实随候选下发（对应 D3 根因 1）

**问题**：AI 反复请求 `targetSdkVersion` 等 Manifest 配置项，但只有代码扩片通道，永远解析不到 → `CONTEXT_EXPANSION_STALLED`。实测原 `platform_assumptions` 仅含 `analysis_platform_api=36` 一项。

**改动 1**：`backend/app/analysis/manifest.py` —— 补充 application 级安全属性解析

```python
app_debuggable = _bool(_attr(application, "debuggable")) ...
app_allow_backup = _bool(_attr(application, "allowBackup")) ...
app_cleartext = _bool(_attr(application, "usesCleartextTraffic")) ...
```

并加入返回字典：`debuggable`（未声明时按平台默认 False，不作 unknown）、`allow_backup`、`uses_cleartext_traffic`。

**改动 2**：`rules/shared/detector.py` —— 新增 `_platform_assumptions()`，替换原单字段硬编码

输出 7 项平台事实，缺失值显式标记 `unknown`（不省略键）：
`analysis_platform_api` / `target_sdk` / `min_sdk` / `compile_sdk_version` / `debuggable` / `allow_backup` / `uses_cleartext_traffic`

**验证结果**（构造含全部属性的 Manifest 实测）：

```
真实 manifest:  target_sdk=34  min_sdk=24  debuggable=True  allow_backup=Trueuses_cleartext_traffic=False
空 manifest: target_sdk=unknown  min_sdk=unknown  debuggable=False  allow_backup=unknown
```

**回归验证**：`scripts/check-backend.sh` → **465 passed**，规则契约检查 18 条通过。

**预期效果**：AI 无需再为 targetSdk/debuggable 类事实发起扩片请求，直接减少无效轮次；同时为后续 crypto/cleartext 类规则提供现成事实基础。

### F3 解除 preflight 单点熔断（对应 D7）

**问题**：preflight 是全流程唯一"一次失败即全量归零"的环节——解析比真实分析更严格，且不允许 repair。

**改动**：新增 `ai.preflight_strict_protocol`（默认 `false`），preflight 默认启用宽松解析并可进入一次 repair；宽松与 repair 都失败时仍熔断。详见下方「D7 修复」小节。

**回归验证**：`scripts/check-all.sh` → **467 passed**（新增 2 条测试）+ 18 条规则契约 + 前端构建通过。

## 当前阶段性结论

1. **"AI 无贡献"不成立**——该结论基于早于代码修改的过期产物，`review_status` 实际含 `ai_false_positive` 5 例、`confirmed` 1 例（D1）。
2. **定级链路正常**——全 informational 是 L1 判据的正确结果；真问题是 15 个 L2 候选全部因 critical gap 卡在 pending（D2）。
3. **CONTEXT_EXPANSION_STALLED 主因是请求类型错配**，不是 JADX 失败；补 Manifest 摘要到初始切片即可缓解（D3）。
4. **7.3 小时可压缩至约 2 小时**——并发度仅 4（上限 32），缓存命中率 0.4%，且疑似存在超时重试（D5）。

**下一步唯一动作**：执行 D4。D7 已修复，阻塞解除；须使用与基线相同的 APK（sha256 `1c55d3fb9f95...`）重跑，核对决策字段落盘、加速比（对照 26188s）、`CONTEXT_EXPANSION_STALLED` 数量（对照 44/44）。

---

## D4 第一次重跑验证结果（run 20260806T143106Z）

**状态**：❌ **验证未成立** —— AI 阶段被跳过，F1/F2 的效果均无法评估。同时发现一个新的 P0 阻塞缺陷。

### 实测数据

| 阶段 | 状态 | 耗时(s) |
|---|---|---|
| basic_check | completed | 0.0 |
| decompiling | partial | 64.9 |
| rule_prescan | partial | 343.3 |
| candidate_funnel | completed | 0.1 |
| code_slicing | completed | 8.1 |
| **ai_analysis** | **skipped** | 10.8 |
| evidence_integrity_validation | completed | 15.0 |
| aggregation | completed | 0.6 |
| **合计** | | **442.8s ≈ 0.12h** |

产出：候选 472 → finding 458；`analysis_incomplete = true`；`circuit_open = true`，`skipped = 79`。

### 两个使本次验证失效的因素

1. **AI 阶段完全没跑** —— `ai_analysis: skipped`，preflight 失败并触发熔断（`circuit_breaking: true`），79 个候选全部跳过。因此：
   - F1（并发提升）**未被检验**：没有任何 AI 并发发生；
   - F2（平台事实）**未被检验**：`CONTEXT_EXPANSION_STALLED` 计数为 0 是因为压根没进入扩片阶段，不是因为修复生效；
   - 442.8s 的总耗时**不能**与基线 26387s 比较——这是"AI 关闭"与"AI 开启"的对比，不是加速比。
2. **APK 不是同一个** —— 新 run APK sha256 `2a80fc5a8735...`（149.5 MB），基线为 `1c55d3fb9f95...`（285.8 MB）。源文件数 49104 vs 24908，候选 472 vs 242。**跨 APK 数据不可直接对比。**

### 🔴 新发现 P0 缺陷：D7 preflight 无 repair 兜底导致全量熔断

**现象**：preflight 返回 HTTP 200 但 schema 校验失败 → `AI_SCHEMA_INVALID` → 整个 run 的 AI 分析被熔断跳过。

**关键对比**（同一模型 `deepseek-v4-flash`、同一 base_url）：

| 指标 | 基线 20260802（成功） | 新 run 20260806（失败） |
|---|---|---|
| `analyzer_version` | 1.3.0 | **2.2.0** |
| `structured_output_mode` | None | **json_object** |
| `latency_ms` | 863 | **10813** |
| `repair_attempts` | — | **0** |

**根因判断**：`analyzer_version` 由 1.3.0 升至 2.2.0 后引入 `response_format: json_object` 严格模式（`ai.py:236`），但 `deepseek-v4-flash` 在该端点下未按纯 JSON 对象返回（延迟 10.8s 说明模型输出了长文本）。

**设计缺陷**：`ai.py:620-628` 对 `analysis_track == "preflight"` **直接返回失败且 `circuit_breaking=True`，不走 repair 流程**——而其他阶段有 `_repair_output()` 兜底。代码中已存在宽松解析能力（`_extract_first_json_object` 支持 markdown 围栏与周边文本，`ai.py:1010-1018`），但 preflight 未使用。

**影响量化**：一次 preflight 的 schema 抖动 = 整个 run 的 AI 能力归零。本次 79 个候选全部跳过，7 小时的分析价值直接变 0。这是**单点故障**，违反"defense in depth"。

**已排除的方向**：提示词强制 JSON **不可行**——三道防线早已到位却全部失效：API 层已发 `response_format: {"type": "json_object"}`（`ai.py:1212`）、`preflight/1.0.0/system.md` 已写明"只输出一个 JSON 对象，不得输出 Markdown、代码围栏或额外说明"、`temperature=0`。模型仍返回长文本（延迟 10813ms vs 基线 863ms）。提示词是"请求"不是"保证"，对带思维链的模型属概率问题，不是措辞问题。

### D7 修复（已实施 2026-08-06）✅

**核心思路**：把 preflight 的解析严格度降到与普通分析同级，同时保留"两者都失败才熔断"的安全阀。preflight 的目的是"确认模型可用"，判定标准不应比真实使用场景更苛刻。

| # | 改动 | 文件 | 说明 |
|---|---|---|---|
| 1 | 新增配置项 `ai.preflight_strict_protocol`（默认 `false`） | `backend/app/config.py:85-88`、`config/default.yaml:127-131` | 默认宽松；设为 `true` 恢复旧的"一次不合格即熔断"语义 |
| 2 | preflight 启用宽松解析 | `ai.py:576-578, 588` | `allow_relaxed=not preflight_strict_only`，可剥离 markdown 围栏、从周边文本提取首个 JSON |
| 3 | 移除 preflight 的硬熔断分支 | `ai.py:623` | `if analysis_track == "preflight"` → `if preflight_strict_only`；非严格模式落入 repair 通道 |
| 4 | repair 失败后 preflight 仍熔断 | `ai.py:651-655` | `circuit_breaking` 增加 `or analysis_track == "preflight"`，避免带着不可用协议跑全量候选 |
| 5 | 放宽轨迹可审计 | `ai.py:578` | metadata 记录 `preflight_strict_protocol`；原有 `protocol_relaxed`/`protocol_relaxation` 保持写入 |

**安全边界未放宽**（关键）：
- 重复键检测 `_DuplicateJSONKeyError` 始终生效（防 `{"ok":true,"ok":false}` 类混淆）；
- Pydantic `additionalProperties: false` 校验不变；
- 放宽只作用于"从噪音中提取 JSON"，不作用于"接受不合规字段"。

**验证**（`scripts/check-all.sh` 全绿，467 passed + 18 条规则契约 + 前端构建）：

| 场景 | relaxed=True | relaxed=False |
|---|---|---|
| 纯 JSON | OK（无放宽） | OK |
| markdown 围栏 | OK，`relaxation=markdown_fence` | JSONDecodeError |
| 周边文本包裹 | OK，`relaxation=surrounding_text` | JSONDecodeError |
| 重复键 | **DUPLICATE_KEY_REJECTED** | 同样拒绝 |

**测试变更**：原 `test_preflight_rejects_markdown_wrapped_json_without_repair` 编码的正是被修的缺陷行为，已替换为三条：
- `test_preflight_accepts_markdown_wrapped_json_by_default`——默认宽松接受，并校验 `protocol_relaxation=markdown_fence`；
- `test_preflight_rejects_markdown_wrapped_json_when_strict`——`preflight_strict_protocol=true` 时保持旧语义、不进 repair；
- `test_preflight_circuit_breaks_when_relaxed_and_repair_both_fail`——纯自然语言响应时宽松+repair 均失败，仍 `circuit_breaking=True`。

**未采纳**：模型名 `deepseek-v4-flash` 经使用者确认合法，不做配置层模型名校验。

### 🔴 D8：preflight 提示词漏声明必填字段（F3 后暴露的真根因）

**F3 生效后错误信息变化**：`"必须直接返回单一严格 JSON 对象"` → `"AI repair 结果不符合目标严格协议"`，说明已成功进入 repair 通道。但 metadata 显示 `protocol_relaxed: false` + `classification: schema_invalid` —— **模型返回的是完美合法的纯 JSON，宽松解析根本没被触发**。问题从来不是 JSON 格式。

**实测证据**（用 `.env` 密钥直连 `api.deepseek.com` + `prompts/preflight/1.0.0` 原文）：

```
HTTP 200，返回内容（142 字符，纯 JSON 无围栏）：
{"analysis_complete": true, "message": "预检完成：规范 JSON 已读取，..."}

PARSED KEYS:       ['analysis_complete', 'message']
EXTRA FIELDS:      none
MISSING REQUIRED:  ['ok']      ← 根因
PYDANTIC ERROR:    loc=('ok',) type=missing msg=Field required
```

**根因**：`prompts/preflight/1.0.0/system.md` 第 7 行只写"不得省略 **analysis_complete**"，**全文 0 次提及 `ok`**（`grep -c "ok"` = 0）。模型精确遵守了它看到的规则——省略的恰好是唯一没被点名的必填字段。这不是模型能力缺陷，是提示词缺陷。

**为什么 repair 也修不回来（且这是正确行为）**：`prompts/repair/1.0.1/system.md` 明确要求"不得猜测缺失的必填裁决"。`ok` 是布尔裁决字段，repair 拒绝编造它完全正确。整条链上唯一的 bug 就在 preflight 提示词。

### D8 修复（已实施 2026-08-06）✅

| # | 改动 | 说明 |
|---|---|---|
| 1 | 新建 `prompts/preflight/1.0.1/system.md` | 新增"输出契约"小节，逐字段声明 `ok`/`message`/`analysis_complete` 类型与必填性 + `acknowledged_capabilities` 可选；补"三个必填字段一个都不得省略，缺任一即视为预检失败" |
| 2 | `prompts/preflight/1.0.1/user.md` | 与 1.0.0 相同（不可信输入声明保持不变） |
| 3 | `prompts/registry.yaml` | 新增 1.0.1 条目（1.0.0 保留共存，无隐式 fallback），`template_sha256.system=4286e043...` |
| 4 | `backend/app/analysis/ai.py:45` | `_PROMPT_VERSIONS["preflight"]` → `1.0.1` |
| 5 | `ai.py:625` | 校验失败时把 `validation_errors[:16]` 写入 `metadata["initial_validation_errors"]`——本次排查时 metadata 完全没记录失败原因，只能靠直连 API 才定位到，此后可直接从 manifest 读出 |

**设计取舍**：曾在提示词末尾放合规输出示例，实测模型会**逐字照抄示例 message 文案**。已改为只声明"键集合必须恰好覆盖三个必填键，message 内容由你根据实际情况撰写"——避免示例污染输出语义。

**真实 API 验证**（同端点同模型，改前 vs 改后）：

| 版本 | 返回 | Pydantic |
|---|---|---|
| 1.0.0 | 缺 `ok` | ❌ `loc=('ok',) missing` |
| 1.0.1（带示例） | 三字段齐全，但 message 照抄示例 | ✅ |
| **1.0.1（最终）** | 三字段齐全 + 自主 message + 主动填 `acknowledged_capabilities` | ✅ **PYDANTIC OK** |

**回归**：`scripts/check-all.sh` → **468 passed**（净增 1）+ 18 条规则契约 + 前端构建 + `sync-ai-protocol.py --check` 无漂移。

**新增回归测试**（已在 D9 扩展为全阶段参数化）：从各阶段 output schema 读取 `required` 列表，逐项断言提示词中出现该字段名。**这类缺陷会随 schema 演进复现**——以后往 `required` 加字段但忘记同步提示词，测试直接拦住。

---

## D4 第二次重跑验证结果（run 20260806T151747Z）

**状态**：⚠️ **部分成立** —— preflight 已通过、决策字段已落盘，但 AI 分析仍全部失败（同类缺陷换了阶段）。

### 实测数据（基线同一 APK sha256 `1c55d3fb9f95`，可对比）

| 阶段 | 状态 | 耗时(s) |
|---|---|---|
| basic_check | completed | 0.0 |
| decompiling | partial | 35.9 |
| rule_prescan | partial | **212.2** |
| candidate_funnel | completed | 0.0 |
| code_slicing | completed | 4.6 |
| ai_analysis | partial | **70.6** |
| evidence_integrity_validation | completed | 3.8 |
| aggregation | completed | 0.1 |
| **总计** | | **327.2** |

AI 阶段统计：`circuit_open=false`、`analyzed=7`、`peak_concurrent=7`、**`failed=7`**、`completed=0`。

### ✅ 已验证成立的三项

1. **D8 修复生效**：preflight `status=passed`、`prompt_version=1.0.1`、**`repair_attempts=0`**（一次通过，未走 repair）、`protocol_relaxed=false`。
2. **D1 悬案彻底解除**：finding 中 `evidence_decision`、`review_state`、`false_positive_basis`、`decision_reason_codes` **全部落盘**，证实此前"全为 None"纯属产物早于代码的统计口径问题。
3. **D2 定级链路确认正常**：`evidence_level` L1=128 → `severity` informational=128；L2=7 → pending=7，与 `severity.py:31-33` 判据完全吻合。

### ⚠️ 未能验证的两项

- **F1 并发效果无法评估**：只有 7 个候选进入 AI（`peak_concurrent=7` < 配置的 12），并发上限未被触及。
- **F2 效果无法评估**：候选全部在首轮即 `ai_failed`，未进入扩片阶段，`CONTEXT_EXPANSION_STALLED` 自然为 0（非修复生效）。

### 附带发现：候选数从 242 降到 149

`rule_prescan` 出现新的规则失败：`ACTIVITY_INTENT_TO_SENSITIVE_SINK` → **`RULE_OUTPUT_LIMIT`（规则输出超过限制）**，critical=true，耗时 4822ms。该规则整族候选丢失，需单独排查（记为 D10）。

### 🔴 D9：l2-review / finalization 提示词漏声明必填字段（与 D8 同源）

**7 个候选全部同一失败原因**：`classification=schema_invalid`，message `"AI repair 结果不符合目标严格协议"`，`prompt_id=l2-review`、`prompt_version=2.0.1`、`repair_attempts=1`、`protocol_relaxed=false`。

**静态核对（决定性证据）**——统计各阶段 system.md 中 required 字段出现次数：

| 阶段 | 版本 | required 数 | 未声明字段 |
|---|---|---|---|
| l1-triage | 2.0.1 | 3 | 无 |
| **l2-review** | **2.0.1** | **5** | **`summary`、`confidence_tier`（各 0 次出现）** |
| **finalization** | **1.0.1** | **4** | **`summary`（0 次出现）** |

`prompts/l2-review/2.0.1/system.md` 通篇未提及 `summary` 与 `confidence_tier` —— 与 D8 完全同源，且影响更大：**l2-review 是主分析阶段，等于 AI 深度分析对所有候选 100% 失败**。

**这解释了此前"AI 精度贡献为 0"的观感**：不是 AI 没价值，是 l2-review 从未成功返回过一次合规输出。

### D9 修复（已实施 2026-08-06）✅

| # | 改动 | 说明 |
|---|---|---|
| 1 | 新建 `prompts/l2-review/2.0.2/system.md` | 在"输出必须严格符合 L2ReviewOutput"下补必填字段清单（5 个，含"一个都不得省略"），并逐条说明 `summary` 语义与 `confidence_tier` 枚举（含"证据不足或有关键 blocking_gaps 不得给 high"、"衡量证据可信度不是影响大小"） |
| 2 | 新建 `prompts/finalization/1.0.2/system.md` | 同样补 4 个必填字段清单与 `summary` 语义 |
| 3 | `prompts/registry.yaml` | 新增两条目（旧版本保留共存，无隐式 fallback），template_sha256 按新文件计算 |
| 4 | `backend/app/analysis/ai.py:44-50` | `_PROMPT_VERSIONS` → `l2-review: 2.0.2`、`finalization: 1.0.2` |

**回归**：`scripts/check-all.sh` → **474 passed**（原 468，净增 6）+ 18 条规则契约 + 前端构建 + sync 无漂移。

**测试升级**：原 preflight 专项测试改为**全阶段参数化**：
- `test_prompt_declares_every_required_output_field[preflight/l1-triage/l2-review/finalization]` —— 从各阶段 schema 的 `required` 反向断言；
- `test_prompt_states_no_required_field_may_be_omitted[preflight/l2-review/finalization]` —— 断言含显式"一个都不得省略"约束（仅列字段名不足以约束模型）。

**端到端验证**：遍历 `_PROMPT_VERSIONS` 实际加载模板，四阶段 `missing=none`。

### 🔴 D10：组件级数据流 trace 按候选复制导致规则输出爆炸

**现象**：`ACTIVITY_INTENT_TO_SENSITIVE_SINK` 失败于 `RULE_OUTPUT_LIMIT`（critical=true，4822ms），
Activity 族候选整体丢失，**候选数从基线 242 降至 149**。

**实测数据**（`rule-work/*/stdout.json` 体积对比）：

| 规则 | stdout 大小 |
|---|---|
| **ACTIVITY_INTENT_TO_SENSITIVE_SINK** | **90.52 MB** |
| DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION | 0.53 MB |
| RECEIVER_INPUT_TO_SINK | 0.44 MB |
| 其余 15 条规则 | < 0.06 MB |

**不是上限太小**（10 MiB 对其他规则富余 20 倍以上），是该规则输出比第二名大 170 倍。

**根因**（`rules/shared/detector.py:331`）：`common_metadata` 收集的是**组件级**全量数据流 trace，
却通过 `result.update(common_metadata)` 无差别附加到**每条链路候选**上：

```
候选数 140，去重后组件仅 7 个
com.xiaomi.shop.activity.MainTabActivity  → 78 条链
  → 同一份 921KB reaching_definitions 被复制 78 遍
组件级 trace 字段占总体积 98.0%（88.7 MB / 90.5 MB）
reaching_definitions 累计条目 532,843
单候选平均 678 KB，最大 1.08 MB（首个候选仅 6.4 KB，属特例）
```

**下游消费核查（决定可安全裁剪）**：`reaching_definitions`、`method_summaries`、
`validation_transitions`、`slot_overwrites`、`summary_fixpoint`、`final_reaching_state`
在 backend **零引用**；`router_validation_bypass` 仅 2 处，且都只做真值判断
（`candidate_funnel.py:676`），不读内容。全部字段均不在 `candidate.schema.json` 的
`properties` 中（靠 `additionalProperties: true` 通过校验）。

### D10 修复（已实施 2026-08-06）✅

**思路**：组件级 trace 摘要化后再随候选下发，逐条明细留在规则进程内。数据流分析本身的
精度不受影响——摘要发生在候选组装阶段，不改 `dataflow.py` 的分析逻辑。

| # | 改动 | 说明 |
|---|---|---|
| 1 | 新增 `_summarize_reaching_definitions()` | 压缩为 `{total, values(去重值数), killed(kill 次数), states(state 分布), samples(≤20)}`，保留覆盖判断与 strong update/kill 语义 |
| 2 | 新增 `_summarize_method_summaries()` | 只留 `{total, methods(≤200 键名), methods_truncated}`，摘要体不外发 |
| 3 | 新增 `_cap_records()`（`_TRACE_RECORD_CAP=200`） | 截断 `validation_transitions`/`slot_overwrites`/`router_validation_bypass`，并**追加显式截断标记**（`trace_truncated`/`total_records`/`retained_records`）——静默截断比报错更危险 |
| 4 | `detector.py:334-344` | `common_metadata` 改为调用上述三个摘要器 |

**真实数据验证**（直接对上次 run 的 90MB 产物套用新摘要器）：

```
候选数 140
修复前:  94,905,153 bytes  (90.5 MB)
修复后:   3,890,662 bytes  ( 3.71 MB)
压缩比:  24.4x     降幅 95.90%
10 MiB 上限: 通过
```

**回归**：`scripts/check-all.sh` → **479 passed**（原 474，净增 5）+ 18 条规则契约 + 前端构建。

**新增测试** `backend/tests/test_rule_output_budget.py`（5 条）：
- 摘要体积与输入条目数解耦（6 万条输入下摘要 < 8 KB，且不随输入线性增长）；
- 压缩保留判定语义（总数 / 去重值数 / kill 次数 / state 分布逐项断言）；
- 截断必须显式标注，未超限时不得插入标记；
- 方法摘要体被剔除但规模保留；
- 脏数据容错（`None`/字符串/混合类型不抛异常）——规则跑在子进程且输入来自反编译产物。

**附带修复**：批量缩进归一化时误改了 detector.py 中 27 处 `if ...: continue` 单语句体的缩进，
已逐行复核并按"上一行条件缩进 +4"修正，27 处全部核对为 `if/elif` 单语句体（含一处多行条件），
语义未改变，474 → 479 测试全绿佐证。

## D14：manifest 上下文携带 0 行号——模型照抄导致行号校验失败（run 20260808T045452Z）

**现象**：D13 修复后第六次重跑（基线 APK `1c55d3fb9f95`），AI 完成率从 8 提升到 **59/147**（40%），
但仍有 **81 个失败，全部同一原因**：`evidence_refs.N.line/end_line: greater_than_equal`（Pydantic ge=1）。

**关键洞察（D13 修的 claim/blocking_gaps 已消失，只剩行号）**：这不是模型问题——
`_build_manifest_context`（`context_builder.py:884-885`）硬编码 `start_line: 0`/`end_line: 0`。
AndroidManifest.xml 是 XML **没有代码行号**，模型忠实照抄输入的 0 → 违反 `minimum: 1`。
模型"做对了"，是数据给了它非法行号。

**风险评估（改 start_line 0→None 的安全性）**：
- `ai.py:1406 int(context["start_line"])`：有 try/except(KeyError/TypeError/ValueError) 保护，None 不崩（该校验返回 False）；
- `context_budget.py:242 int(start_line)`：仅在超长 context 裁剪时执行，manifest context 只有几行不会走到；
- `context_budget.py:311 int(start_line or 0)`：`or 0` 兼容 None；
- `context_builder.py:267` 排序：只作用于 file methods，manifest context 不参与。

**修复（数据层治本 + 提示词双保险）**：
1. **数据层**：`context_builder.py:_build_manifest_context` → `start_line: None`/`end_line: None`（null 语义 = "整个上下文"）。
   模型会照抄 null → schema 允许 → 治本；附注释说明 0 导致 schema_invalid 的事故。
2. **提示词 2.0.4**：新增 line/end_line 取值规则——被引用上下文无代码行号（kind=manifest_component，start_line/end_line 为 null）
   时输出 **null**；仅代码文件上下文输出 >=1 真实行号；任何情况不得输出 0 或负数。
3. registry 注册 2.0.4（旧版本共存）；`ai.py:_PROMPT_VERSIONS` → 2.0.4；测试版本号同步。
4. 新增 `tests/test_context_builder_manifest.py`（3 条）：manifest context 行号必须为 None、仍携带组件事实、缺组件时返回 None。

**验证**：
- 真实 API 端到端（上一失败候选输入 + 生产路径）：manifest context start_line=None；
  模型引用代码上下文行号 127/79/104（全部合法）→ **HTTP 200 + PYDANTIC OK**；
- `scripts/check-all.sh` → **490 passed**（原 487，净增 3）+ 18 条规则契约 + 前端构建。

**教训**：① 模型是"忠实反射镜"——给 0 行号就输出 0，给 null 就输出 null；**数据层的语义正确性是 AI 输出的前置条件**；
② 之前误判"模型不遵守提示词"，实际是输入数据引导它犯错；③ 无行号资源（XML/manifest）用 null 而非 0 是通用原则。

## D13：l2-review 提示词未声明嵌套结构约束（run 20260808T043831Z）

**现象**：D12 修复后第五次重跑（基线 APK `1c55d3fb9f95`），preflight 通过（prompt 1.0.1、http 200），
但 AI 完成率仍低：**completed 8 / failed 132 / incomplete 7**（峰值并发 12）。

**与 D11 的对比**：`empty_initial_content = 0` —— 空 content 问题彻底消失（thinking 关闭 + max_tokens 8000 + model 修复全部生效）。
失败原因变为**真实的 schema 嵌套校验错误**，诊断字段（`initial_validation_errors`）直接给出：

| 数量 | 错误 | 根因 |
|---|---|---|
| 62 | `evidence_refs.N.claim: missing` | EvidenceReference 的 required 字段 `claim`，提示词未声明 |
| 61 | `evidence_refs.N.line/end_line: greater_than_equal` | Pydantic `ge=1` 拒绝 —— **模型输出 0 或负行号**（程序员 0-based 习惯） |
| 8 | `blocking_gaps.N: model_type` | BlockingGap 元素结构问题 |
| 1 | `evidence_refs.N.text: extra_forbidden` | `additionalProperties: false` 拒绝协议外字段 |

**根因（第三次同源复发，层级逐层深入）**：
- D8：preflight 顶层必填字段未声明（漏 `ok`）；
- D9：l2-review/finalization 顶层必填字段未声明（漏 `summary`/`confidence_tier`）；
- **D13：l2-review 嵌套结构字段未声明** —— EvidenceReference（required: context_id+claim，line/end_line 必须 >=1，禁止额外字段）与
  BlockingGap（required: code+message+critical）的约束从未写入提示词。模型不知道 evidence_refs 元素必须含 claim、
  行号必须 >=1、不得加 text 等字段。
- **repair 也失败的原因**：repair 收到的 invalid_output 本身就缺 claim/行号非法，repair 提示词只被要求"格式修复不补造事实"，
  无法把缺的 claim 补回来（且 repair 输出同样受嵌套约束）。

**修复（l2-review 2.0.3）**：
1. `prompts/l2-review/2.0.3/system.md` 新增两条：
   - `evidence_refs` 元素结构：claim（必填）/context_id（必填）/path（可空）/line 与 end_line（整数或 null，**行号必须 >=1，不得为 0 或负数**）；禁止 claim/context_id/path/line/end_line 之外字段；
   - `blocking_gaps` 元素结构：code（必填）/message（必填）/critical（必填）；禁止协议外字段。
2. `prompts/registry.yaml` 注册 2.0.3（2.0.1/2.0.2 保留共存）；`ai.py:_PROMPT_VERSIONS` → 2.0.3。
3. 测试扩展：`test_prompt_registry.py` 版本号同步 2.0.3；新增
   `test_l2_review_prompt_declares_nested_evidence_reference_constraints` —— 从 schema `$defs` 读取
   EvidenceReference/BlockingGap 的 required 逐一断言，并检查"不得为 0 或负数"与"不得添加"约束存在。

**验证**：
- 真实 API 端到端（用上一个失败候选的输入重建请求 + `_chat_payload` 生产路径）：
  **HTTP 200 + PYDANTIC OK**；evidence_refs 缺 claim 0 / 行号<1 0 / 协议外字段 0；输出质量高（unresolved 裁决 + 规范引用）；
- `scripts/check-all.sh` → **487 passed**（原 486，净增 1）+ 18 条规则契约 + 前端构建。

**教训**：提示词约束不足已三次复发（顶层→顶层→嵌套），每次都是"schema 的 required/约束未写入提示词"。
参数化测试只覆盖顶层 required，本次新增嵌套 $defs 检查。**后续新增 AI 输出 schema 嵌套结构时，
必须同步：① system.md 声明嵌套字段与约束；② 测试从 $defs 读取断言；③ 行号/枚举/pattern 类硬约束必须显式写入提示词**
（0-based 行号是模型的常见错误，需要像"不得为 0 或负数"这样的显式约束）。

## D12：D11 补丁引入回归——`_chat_payload` 丢失 `model` 字段（run 20260808T042228Z）

**现象**：充值后第四次重跑（基线 APK `1c55d3fb9f95`），preflight 立即失败：
`request_incompatible · AI 请求与服务能力不兼容`，`http_status=400`，`initial_latency_ms=200`（200ms 即被拒）。

**排查过程**：
1. `_classify_http_error` 确认 400/422 → `request_incompatible`；
2. 用 `.env` 密钥直连 API：手写 body（含 model）→ **HTTP 200**；带 `thinking` 参数也 200 —— 排除 thinking 参数与端点兼容性问题；
3. 用当前代码的 `_chat_payload()` 重建请求 → **HTTP 400 复现**，真实错误体：
   `Failed to deserialize the JSON body into the target type: missing field 'model' at line 1 column 1937`；
4. 对比 payload keys：`['max_tokens','messages','response_format','temperature','thinking']` —— **`model` 字段缺失**。

**根因**：D11 补丁（`backend/app/analysis/ai.py` 的 `_chat_payload` 改造）在替换函数体时**把 `"model": model,` 那行弄丢了**（补回 `payload: dict[str, Any] = {` 时漏了它）。此前 probe 返回 200 是因为 probe 手写 body 包含 model；run 走 `_chat_payload` 则 400。
**测试盲区**：`test_ai_thinking.py` 4 条用例只断言 thinking/response_format/temperature/max_tokens，**从未断言 model** —— 字段丢失测试全绿。

**修复**：
1. `ai.py:1248` 补回 `"model": model,`；
2. `test_ai_thinking.py` 扩展为 7 条：原有 4 条全部加 `assert payload["model"] == "test-model"`，新增参数化用例 `test_chat_payload_always_contains_required_fields`（3 组参数 × model/messages/response_format 断言）—— 锁死"请求体必须始终包含 model 与 messages"。

**验证**：
- 真实 API 端到端：payload 含 model → **HTTP 200** + 合规 JSON；
- `scripts/check-all.sh` → **486 passed**（原 483，净增 3）+ 18 条规则契约 + 前端构建。

**教训**：① 补丁类改动必须用"字段完整性"断言兜底——结构体的关键字段（model/messages）比行为字段（thinking）更重要；② 真实 API 复现（`_chat_payload` 重建请求）一锤定音，手写 body 的 probe 会掩盖字段丢失类 bug；③ run 的 metadata 缺 `preflight_strict_protocol` 字段本就是旧代码信号，但当时未警觉——**metadata 出现"应有字段缺失"时应直接怀疑代码版本不一致**。

## D4 第三次重跑验证结果（run 20260806T155116Z）

**状态**：⚠️ **两项修复确认生效，但 AI 完成率暴露新根因**。

### 实测数据（基线同一 APK sha256 `1c55d3fb9f95`）

| 阶段 | 状态 | 说明 |
|---|---|---|
| rule_prescan | partial | `rule_failures: []`，**候选 289**（基线 242，D10 修复后 Activity 族回归） |
| ai_analysis | partial | **analyzed 147 / completed 2 / failed 138 / incomplete 7**，`peak_concurrent=12`（F1 配置生效） |
| aggregation | completed | finding 275 |

### ✅ 已确认成立

1. **D10 修复生效**：规则失败清零（`rule_failures: []`），候选 289 超过基线 242。
2. **F1 并发配置生效**：`peak_concurrent=12` = 配置值，并发上限首次被真实触及。
3. **D9 修复部分生效**：2 个候选 `ai_completed`，l2-review 2.0.2 能产出**高质量合规输出**（详实的 unresolved 推理、正确的 confidence_tier=low、空 evidence_refs）。

### 🔴 D11：deepseek 思维模式默认开启 → content 为空

**失败面**：138 个 `ai_failed` 中 **131 个初始响应为空字符串**（`initial_response_hash` = SHA256("") = `e3b0c442...`），HTTP 200 但 content 为空；repair 也失败（repair 有非空响应但仍 schema_invalid）。剩余 7 个非空但解析失败（context_requests 字段问题）。

**排除的假设**：
- ❌ 输入大小：成功样本输入 7191 tokens vs 失败 7149 tokens，几乎相同；
- ❌ 截断（max_tokens=3000）：成功样本同样 3000，且无 finish_reason=length 记录；
- ❌ 并发高峰：失败**均匀分布**（15:56-16:13 每分钟 12-15 个），非集中爆发。

**根因（WebSearch 确证 deepseek-v4-flash 官方行为）**：
- **thinking 模式默认开启**："Thinking defaults to enabled, so set the toggle explicitly when reproducibility matters"；
- JSON 模式响应含 `reasoning_content` 字段，最终答案在 `content`；
- **DeepSeek 官方承认 JSON 模式下 content 偶发为空**，建议显式关 thinking + 有界恢复策略；
- 31 秒中位延迟 = 模型在"思考"；`max_tokens=3000` 被推理 token 耗尽 → `content` 为空 → 宽松解析也救不回（空串无 JSON 可提取）。

**补充事实**：probe 直连 API 时发现**当前余额不足**（HTTP 402 Insufficient Balance）——重跑前需充值。

### D11 修复（已实施 2026-08-07）✅

| # | 改动 | 说明 |
|---|---|---|
| 1 | `AISettings` 新增 `disable_thinking`（默认 true）/ `thinking_param`（默认 "thinking"）/ `max_output_tokens`（兜底，默认 None） | `backend/app/config.py` |
| 2 | `_chat_payload` 支持 `disable_thinking`，开启时发送 `{"thinking": {"type": "disabled"}}` | `ai.py:1214-1244` |
| 3 | 两个调用点传 `disable_thinking`/`thinking_param`（从 settings 读取，兼容其他端点） | `ai.py:471-478, 738-745` |
| 4 | `configure_budget_identity` 用 `settings.max_output_tokens` 兜底 budget 值 | `ai.py` |
| 5 | **空 content 诊断**：`initial_response_hash` 计算后若 content 为空，记录 `empty_initial_content`/`finish_reason`/`completion_tokens`/`reasoning_tokens` 到 metadata——下次遇到可直接从 manifest 读出证据，不必再 probe | `ai.py:594-609` |
| 6 | `context_budget.max_output_tokens` 3000 → **8000**（V4 支持至 384000） | `config/default.yaml:72` |
| 7 | `config/default.yaml` 补 `disable_thinking`/`thinking_param`/`max_output_tokens` 配置与注释 | `config/default.yaml:132-139` |

**验证**：`scripts/check-all.sh` → **483 passed**（原 479，净增 4）+ 18 条规则契约 + 前端构建。

**新增测试** `backend/tests/test_ai_thinking.py`（4 条）：thinking 参数随开关出现/消失、参数名可配置（reasoning_effort）、max_tokens 透传。

**下一步前置条件**：① deepseek 账户充值；② 用基线 APK（`1c55d3fb9f95`）第四次重跑；③ 核对 AI 完成率（预期大幅提升）、`reasoning_tokens` 诊断字段、F2 平台事实对 CONTEXT_EXPANSION_STALLED 的影响。

## 排查清单更新（第二轮）

| # | 优先级 | 方向 | 状态 |
|---|---|---|---|
| D9 | **P0** | **l2-review/finalization 漏声明必填字段** | ✅ 已修复（2026-08-06） |
| D10 | **P0** | `ACTIVITY_INTENT_TO_SENSITIVE_SINK` 触发 `RULE_OUTPUT_LIMIT`，候选 242→149 | ✅ 已修复（2026-08-06） |
| D11 | **P0** | **deepseek 思维模式默认开启 → content 为空 → AI 完成率 2/147** | ✅ 已修复（2026-08-07） |
| D12 | **P0** | **D11 补丁丢失 `_chat_payload` 的 `model` 字段 → HTTP 400 → preflight 熔断** | ✅ 已修复（2026-08-08） |
| D13 | **P0** | **l2-review 提示词未声明嵌套结构（EvidenceReference/BlockingGap）约束 → 132/147 schema_invalid** | ✅ 已修复（2026-08-08） |
| D14 | **P0** | **manifest 上下文携带 0 行号 → 模型照抄 → line>=1 校验失败（81/147）** | ✅ 已修复（2026-08-08） |
| D4 | P0 | 第四次重跑：验证 F1 并发 + F2 平台事实 + AI 真实贡献 | ⬜ 未开始（D11 已解除阻塞，需先充值） |

### D4 重跑的正确前置条件

1. ~~先修 D7~~ ✅ 已完成（见上节），preflight 默认不再因围栏/包裹文本熔断；
2. 使用**与基线相同的 APK**（sha256 `1c55d3fb9f95...`）；
3. 确认 `AI_APK_TRACER_OPENAI_API_KEY` 在运行环境已设置；
4. 重跑后核对三项指标：决策字段落盘、加速比（对照 26188s）、`CONTEXT_EXPANSION_STALLED` 数量（对照 44/44）。

## 排查清单更新

| # | 优先级 | 方向 | 状态 |
|---|---|---|---|
| D7 | **P0** | **preflight 无 repair 兜底导致 AI 全量熔断** | ✅ 已修复（2026-08-06） |
| D8 | **P0** | **preflight 提示词漏声明必填字段 `ok`** | ✅ 已修复（2026-08-06） |
| D4 | P0 | 用相同 APK 重跑并评估 F1/F2 效果 | ⬜ 未开始（D7 已解除阻塞） |

---

## D1（P0）evidence_decision 全为 None

**状态**：✅ 已完成
**结论**：**决策链路正常工作，字段缺失是代码版本差异，不是 bug。上一轮"AI 无贡献"的推断被推翻。**

### 排查过程与证据

1. **DecisionEngine 确实被调用**：`orchestrator.py:251` → `DecisionEngine().apply(verified)`，`apply()` 内部对每个候选调用 `decide()`（`decision.py:529-533`）。
2. **aggregate 确实写入决策字段**：`aggregate.py:71-72` 明确写入 `evidence_decision` 与 `false_positive_basis`，来源为 `aggregate_review_states()`（`review_state.py:47-75`）。
3. **写盘无过滤**：`orchestrator.py:272-273` 是整对象 `json.dumps`，不存在字段白名单裁剪。
4. **实测产物**：`evidence_decision` / `review_state` / `decision_reason_codes` 三个字段在 136 个 finding 中**全部缺失**，但 `review_status` **有完整分布**。
5. **时间线比对**：run 跑于 8月2日，`aggregate.py` 等决策模块改于 8月4日——**产物早于代码**。

### 关键纠正

`review_status` 分布证明决策逻辑当时就在工作：

```
pending_ai 123 / ai_false_positive 5 / pending_manual 4 / manual_false_positive 3 / confirmed 1
```

其中 **`ai_false_positive` = 5**（AI 判误报且有确定性反证背书）、**`confirmed` = 1**。上一轮我统计 `evidence_decision` 字段得出"AI 贡献为 0"，实际是**该字段当时还没被写入产物**，属于统计口径错误，而非 AI 真的没有贡献。

### 待办

无需修复代码。需用当前代码重跑一次验证字段是否已正常落盘（见 D4）。

---

## D2（P0）severity 全部 informational/pending

**状态**：✅ 已完成
**结论**：**定级链路正常，分布符合判据设计；但暴露 L2 晋级率偏低的真问题。**

### 证据

`severity.py:31-33` 第二道判据：

```python
evidence_level = candidate.get("evidence_level", "L1")
if evidence_level == "L1":
    return "informational", ["L1 仅确认攻击面或配置事实，不代表漏洞链成立"]
```

实测分布完全吻合：

| evidence_level | 数量 | severity |
|---|---|---|
| L1 | 121 | informational 121 |
| L2 | 15 | pending 15 |

即：121 个 L1 按设计判 informational；15 个 L2 全部因 critical gap 落到 pending（`_has_critical_gap` 命中）。**定级函数工作完全正常。**

### 真问题：L2 候选全部卡在 pending

15 个 L2 候选无一晋级到 critical/high/medium/low，全部因 critical gap 停在 pending。结合 D3 可知，这些 gap 主要是 `CONTEXT_EXPANSION_STALLED`（AI 分析未完成）与 `JADX_PARTIAL_DECOMPILATION`。

**这才是"效果不如预期"的真实位置**：不是 AI 没用，而是**证据链因上下文缺失无法闭合，导致所有 L2 候选卡在待定状态**。

---

## D3（P0）CONTEXT_EXPANSION_STALLED 根因

**状态**：✅ 已完成
**结论**：**主因不是 JADX 反编译失败，而是 AI 扩片请求与索引能力不匹配。修正上一轮判断。**

### 证据

1. JADX 虽 exit 3（181 错误），但产出 **24908 个源文件**；索引 `classes` 表 24152 行、`methods` 表 232683 行，**索引本身健康**；
2. 44 个停滞候选的扩片请求分布：

| 请求类型 | 次数 |
|---|---|
| class | 53 |
| method | 35 |
| file_symbols | 32 |
| callers | 17 |
| component | 11 |
| callees | 6 |

3. 抽样 trace 显示 AI 实际在请求：
   - `targetSdkVersion` —— **Manifest 配置项，根本不在代码索引中**（请求类型错配）
   - `C6585c$a`、`InterfaceC7771by.a` —— 混淆内部类/接口，索引中命名形式不匹配
   - 动态 Receiver 的 `onReceive` 实现 —— 跨类解析，受符号歧义策略影响

### 三类根因（按可修复性排序）

| # | 根因 | 修复难度 | 建议 |
|---|---|---|---|
| 1 | **请求类型错配**：AI 想要 Manifest 事实但只有代码扩片通道 | 易 | 在初始切片中直接提供 Manifest 摘要（targetSdk/minSdk/debuggable/组件导出表），避免 AI 浪费轮次索要拿不到的东西 |
| 2 | **符号歧义不连边**：`context_builder.py:392-396` 采用"同类唯一 or 全局唯一才连边"，混淆代码下大量调用点被丢弃 | 中 | 输出"候选目标列表 + 歧义标记"替代直接不连边 |
| 3 | **混淆内部类解析**：`C6585c$a` 需名称规范化映射 | 较难 | 建立内部类名映射表 |

### 修正上一轮判断

之前称"真因是 JADX exit 3 导致索引缺类"**不准确**。索引有 24152 个类可用，AI 要不到的主要是"索引里没有这个概念"（Manifest 配置）和"存在但解析不到"（歧义/内部类），而非"文件没反编译出来"。

**根因 1 的修复性价比最高**：只需在切片初始上下文补 Manifest 摘要，即可消除一部分无效扩片轮次，同时降低 AI 耗时。

---

## D4（P0）用当前代码重跑并重新评估 AI 价值

**状态**：⬜ 未开始 —— **阻塞所有价值判断**

**必要性**：现有 run 产物早于决策代码修改（见文首前提修正），基于其得出的任何"AI 有用/无用"结论都不可信。

**执行方案**：
1. 用当前代码对同一 APK（com.mi.health）跑一次完整扫描；
2. 确认 `evidence_decision` / `review_state` / `false_positive_basis` 已正常落盘；
3. 统计 `ai_false_positive`、`confirmed`、`deterministically_refuted` 的真实分布；
4. 与 rule_only 模式（`source_analysis.enabled=false` 或 AI 关闭）做对照，量化 AI 独有贡献。

**度量口径**：AI 独有贡献 = AI-on 与 AI-off 两次 run 在 `evidence_decision` 与 `severity` 分布上的差异，而非单看某个字段是否为 0。

---

## D5（P1）单候选 115s 的构成

**状态**：✅ 已完成
**结论**：**并发度配置过低是主因，存在 4~8 倍的压缩空间，且不损失分析质量。**

### 实测配置（`config/default.yaml`）

| 参数 | 当前值 | 上限 | 说明 |
|---|---|---|---|
| `candidate_concurrency` | **4** | 32 | 单次扫描并发分析候选数 |
| `max_concurrent` | **6** | 64 | 进程级 AI HTTP 并发 |
| `provider_max_in_flight` | **4** | — | 同 provider 在途请求数（实际瓶颈） |
| `timeout_seconds` | 120 | — | 单次请求超时 |
| `retry_count` | 1 | — | 重试次数 |

### 耗时反推

- 228 个候选 / 并发 4 = **57 批**；
- 26188s / 57 批 ≈ **459s/批**；
- 每候选平均 2.3 轮（轮次分布 1~5），即每轮 LLM 调用约 **200s**——**接近 `timeout_seconds=120` 的两倍，说明存在超时重试**（`retry_count=1`，一次超时+一次重试≈240s）。

### ai-cache 命中率

`ai-cache/` 目录仅 **1 个条目**（228 个候选）→ **命中率约 0.4%，形同虚设**。且配置注释明确"不会跨 run 共享"，重跑同一 APK 无法复用。

### 三项可立即验证的优化

| # | 措施 | 预期收益 | 风险 |
|---|---|---|---|
| 1 | `candidate_concurrency` 4 → 12~16，`provider_max_in_flight` 4 → 8~12 | **3~4 倍加速**（7.3h → ~2h） | 需确认 provider 限流阈值，429 会触发冷却 |
| 2 | 排查 200s/轮的超时重试——降低单请求上下文体积或提高 timeout 合理性 | 减少无效等待 | 需确认是模型慢还是上下文过大 |
| 3 | 开启跨 run 缓存（按 slice 内容 hash） | 重复扫描同一 APK 时近乎瞬时 | 需保证 prompt 版本变更时缓存失效 |

**措施 1 是最快见效的**：仅改配置，无代码改动，可立即用一次重跑验证（与 D4 合并执行）。

### 与 D4 的关系

D4 重跑时应同时应用措施 1，一次 run 同时验证"决策字段落盘"与"并发提速"两件事。

---

## D6（P2）候选收敛合理性

**状态**：⬜ 未开始

排查方向：242 候选 → 136 finding 的 scope_key/chain_key 合并逻辑是否丢失有效候选。
