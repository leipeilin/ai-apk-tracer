# 对《AI-APK-Tracer 项目优化方案：确定性可信判定 × Agent 化批量探索》的审核意见

> **审核对象**：`docs/analysis/2026-08-18-project-optimization-plan.md`（2026-08-18 修改版，下文称"方案"）
> **审核日期**：2026-08-18
> **审核范围**：方案与用户构想（包资产批量扫描 + API 入口表 + API 接口调用树 + 攻击面列表 + Agent1 探索跟踪）的贴合度；方案接入当前项目代码的合理性与落地风险。
> **方法**：方案全文逐节复核；与现有流水线代码锚点核对（`orchestrator.py` / `candidate_funnel.py` / `candidate.schema.json` / `index_store.py` / `detector.py` / `config/default.yaml`）；对照 `2026-08-16-vulnerability-discovery-success-plan.md` 的实证结论（动态终审 8 项成立漏洞、S1–S11）交叉验证。
> **总体表态**：**方向认可，可进入 Phase 0 落地准备**。方案已覆盖用户构想的主体（API 入口表、call_tree、攻击面、Agent1 检索循环、三档校验均已纳入），且保留了项目"确定性核心为最终权威"的基因。但存在 **1 处与用户需求直接冲突**（Agent1"是否构成漏洞、漏洞描述" vs 低信任边界）、**3 处会导致验收落空或无法校验的关键缺口**（hops 结构缺失、检索循环状态机缺失、候选归一化工作量被低估），以及若干需补定义/补闭环的次要问题。
> **增补记录（2026-08-21）**：完成 §4 全部代码锚点逐项复核（14 项断言：10 项属实、3 项部分属实、1 项为新模块未实施的预期状态，锚点总体可信度高）；新增 §4.11–§4.13 三项缺口；对全部问题加状态标记（✅ 已解决 / ⚠️ 待决断），状态总览见 §4.0，待决断项集中在 §7。
> **决断记录（2026-08-21）**：用户已拍板——§7.1 采用选项 1（新增 `explorer_deep_dive` 协议）；§7.2 采用方案 B（规则运行时输出产物 JSON，`api_surface` 读产物，backend → rules 保持零依赖）。§4.4 / §4.11 已关闭，§4 全部 13 项问题均为"已解决"；修订点已合入方案正文（2026-08-21 修订版），实施计划与验收标准见 `2026-08-21-explorer-track-implementation-plan.md`。

---

## 1. 背景与审核前提

### 1.1 项目现状

AI-APK-Tracer 是"规则先行、AI 受限观测、确定性终裁"的本地单用户 APK 静态分析工具：

```text
APK → decompile/index → rule_prescan → candidate_funnel → code_slicing
     → ai_analysis（strict observation）→ evidence validation + decision → aggregation → review
```

关键事实（审核核对过的代码锚点）：

- 候选**全部**由规则产生（`backend/app/analysis/rule_runner.py`），AI 只能看到规则候选及其切片；
- `l1_skip_ai=true` 后 L1 informational 默认不进 AI（`config/default.yaml` funnel 段）；
- 候选必须符合 `schemas/candidate.schema.json`（强制 `rule_id` / `rule_version` / `component` / `sources` / `sinks` / `blocking_gaps`）；
- funnel identity 计算已排除 `rule_id` 等字段（`backend/app/analysis/candidate_funnel.py` `_PIPELINE_IDENTITY_EXCLUDED_FIELDS`），为探索候选以固定 `rule_id` 并入预留了兼容空间；
- 8-16 成功方案实证：8 项动态终审成立漏洞机器闭合 0/8，4 项藏在 L1，根因是确定性层漏检（FM-1~FM-4），并已给出 S1–S11 确定性补强方案。

### 1.2 用户设计构想（审核基准）

用户在 2026-08-18 提出的部分构想（下文逐条对照）：

1. 包资产批量扫描（package list → 批量 run → 资产级汇总）；
2. 资产扫描层：资产注册表 → 工具化 decompile 得到反编译源码目录 → 基于源码生成对外暴露的 API 入口表 → 构建从入口 API 深挖的 API 接口调用树；
3. 工具化提取攻击面列表（`/attack_surface/com.xxx.example/activity.json`）；
4. Agent1 根据攻击面描述文件 + API 入口表 + API 接口调用树探索可利用攻击面，持续跟踪直到找到 sink，返回数据流，并描述组件/代码功能、是否构成漏洞、漏洞描述；
5. ……（后续待设计，方案以报告/PoC/评估闭环承接）。

---

## 2. 用户构想 vs 方案覆盖度对照

| 用户构想 | 方案对应设计 | 覆盖状态 |
|---|---|---|
| 包资产批量扫描 | Phase 1 Asset Layer（registry / batch / API / 前端） | ✅ |
| 资产注册表 | `backend/app/assets/registry.py`（sha256、来源、最近 run_id） | ✅ |
| 工具化 decompile → 反编译源码目录 | §2.0 复用 `run_dir/decompile/sources`，不另建 `/Source/...` | ✅ 等价且更优 |
| 对外暴露的 API 入口表 | §2.1 `api_entry_table.json`（manifest + Binder/AIDL + WebView bridge + Deep Link + Provider + 动态 Receiver） | ✅ |
| API 接口调用树（从入口深挖） | §2.2 `call_tree.py` on-demand 有界构建（深度 ≤ 8、节点 ≤ 500） | ✅ |
| 攻击面列表 | §2.3 `run_dir/attack_surface/{activity,service,provider,receiver}.json` | ✅ |
| Agent1 探索跟踪到 sink、返回数据流 | §2.4 受控检索循环（structured read/callers/callees → 本地取码 → 下一轮） | ⚠️ 循环状态机未定义（见 §4.3） |
| 描述组件/代码功能 | `component_summary` | ✅ |
| **是否构成漏洞、漏洞描述** | **§2.4 明确"禁止直接下'漏洞成立'结论"** | ❌ **直接冲突（见 §4.1）** |
| **返回数据流** | `chain_proposal.path` 仅为方法名字符串数组 | ⚠️ 太松，撑不起逐跳校验（见 §4.2） |
| ……（待设计） | Phase 3 报告/PoC 骨架、Phase 4 评估闭环 | ✅ 有延伸 |

---

## 3. 认可的部分（直接采纳）

| 条目 | 审核结论 |
|---|---|
| 双轨制（确定性主链 + 低信任探索轨） | ✅ 与项目核心原则一致：探索 Agent 只输出建议链 + 代码引用，最终裁决权保持在确定性层 |
| 复用现有反编译目录 | ✅ 避免 `/Source/com.xxx.example/source` 重复落盘与产物漂移，仅新增视图层产物 |
| API 入口表来源范围 | ✅ manifest 组件、Binder 绑定（`index_reader.py` 已实现）、WebView bridge、Provider、动态 Receiver 均为现有产物可确定性生成 |
| call_tree on-demand 有界构建 | ✅ 不预生成全量调用树，规避大 APK 内存/时间爆炸；可选落盘支持复现 |
| 三档校验 + `custom_sink_proposal` 不直接否决 | ✅ 回应了上一版"taxonomy 门禁复刻规则边界"的问题，未命中词表不再一票否决 |
| 默认关闭 + 配置开关 + 回归门禁 + identity 含 `candidate_source` | ✅ 不污染确定性核心，防跨源错误合并 |
| Phase 0 Schema 先行 | ✅ 与仓库 19 个 JSON Schema + prompt registry 版本化门禁的既有治理方式一致 |

---

## 4. 问题清单（按严重度排序）

### 4.0 问题状态总览（2026-08-21）

状态标记含义：**✅ 已解决** = 解决方式已明确并写入本文档，关闭条件为合入方案正文；**⚠️ 待决断** = 存在多个可行方案，需用户在 §7 拍板后关闭。§4.11–§4.13 为 2026-08-21 代码复核新增，按发现顺序追加编号（不按严重度重排）。2026-08-21 决断后，§4 全部 13 项均为"✅ 已解决"。

| 编号 | 严重度 | 问题摘要 | 状态 | 关闭条件 |
|---|---|---|---|---|
| §4.1 | 关键 | Agent1"是否构成漏洞、漏洞描述"与低信任边界冲突 | ✅ 已解决 | 修订建议 + 落地方案（§5.7）合入方案正文 |
| §4.2 | 关键 | `chain_proposal.path` 撑不起逐跳校验 | ✅ 已解决 | hops 结构合入方案正文与 Schema 草案（数据层已验证可支撑） |
| §4.3 | 关键 | 检索循环缺状态机与终止语义 | ✅ 已解决 | 循环驱动 + 预算参数合入方案正文（§5.4 已含草案） |
| §4.4 | 高 | `explorer_partial` 深挖协议与预算未定义 | ✅ 已解决 | 已决断（2026-08-21）：新增 `explorer_deep_dive` 协议（§7.1 选项 1） |
| §4.5 | 高 | `custom_sink_proposal` 缺升级闭环 | ✅ 已解决 | 升级回路合入方案正文 |
| §4.6 | 高 | 候选归一化工作量被低估 | ✅ 已解决 | Phase 0 产出字段级映射表（required 实为 10 项，见本节验证补充） |
| §4.7 | 中 | `@JavascriptInterface` 提取路径未定义 | ✅ 已解决 | 调用点推导路径合入方案正文（锚点已修正） |
| §4.8 | 中 | 与 8-16 S1–S11 承接顺序未写明 | ✅ 已解决 | "确定性先行"承接关系合入方案正文 |
| §4.9 | 中 | 批量 × 探索轨成本联动缺失 | ✅ 已解决 | batch 预算帽合入方案正文（落地阶段提前见 §4.12） |
| §4.10 | 低 | 验收口径偏弱 | ✅ 已解决 | 三加一口径合入方案正文 |
| §4.11 | 高 | 规则侧解析能力跨层复用路径未定义（新增） | ✅ 已解决 | 已决断（2026-08-21）：方案 B 规则产物 JSON 传递（§7.2） |
| §4.12 | 高 | batch 级预算帽须提前至 Phase 1（新增） | ✅ 已解决 | 解决方式已明确（见本节），合入 Phase 1 任务与验收 |
| §4.13 | 中 | assets/batches 存储须走迁移机制（新增） | ✅ 已解决 | 解决方式已明确（见本节），合入 Phase 1 任务 |

### 4.1 [关键] Agent1"是否构成漏洞、漏洞描述"与低信任边界直接冲突 【✅ 已解决】

**冲突点**：用户构想要求 Agent1 返回"是否构成漏洞、漏洞描述"；方案 §2.4 规定探索 Agent"禁止：直接下'漏洞成立'结论、直接写正式 facts"。若按方案实施，Agent1 的输出将缺少用户明确要求的两项内容。

**修订建议**：折中设计——`ExplorerObservation` 增加 `hypothesis`（`likely / possible / unlikely`）与 `impact_proposal`（影响面、攻击场景、漏洞类型描述），语义定义为"假设 + 依据"，不是裁决；最终"是否构成"仍由 decision + 人工复核拍板。`hypothesis`/`impact_proposal` 在人工确认后可直接作为 Phase 3 报告草稿的种子，恰好承接用户"……（待设计）"的后半段。此设计不破坏"AI 不写事实"的核心，同时满足用户对 Agent1 判断与描述的需求。

**落地方案（2026-08-18 复核补充）**：Agent1 判定成立的候选**复用当前 L2 判定链路**，即归一化为 `evidence_level=L2` 的候选后走与规则候选完全相同的链路：funnel L2 路由 → 切片 → L2 AI 复核（strict observation）→ 证据回查 → DecisionEngine → `review_state=pending_manual`（报告显示"待人工复核"）→ `render_markdown` 输出 MD 描述。具体映射见 §5.7。必须守住的边界：

1. Agent1 的"判定成立"只是 L2 的输入假设，不是裁决依据；最终 `evidence_decision`（supported / ai_likely_supported / deterministically_refuted / unresolved）由确定性 DecisionEngine 产出，`pending_manual` 语义是"自动分析完成、待人工复核"，与规则候选一致；
2. 只有 `validated` 档候选进入 L2 正式轨道；`partially_validated` 走深挖协议（或带 `blocking_gaps` 的 L2），`unverified` 不进（引用不可回查）；
3. MD 报告中探索假设（Agent1 的 `hypothesis`/`impact_proposal`）与确定性证据分开展示，来源标注为探索假设，不混入确定性结论章节。

### 4.2 [关键] `chain_proposal.path` 无法支撑"每一跳 dataflow"校验 【✅ 已解决】

**问题**：方案 §5.3 草案中 `path` 仅为 `["SplashActivity.onCreate", "loadUrl"]` 字符串数组；§2.5 承诺的确定性回查"每一跳 dataflow"无法对字符串名逐跳执行（同名方法、重载、跨类调用均无法消歧）。

**修订建议**：将 `path` 升级为结构化 `hops`：

```json
"hops": [
  {"from_method_id": "com/example/SplashActivity.java#onCreate:42",
   "to_method_id": "com/example/WebHelper.java#loadUrl:120",
   "call_site_line": 55,
   "arg_positions": [0],
   "resolved_via": "direct_call"}
]
```

校验器逐跳对 `analysis.sqlite3` 的 `call_sites` 表回查。不改此项，`validated` 档就是名义上的，三档校验失去可执行基础。

> 2026-08-21 验证注：`call_sites` 表已确认含 `method_id` / `resolved_target_id` / `resolve_status` / `start_line` 等字段（`index_store.py` L111-128 建表），且 `index_store.py` 已提供 `get_call_sites`（L565）与按 `resolve_status='resolved'` 的 callees/callers 双向查询（L605-621）——hops 逐跳回查在数据层完全可支撑，无索引层前置工作。

### 4.3 [关键] 检索循环缺状态机与终止语义 【✅ 已解决】

**问题**：方案 §2.4 描述"Agent 每次只输出结构化读码请求 → 本地取回 → 拼入下一轮 → 直到找到 sink 或预算耗尽"，但未定义：谁决定"找到 sink"、每轮协议字段、最大轮数、循环驱动方。

**修订建议**：

- 每轮 `ExplorerObservation` 携带 `loop: {done: bool}`，模型在轮末声明"已形成 sink 链"或"需更多上下文"；
- `explorer.py` 是循环驱动者（**不是模型自循环、不是模型自行调工具**），保证每轮输入输出可落盘、可审计；
- 预算参数：`max_rounds_per_entry`（建议 4）、`max_requests_per_entry`（建议 20）、每轮 token 复用 `context_budget`；
- 跑满预算强制终止，产出"部分链 + 缺口清单"而非失败。

### 4.4 [高] `explorer_partial` 送 AI 深挖的协议与预算未定义 【✅ 已解决】

**问题**：方案 §2.6 说 `partially_validated` 候选"可送 AI 深挖"，但当前只有 L1 triage / L2 review 两个 AI 协议（`prompts/` 目录），没有"基于不完整链补齐事实"的协议；"可送"占哪个预算也未说明。

**修订建议**：新增 `explorer_deep_dive` 协议（输入：partial 候选 + 缺失事实清单，输出：可回查的证据/引用，禁止改写链）；或明确复用 L2 review 但声明 `deterministic_facts` 不完整。预算归属写清楚：探索检索占"探索预算"，深挖与 L2 复核占"复核预算"，两本账分开统计。

> 2026-08-21 决断：采用选项 1——新增 `explorer_deep_dive` 协议（输入：partial 候选 + 缺失事实清单；输出：可回查的证据/引用；禁止改写链）。与 L2 review 职责分离：深挖=补齐事实，L2=独立裁决。预算两本账：探索检索走探索预算，深挖与 L2 复核走复核预算。选项权衡过程见 §7.1。

### 4.5 [高] `custom_sink_proposal` 缺升级闭环 【✅ 已解决】

**问题**：方案 §2.2 改为"未命中 taxonomy 不直接否决，进入 `partially_validated` 或人工队列继续深挖"，但"继续深挖"之后没有定义如何升级为正式能力。若缺失闭环，探索轨对"全新漏洞类型/自定义 sink"永远只是分诊工具，无法缓解"规则覆盖限制"这一核心痛点。

**修订建议**：补一条显式回路：**人工确认 custom sink → 版本化扩展 sink taxonomy → 候选重校验 → validated → 进 golden 集**。闭环后探索轨才真正"长能力"（探索发现 → 人工验证 → 规则化 → 回归固化），与 Phase 4 的持续优化目标自洽。

### 4.6 [高] 探索候选归一化为现有 candidate schema 的工作量被低估 【✅ 已解决】

**问题**：`schemas/candidate.schema.json` 强制 `rule_id`（pattern `^[A-Z0-9_]+$`）、`rule_version`、`component`、`sources`、`sinks`、`blocking_gaps`。探索候选进入 funnel 前必须归一化为该形状（如 `rule_id=EXPLORER_AGENT`、`evidence_level=L2`、`sources/sinks` 由校验通过的 chain 转换）。funnel identity 已排除 `rule_id`（技术上可行），但归一化涉及字段映射、chain 转 sources/sinks、缺省 `blocking_gaps` 构造，是 Phase 2 的硬工作量，方案 §2.5 仅一句话带过。

**修订建议**：Phase 0 就产出"ExplorerCandidate → Candidate 归一化映射表"（字段级），并纳入 Phase 2 工作分解与验收。

> 2026-08-21 验证补充：`schemas/candidate.schema.json` 实际 required 共 **10 项**：`rule_id`（pattern `^[A-Z0-9_]+$`）、`rule_version`、`component`、`severity_hint`、`confidence_tier`、`evidence_level`、`locations`、`sources`、`sinks`、`blocking_gaps`——比上文列举多 4 项。映射表须覆盖全部 10 项，探索候选侧来源建议：`severity_hint` 由 `impact_proposal` 映射、`confidence_tier` 由三档校验档位映射、`evidence_level` 固定 `L2`、`locations` 由 hops 的 `evidence_refs` 转换。

### 4.7 [中] `@JavascriptInterface` 的提取路径未定义 【✅ 已解决】

**问题**：方案 §2.1 将 WebView bridge 来源列为"代码索引扫描"，但当前索引不保存注解（`indexer.py` 仅剥离参数注解），现有检测依赖 `addJavascriptInterface` 调用点正则（`rules/shared/detector.py` 的 `WEBVIEW_JS_BRIDGE_EXPOSED` 分支，2026-08-21 锚点修正：实际位于 `_webview_crypto_match` L2842 内 L2860-2872，并无名为 `_webview_js_bridge` 的函数）。"索引扫描注解"没有现成能力。

**修订建议**：明确提取路径：优先 `addJavascriptInterface(obj, name)` 调用点 → 解析 `obj` 类型 → 枚举其 public 方法；`@JavascriptInterface` 注解仅作辅助（JADX 是否保留注解需先验证，若无则以调用点推导为准）。

> 2026-08-21 验证注：已确认 `indexer.py` 仅有 `_strip_parameter_annotations`（L990）、无任何注解保存逻辑——"索引扫描注解"确无现成能力，修订建议的调用点推导路径成立且为唯一现成路径；跨层复用该检测的机制归入 §4.11 / §7.2 一并决断。

### 4.8 [中] 与 8-16 S1–S11 的承接顺序未写明 【✅ 已解决】

**问题**：探索轨验收目标（6/8 覆盖）与 8-16 方案 S1/S3（Binder 解析、L1→L2 确定性深挖）的目标集合高度重叠，一个零 AI 成本、一个高 AI 成本。方案未说明两者先后关系，存在重复投资与候选重复的风险。

**修订建议**：方案中明确承接关系：**先落 S1–S11 确定性修复，探索轨定位为"确定性补强后的残余缺口 + 新类型发现"**；探索候选与规则候选同链时以 `related_candidate_ids` 关联（不合并 identity，但人工视图可对照），避免同一漏洞在人工队列出现两条互不相干的候选。

### 4.9 [中] 批量扫描 × 探索轨的成本联动缺失 【✅ 已解决】

**问题**：方案配置只有 per-run 探索参数；50 候选 × 多轮检索 × 批量 APK 的 AI 成本在 batch 层无总帽。

**修订建议**：batch 配置继承 run 级探索配置，并增加 batch 级总 AI 预算帽（如 `batch.max_ai_calls`、`batch.max_wall_seconds`），超限任务降级为"跳过探索轨，仅确定性主链"。

> 2026-08-21 补充：预算帽的落地阶段须提前至 Phase 1（批量扫描本身即需总帽，而非仅探索轨需要），见 §4.12。

### 4.10 [低] 验收口径仍偏弱 【✅ 已解决】

**问题**：Phase 2 验收"探索轨能覆盖至少 6 项（不要求机器闭合，只要求候选出现）"度量的是 prompt 召回而非系统能力；且未定义"候选与 ground truth 是同一链"的匹配口径，负样本也未纳入探索轨验收。

**修订建议**：验收改为三加一：

1. ≥6/8 中至少 4 条为 `validated`，其余为 `partially_validated`；
2. 负样本（V-04/V-05/V-06、shop 140 控制流共现、OwnSystem 未选择）不出现在探索轨 supports/候选池；
3. 记录探索轨 AI 调用数与 wall-time 成本基线；
4. call_tree 单入口查询延迟/内存实测（深度 ≤ 8、节点 ≤ 500 预算内）。

### 4.11 [高] 规则侧解析能力的跨层复用路径未定义（Binder / 动态 Receiver / WebView 检测）【✅ 已解决】

> 2026-08-21 代码复核新增：锚点位置修正 + 依赖方向缺口。选项权衡与推荐见 §7.2。

**问题**：方案 §2.1 / §5.2 假设 `api_surface`（backend 侧）可直接复用"现有 `index_reader` Binder 绑定、`receiver_registration.py` 动态 Receiver 解析、`addJavascriptInterface` 检测"。但逐项复核确认：

1. **锚点位置偏差**：Binder/AIDL transaction 绑定解析（`_binder_transactions`、`_binder_transaction_code`、实现方法绑定与 `BINDER_IMPLEMENTATION_AMBIGUOUS/UNRESOLVED` gap）位于**规则侧** `rules/shared/index_reader.py`（L1065/L1199/L773-805）；动态 Receiver 解析位于 `rules/shared/receiver_registration.py`；WebView bridge 检测位于 `rules/shared/detector.py`。`backend/app/analysis/index_reader.py` **不存在**，`index_store.py` 本身不做 Binder 解析。
2. **依赖方向缺口**：`backend/app/` 全部源码中**没有任何** `import rules` 先例（全文检索确认）。backend 与规则包目前只通过 `rule_runner.py` 子进程 + JSON 协议交互；方案未定义 backend 侧 `api_surface` 获取这三类规则侧解析结果的机制。

**可选方案**（详见 §7.2）：A——backend 直接 `import rules.shared.*`（最快，但开创 backend → rules 反向依赖先例）；B——规则运行时额外输出确定性产物 JSON，`api_surface.py` 读产物（符合现有"规则子进程 + JSON 协议"模式）；C——解析逻辑上移共享层，双侧引用（依赖方向最干净，但涉及存量迁移与规则回归）。

**连带修正**：本文件 §5.2 表格锚点已更正为规则侧实际路径；无论选哪个方案，方案正文 §2.1 的"来源"列须写明实际路径与传递机制。时序上 `api_surface` 必须排在 `rule_prescan` 之后（§5.1 阶段顺序已如此，方案正文须显式写明该因果：Binder/动态 Receiver/WebView 三类入口来源依赖规则产物或规则侧解析能力）。

**决断结果（2026-08-21）**：采用方案 B——规则运行时额外输出确定性产物（`binder_bindings.json` / `receiver_registrations.json` / `webview_js_bridges.json`），`api_surface.py` 读产物组装入口表；backend 不 import 规则侧代码，backend → rules 保持零依赖。方案正文 §2.1 与本文件 §5.2 已按此修订；方案 C（逻辑上移共享层）作为探索轨稳定后的长期演进项保留，不阻塞当前 Phase。

### 4.12 [高] batch 级 AI/时间预算帽须提前至 Phase 1 落地【✅ 已解决】

> 2026-08-21 代码复核新增。解决方式已明确。

**问题**：§4.9 的修订（batch 级预算帽）挂在探索轨语境下，易被排到 Phase 2。但现状是 `context_budget.max_requests_per_run=500`、`ai.candidate_concurrency=12`——**Phase 1 的批量扫描本身**（不含探索轨）在多 run 并发时就没有 batch 级总帽，AI 成本随资产数线性放大且无降级路径。若 Phase 1 不带总帽上线，批量验收（3 APK）通过后在真实资产规模下会成本失控。

**解决方式**：

1. Phase 1 的 `batch` 配置即包含 §5.5 草案中的 `batch.max_ai_calls`（默认 0=沿用 run 级）与 `batch.max_wall_seconds`，不推迟到 Phase 2；
2. 超限语义：未启动的 run 降级为"跳过 AI 阶段，仅确定性主链"（复用 `ai.enabled=false` 行为），run 记录 `ai_skipped_by_batch_budget` 标记，batch 汇总可审计；
3. Phase 1 验收增加一条：构造 `batch.max_ai_calls=1` 的批量任务，验证后续 run 正确降级且标记可见。

**关闭条件**：合入方案正文 Phase 1 任务清单与验收标准。

### 4.13 [中] assets / batches 存储扩展须走 schema_migrations 机制【✅ 已解决】

> 2026-08-21 代码复核新增。解决方式已明确。

**问题**：方案 Phase 1 仅写"SQLite 增加 `assets` / `batches` 表，run 关联 `asset_id` / `batch_id`"，未指定落库方式。`backend/app/shared/repository.py` 已内联建 `runs` / `findings` / `review_history` 表并维护 `schema_migrations` 表（L75，迁移机制已存在）。若新表直接改内联 `CREATE TABLE`（L79 起），存量库（已产生数据的 `tracer.sqlite3`）不会升级；`runs` 加列也无法无损完成。

**解决方式**：

1. `assets` / `batches` 新表与 `runs` 加列（`asset_id` / `batch_id`，可空外键）一律通过版本化迁移脚本实现并注册进 `schema_migrations`；
2. 迁移含升级路径测试（模拟旧版本库 → 迁移 → 结构与既有数据完好）；回滚至少提供"保留旧库文件重建"的文档说明；
3. 遵循仓库 SQL 安全约定：全部参数绑定，禁止字符串拼接。

**关闭条件**：合入方案正文 Phase 1 任务清单。

---

## 5. 接入当前项目的具体方案

### 5.1 合流点（代码锚点）

探索轨合流点放在 guard 验证之后、`CandidateFunnel.process` 之前（`backend/app/analysis/orchestrator.py` `_run` 的 `candidate_funnel` 阶段前）：

```text
rule_prescan（现有）
   ↓
api_surface + attack_surface（新增，确定性，共享索引）
   ↓
explorer 检索循环（新增，explorer.enabled 才跑）
   ↓
explorer_validation 三档校验（新增）
   ↓
归一化（chain_proposal/hops → candidate，rule_id=EXPLORER_AGENT）
   ↓ 并入 candidates 列表
candidate_funnel → code_slicing → ai_analysis → evidence → decision → aggregation（全部复用现有）
```

### 5.2 新增模块与复用映射

| 新增模块 | 职责 | 复用的现有能力 |
|---|---|---|
| `backend/app/analysis/api_surface.py` | 生成 `api-entry/api_entry_table.json` | manifest 组件 + 规则产物 `binder_bindings.json` / `receiver_registrations.json` / `webview_js_bridges.json`（锚点已修正为规则侧实际路径：`rules/shared/index_reader.py` Binder 绑定、`rules/shared/receiver_registration.py`、`rules/shared/detector.py` `WEBVIEW_JS_BRIDGE_EXPOSED`；2026-08-21 决断方案 B：规则产物 JSON 传递，见 §7.2，backend 不 import 规则侧代码） |
| `backend/app/analysis/attack_surface.py` | 四组件攻击面 JSON | manifest + code-index + 规则输出 |
| `backend/app/analysis/call_tree.py` | 有界 on-demand 检索服务 | `analysis.sqlite3` `call_sites` 表、`index_store.py` by_class/by_package 目标解析 |
| `backend/app/analysis/explorer.py` | 检索循环驱动（非模型自循环） | ai runtime / cache / trace / scheduler（`ai_scheduler.py`） |
| `backend/app/analysis/explorer_validation.py` | 三档校验（hops 逐跳回查 + Guard） | `guard_verifier.py`、`findings/evidence.py` 引用回查 |
| `prompts/explorer/1.0.0/` + `schemas/explorer_observation.schema.json` | Agent1 协议 | prompt registry 版本化 + JSON Schema 门禁 |

### 5.3 产物与生命周期

- `run_dir/api-surface/api_entry_table.json`
- `run_dir/attack_surface/{activity,service,provider,receiver}.json`
- `run_dir/explorer/{observations,candidates}.json`（每轮观测落盘，支持回放审计）
- `run_dir/api-surface/call_tree/{entry_id}.json`（可选落盘）

全部写入 `run_manifest.artifacts`，随 run 版本固化；不另建资产级产物目录，批量扫描/回放/审计天然成立。

### 5.4 探索协议草案（每轮）

```text
输入：attack_surface + api_entry_table + call_tree 服务句柄
      + 已积累代码片段 + 前轮 proposals/read_requests
输出：
  read_requests[]   → get_method_body / get_callees / get_callers / search_symbol
  chain_proposals[] → hops 结构 + evidence_refs + confidence + hypothesis
  component_summary → 组件/代码功能描述
  loop             → {done: bool}
预算：max_rounds_per_entry=4、max_requests_per_entry=20、每轮 token 复用 context_budget
```

### 5.5 配置

```yaml
explorer:
  enabled: false
  max_candidates_per_run: 50
  max_rounds_per_entry: 4
  max_requests_per_entry: 20
  auto_promote: false
  allow_external_code: true
  call_tree:
    max_depth: 8
    max_nodes: 500

batch:
  max_concurrent_runs: 2
  max_ai_calls: 0          # 0 = 沿用 run 级；>0 = batch 总 AI 预算帽
  max_wall_seconds: 0
```

### 5.6 验收口径修订

按 §4.10 的三加一口径替换"候选出现即算覆盖"的弱验收；默认配置下（`explorer.enabled=false`）全量既有测试通过、run 输出与当前基线一致。

### 5.7 探索候选复用 L2 判定链路（最终判定路径）

探索候选（`validated` 档）归一化后以 `evidence_level=L2` 并入现有流水线，与规则候选同轨，最终产出"待人工复核"状态与 MD 报告：

| 环节 | 现有实现（代码锚点） | 探索候选接入方式 |
|---|---|---|
| 归一化 | `schemas/candidate.schema.json` 候选形状 | `rule_id=EXPLORER_AGENT`、`evidence_level=L2`、`sources/sinks` 由校验通过的 hops 转换、`blocking_gaps` 由三档校验结果填充 |
| 路由 | `candidate_precheck`：`evidence_level=="L2"` → L2_REVIEW（`backend/app/analysis/candidate_funnel.py`） | 归一化后直接命中 L2 路由，无需新增分支 |
| 切片 | `context_builder.build_initial`（`backend/app/analysis/context_builder.py`） | 探索候选的 `evidence_refs` 并入初始上下文，照常生成 |
| L2 AI 复核 | `prompts/l2-review` strict observation，独立裁决 supports/refutes/unresolved | Agent1 的 `hypothesis`/`reasoning` 作为输入上下文，L2 AI 独立复核、不受其结论左右 |
| 证据回查 + 决策 | `verify_candidate` + `DecisionEngine.apply`（`backend/app/analysis/orchestrator.py`） | hops 逐跳对 `analysis.sqlite3` `call_sites` 回查，Guard/授权照常判定 |
| 状态 | `derive_review_state` 自动分析完成即 `pending_manual`（`backend/app/findings/review_state.py`）；`STATUS_LABELS` 显示"待人工复核"（`backend/app/findings/report.py`） | 与规则候选一致；人工确认后置 `confirmed` / `manual_false_positive` |
| MD 报告 | `build_report_payload` + `render_markdown`（`backend/app/findings/report.py`）；API：`backend/app/api/routes.py` report endpoint | Agent1 的 `hypothesis`/`impact_proposal`/`component_summary` 作为 `description` 种子，但标注"探索假设"来源，与确定性证据分离展示 |

判定路径一句话：**Agent1 负责"提出并描述"，L2 链路负责"验证并定状态"**。`validated` 候选 → L2 复核/证据/决策 → `pending_manual` → 人工确认后进入 Phase 3 报告生成。

---

## 6. 结论与建议

**结论**：修改后的方案与用户构想的贴合度已很高（主体五项全部纳入），方向正确、接入方式与项目基因一致（低信任输入 → 确定性门禁 → 高信任输出）。当前不建议推翻重设计，而是按 §4 的修订点补强后进入 Phase 0。

**建议的实施顺序**：

1. **Phase 0 前**：先补齐 §4.1（hypothesis/impact_proposal）、§4.2（hops 结构）、§4.3（循环状态机）三个关键设计，产出 `ExplorerObservation` 与 `ExplorerCandidate` Schema；
2. **Phase 0**：同时产出"ExplorerCandidate → Candidate 归一化映射表"（§4.6）与 call_tree 服务接口定义；
3. **Phase 1/2**：资产批量先行，探索轨在单 APK 上验证后再推广批量；探索轨与 8-16 S1–S11 并行时，以"确定性补强先行、探索轨承接残余缺口"为序（§4.8）；
4. **Phase 4**：以动态终审为 ground truth 扩展 golden，探索轨命中率与成本基线双指标回归（§4.10）；
5. **决断项（2026-08-21 已全部关闭）**：§7 两项已拍板（§7.1 选新增 `explorer_deep_dive` 协议、§7.2 选方案 B 规则产物传递）；§4 全部 13 项问题均为"已解决"，修订点已合入方案正文，实施计划与验收标准见 `docs/analysis/2026-08-21-explorer-track-implementation-plan.md`。

> 备注：本审核文档为设计评审结论，非实施记录；实施前须将 §4 的修订点合入方案正文，避免设计与落地脱节。

---

## 7. 决断记录（2026-08-21 拍板，原"待决断问题清单"）

> 以下两项已于 2026-08-21 由用户拍板（结论标注于各小节末尾），对应 §4.4 / §4.11 已关闭并同步方案正文。原文保留以记录权衡过程。

### 7.1（对应 §4.4）`partially_validated` 候选送 AI 深挖：新增协议 or 复用 L2 review？

| 维度 | 选项 1：新增 `explorer_deep_dive` 协议 | 选项 2：复用 l2-review，声明 facts 不完整 |
|---|---|---|
| 实现成本 | 新增 prompt 版本目录 + schema + registry 注册（一个 Phase 0 工作项） | 近零增量：输入 payload 加 `facts_incomplete=true` 标记与 prompt 说明 |
| 语义清晰度 | 输入输出专为"补齐缺失事实"设计，可禁止改写链 | L2 语义是"独立裁决 supports/refutes"，与"补齐事实"目标错位，prompt 需显著改造——改造后实质等于新协议 |
| 治理成本 | 版本化、哈希门禁走既有机制，多一个协议维护 | 少一个协议，但 l2-review 语义被重载，回归基线与统计口径可能互相污染 |
| 适用场景 | partial 候选量大、深挖成为常态 | partial 候选少、深挖偶发 |

**推荐**：**选项 1（新增 `explorer_deep_dive`）**。选项 2 的"复用"需改造 prompt 语义，改完已不是原协议，却继承 l2-review 的回归基线与统计口径，治理上得不偿失；且 §4.1 已确定 Agent1 validated 候选走标准 L2 复核，深挖（补事实）与 L2（裁决）职责分离更不易混淆。预算归属按 §4.4 原建议：探索检索走探索预算，深挖与 L2 复核走复核预算，两本账分开统计。

**✅ 决断结果（2026-08-21）**：采用选项 1（新增 `explorer_deep_dive` 协议）。已执行：§4.4 状态改"已解决"；Phase 0 工作项增加 `prompts/explorer-deep-dive/1.0.0/` + 对应 schema（已合入方案正文与实施计划）。

### 7.2（对应 §4.11）backend 侧 `api_surface` 获取规则侧解析结果的机制？

三个选项的完整利弊见 §4.11：

- **方案 A**：backend 直接 `import rules.shared.index_reader` 等模块。实现最快，但开创 backend → rules 反向依赖先例（当前 0 处），规则包独立演进性受损；规则子进程隔离的设计语义也被绕开。
- **方案 B**：规则运行时额外输出确定性产物（如 `run_dir/rule_prescan/binder_bindings.json`、`receiver_registrations.json`、`webview_js_bridges.json`），`api_surface.py` 读产物组装入口表。与现有"规则子进程 + JSON 协议"模式完全一致，依赖方向不变；代价是规则产物清单需扩展。时序无额外代价——`api_surface` 本就必须排在 `rule_prescan` 之后（§5.1）。
- **方案 C**：将 Binder/动态 Receiver 解析逻辑上移至共享层（如 `backend/app/analysis/`），规则侧与 backend 共同引用。依赖方向最干净、单源；但涉及存量代码迁移与规则侧全量回归，工作量与风险最大。

**推荐**：**方案 B（规则产物传递）**。理由：与现有架构模式零冲突；方案 C 的收益真实但可延后（探索轨稳定、确认长期双端需求后再做，避免 Phase 2 同时引入"新能力 + 存量重构"两类风险）；方案 A 的反向依赖先例不建议开。

**✅ 决断结果（2026-08-21）**：采用方案 B（规则产物 JSON 传递）。已执行：§4.11 状态改"已解决"；方案正文 §2.1 与本文件 §5.2 已按方案 B 修订；Phase 2 工作分解增加"规则产物扩展（binder_bindings / receiver_registrations / webview_js_bridges）"任务项。方案 C 作为长期演进项保留。
