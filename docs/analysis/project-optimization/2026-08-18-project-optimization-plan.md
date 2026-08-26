# AI-APK-Tracer 项目优化方案：确定性可信判定 × Agent 化批量探索

> **日期**：2026-08-18
> **性质**：指导性优化方案（非实施记录）。面向“当前项目与 Agent 化批量漏洞挖掘框架对比后的补强方向”。
> **前置结论**：本项目当前优势是“准、可审计、可回归、误报低”，短板是“规则覆盖有限、单 APK 工作流、无批量资产扫描、不产出 PoC/修复方案、探索性不足”。本方案在**不牺牲确定性核心**的前提下补齐这些短板。
> **修订记录（2026-08-21）**：按评审文档（`2026-08-18-project-optimization-plan-review.md`）§4 全部 13 项修订点合入正文，含两项用户决断——深挖采用**新增 `explorer_deep_dive` 协议**（评审 §7.1 选项 1）、规则侧解析能力复用采用**规则产物 JSON 传递**（评审 §7.2 方案 B，backend → rules 保持零依赖）。实施计划与验收标准另见 `2026-08-21-explorer-track-implementation-plan.md`。同日增补：**核验 agent（L2 agent 化演进）**设计见 §2.7，实施见实施计划 T2.11/T2.12。

---

## 1. 优化目标

1. **保留并强化确定性核心**：规则、funnel、证据校验、决策、版本化协议仍是最终判定的权威边界。
2. **增加探索性发现能力**：引入“探索候选”轨道，允许类似 Agent1 的自由源码追踪产出候选，但必须经过确定性校验后才能进入 L2/正式 finding。
3. **支持包资产批量扫描**：从“单个 APK 上传”演进为“package list → 拉取/反编译 → 批量 run → 资产级汇总”。
4. **增强交付能力**：在人工确认后生成结构化漏洞报告、可审查的 PoC 骨架与修复建议，不自动执行 APK。
5. **建立可度量的持续优化闭环**：以动态终审/人工复核为 ground truth，扩展 golden 集与批量评估，量化 precision/recall/成本/报告质量。

---

## 2. 总体设计原则

### 2.1 双轨制：确定性主链 + 探索补充轨

```text
确定性主链（现有）:
APK → decompile/index → rule_prescan → funnel → slice → AI strict observation
     → evidence validation → decision → aggregation → review

探索补充轨（新增）:
asset → decompile/index → api_surface + call_tree + attack_surface
     → explorer candidates（低信任，受控检索循环）
     → deterministic candidate normalization（三档校验）
     → validated/partially_validated → 并入 funnel 校验
     → unverified → 人工队列（不占 AI 预算）
```

- 探索轨不能直接写 `sources/sinks/propagation_paths`，只能输出“建议链 + 代码引用”。
- 确定性校验通过后，探索候选与规则候选走同一套 L2 证据/决策/定级流程。
- 探索轨不增加正式候选的误报：`unverified` 探索候选默认不占 AI 预算、不进入报告，只进入人工队列（可配置）。

### 2.2 低信任输入，高信任输出

- 探索候选必须携带：`source=explorer_agent`、`prompt_version`、`model`、`evidence_refs`、`confidence`、`chain_proposal`。
- 证据引用必须回查 SQLite 索引/反编译产物；无法回查的引用直接失效。
- 所有探索候选进入确定性门禁：source/sink taxonomy、每一跳 dataflow、授权/Guard、coverage gap 检查；**未命中现有 taxonomy 的 sink 标记为 `custom_sink_proposal`，不直接否决**，进入 `partially_validated` 或人工队列继续深挖。

### 2.3 可审计与可回滚

- 探索 Agent 的 prompt/schema 同样版本化、哈希门禁。
- 每次探索候选写入独立 trace；批量扫描结果可按资产、规则版本、探索版本回放。
- 任何新能力默认关闭，通过配置显式开启；上线前须跑历史回归。

### 2.4 安全边界

- 不执行 APK 代码；不自动发起攻击请求。
- PoC 默认输出“PoC 骨架”（调用序列/Intent/参数描述），只有用户显式确认后才可生成可执行脚本；生成物需标注“未经动态验证”。
- 批量拉取仅支持用户已配置的合法来源（如本地已下载 APK、企业授权仓库），不内置绕过。

---

## 3. 总体架构（目标态）

```text
┌─ Asset Layer（新增）────────────────────────────────────────┐
│ package list / import / pull → asset registry                │
│   → batch scan jobs → per-APK run → asset summary            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ Deterministic Artifact Layer（新增）─────────────────────────┐
│ decompile sources（复用 run_dir/decompile/sources）          │
│   → api-surface/api_entry_table.json（API 入口表）           │
│   → call_tree on-demand service（API 接口调用树，有界构建）   │
│   → attack_surface/*.json（activity/service/provider/receiver）│
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ Explorer Layer（新增，类似 Agent1，受控检索循环）─────────────┐
│ attack_surface + api_entry_table + call_tree service          │
│   → agent 输出 structured read/callers/callees 请求           │
│   → 本地检索取回代码片段 → 直到 sink 或预算耗尽               │
│   → explorer candidates（strict observation）                │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ Deterministic Validation（复用/扩展现有）────────────────────┐
│ candidate normalization → 三档校验（validated/partial/unverified）│
│   → funnel identity → L2 promotion gate → evidence → decision │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─ Reporter Layer（新增，类似 Agent2，人工确认后触发）────────────┐
│ finding + evidence + code refs → report draft                │
│   → PoC skeleton + fix suggestions → human review             │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. 分阶段实施计划

### Phase 0：基线与接口设计（约 1 周）

**目标**：先定义清楚“探索候选”和“资产批量”的边界，避免污染现有确定性核心。

**任务**：

1. 梳理现有 run 生命周期与 API，设计 `BatchScan` / `Asset` 数据模型。
2. 定义 `ExplorerCandidate` Schema：
   - 必填：`candidate_id`、`source`、`component`、`entry_method`、`chain_proposal`（含结构化 `hops`、`hypothesis`、`impact_proposal`）、`evidence_refs`、`confidence`、`prompt_version`、`model`。
   - 禁止字段：`sources/sinks/propagation_paths` 不能由探索 Agent 直接写正式值。
3. 设计 `api_entry_table` Schema：
   - 复用 manifest + 索引 + Binder/WebView 解析结果，确定 API 入口表的字段边界。
4. 设计 `call_tree` 服务接口：
   - 定义 `get_method_body` / `get_callees` / `get_callers` 等检索能力与预算参数（深度、节点数）。
5. 设计 `attack_surface` Schema：
   - 复用现有 manifest 解析结果与规则输出，导出四个组件 + WebView/密码学攻击面 JSON。
6. 制定“探索候选接入 funnel”的配置开关：
   - `explorer.enabled=false` 默认关闭；
   - `explorer.max_candidates_per_run=50`；
   - `explorer.auto_promote=false`；
   - `api_surface.enabled=false`、`call_tree.max_depth=8`、`call_tree.max_nodes=500`。
7. 定义 `explorer_deep_dive` 协议 Schema 与 prompt 骨架（评审 §7.1 决断：新增协议）：输入 partial 候选 + 缺失事实清单，输出可回查证据/引用，禁止改写链；预算归属复核账本。
8. 定义规则产物 Schema（评审 §7.2 决断方案 B）：`binder_bindings` / `receiver_registrations` / `webview_js_bridges`，并确认规则侧导出改造点。
9. 产出“ExplorerCandidate → Candidate 归一化映射表”（字段级，覆盖 candidate.schema.json required 全部 10 项，映射关系见 §2.5）。
10. 定义核验 agent（`verify`）协议 Schema 与 prompt 骨架（2026-08-21 增补，见 §2.7）：命题清单输入结构 + 盲验输入构造规则（剥离假设层）+ 逐命题判定输出；配置开关 `verify.enabled=false` 默认关闭，含轮数与降级回退参数。

**验收**：

- 新增 Schema 文件通过现有 schema 校验测试。
- 配置开关可加载、可回退。
- 不改变任何现有 run 行为（全量测试通过）。

---

### Phase 1：资产批量扫描层（约 2 周）

**目标**：支持“给定 package list，批量创建 run、汇总结果”。

**方案**：

1. 新增 `backend/app/assets/`：
   - `registry.py`：资产注册表（package name、apk path/sha256、来源、状态、最近 run_id）。
   - `batch.py`：批量扫描编排（串行/并发、失败重试、资源上限）。
2. 新增 API：
   - `GET /api/assets`：资产列表。
   - `POST /api/assets/import`：导入本地 APK 或包名列表。
   - `POST /api/batches`：创建批量扫描（可指定资产子集、配置覆盖）。
   - `GET /api/batches/{batch_id}`：批量进度与汇总。
3. 存储扩展：
   - SQLite 增加 `assets` / `batches` 表，run 关联 `asset_id` / `batch_id`；一律通过版本化迁移脚本注册进 `schema_migrations`（禁止改内联 CREATE TABLE），迁移含旧库升级路径测试。
4. batch 级预算帽（Phase 1 即落地，不推迟到 Phase 2；评审 §4.12）：
   - `batch.max_ai_calls`（默认 0=沿用 run 级）与 `batch.max_wall_seconds`；
   - 超限语义：未启动的 run 降级为“跳过 AI 阶段，仅确定性主链”（复用 `ai.enabled=false` 行为），run 记录 `ai_skipped_by_batch_budget` 标记，batch 汇总可审计。
5. 前端新增“资产/批量”页面：
   - 资产列表、导入、批量扫描、按包/按批次查看 findings 汇总。

**验收**：

- 用 3 个本地 APK 导入并批量扫描成功，每个 APK 独立 run，结果可按批次汇总。
- 单 APK run 行为与当前一致（回归测试通过）。
- 批量扫描有并发上限、失败任务可单独重跑。
- 构造 `batch.max_ai_calls=1` 的批量任务：后续 run 正确降级为仅确定性主链，`ai_skipped_by_batch_budget` 标记可见、batch 汇总可审计。
- 旧版本 `tracer.sqlite3` 经迁移脚本升级后结构与既有数据完好（迁移测试通过）。

---

### Phase 2：API surface + call tree + 探索轨合流（约 3-4 周）

**目标**：让系统具备 Agent1 的“从攻击面出发、沿 API 调用树自由追踪”能力，但产出受控、可回查、可审计。

#### 2.0 接入原则

- **确定性补强先行**：先落 8-16 方案 S1–S11 确定性修复，探索轨定位为“确定性补强后的残余缺口 + 新类型发现”；探索候选与规则候选同链时以 `related_candidate_ids` 关联（不合并 identity，人工视图可对照）。
- **规则产物传递（评审 §7.2 决断方案 B）**：规则运行时新增导出 `binder_bindings.json` / `receiver_registrations.json` / `webview_js_bridges.json` 确定性产物（注册进 run_manifest.artifacts）；backend 侧 `api_surface.py` 只读产物、**不 import 规则侧代码**，backend → rules 保持零依赖。
- **复用现有反编译目录**：不另建 `/Source/com.xxx.example/source`，统一使用 `run_dir/decompile/sources`，只在产物层增加 `api-surface/`、`attack_surface/`、`explorer/` 视图。
- **不预生成全量调用树**：大 APK 会爆内存/时间；调用树按入口 **on-demand 有界构建**，深度/节点数受预算约束。
- **Agent1 低信任**：只输出“建议链 + 代码引用”（含 `hypothesis` / `impact_proposal` 假设字段），不直接写正式 `sources/sinks/propagation_paths`，不直接写 finding/review_status；validated 候选最终判定复用现有 L2 链路（见 §2.5）。
- **校验分三档**：`validated / partially_validated / unverified`；`unverified` 不等于低价值，保留人工队列并按置信度/引用完整度排序。

#### 2.1 API 入口表（API surface）

- 新增 `backend/app/analysis/api_surface.py`，产物：
  ```text
  run_dir/api-surface/api_entry_table.json
  ```
- 内容来源（全部确定性生成；评审 §7.2 决断方案 B：规则运行时输出产物 JSON，`api_surface.py` 读产物组装，backend 不 import 规则侧代码）：
  | 入口类型 | 来源 |
  |---|---|
  | Activity/Service/Receiver/Provider 入口 | manifest + 源码入口方法 |
  | Binder AIDL 接口方法 / transaction | 规则产物 `rule_prescan/binder_bindings.json`（规则侧 `rules/shared/index_reader.py` 解析结果导出） |
  | WebView JS bridge | 规则产物 `rule_prescan/webview_js_bridges.json`（调用点推导：`addJavascriptInterface(obj, name)` → 解析 `obj` 类型 → 枚举 public 方法；`@JavascriptInterface` 注解仅辅助） |
  | Deep Link / Intent filter | manifest |
  | Provider authority / URI | manifest + 源码 |
  | 动态 Receiver action | 规则产物 `rule_prescan/receiver_registrations.json`（规则侧 `rules/shared/receiver_registration.py` 解析结果导出） |
- **时序约束**：`api_surface` 必须排在 `rule_prescan` 之后（Binder / 动态 Receiver / WebView 三类入口来源依赖规则产物）。
- 新增 `schemas/api_entry_table.schema.json` 与规则产物 schema（`binder_bindings` / `receiver_registrations` / `webview_js_bridges`），为 Agent1 提供“对外暴露 API 揭秘”的稳定输入。

#### 2.2 API 接口调用树（on-demand）

- 新增 `backend/app/analysis/call_tree.py`，复用 `analysis.sqlite3` 调用边，提供检索服务：
  ```text
  get_entry_points()
  get_method_body(method_id)
  get_callees(method_id)
  get_callers(method_id)
  resolve_invoke_target(expr)
  class_hierarchy(class_name)
  search_symbol(name)
  ```
- 按入口构建**有界子树**（深度、节点数、token 预算），可选落盘：
  ```text
  run_dir/api-surface/call_tree/{entry_id}.json
  ```
- 该服务同时供 Explorer Agent、核验 Agent（取证循环，见 §2.7）和未来人工分析使用，不固定预生成全量调用树。

#### 2.3 攻击面导出（attack_surface）

- 新增 `backend/app/analysis/attack_surface.py`，从 `manifest.json` + `code-index.json` + 规则输出生成：
  ```text
  run_dir/attack_surface/activity.json
  run_dir/attack_surface/service.json
  run_dir/attack_surface/provider.json
  run_dir/attack_surface/receiver.json
  ```
- 每个文件包含：组件名、导出状态、权限、入口方法、intent/action/uri、敏感能力、关联 API 入口。
- 新增 `schemas/attack_surface.schema.json`。

#### 2.4 探索 Agent（Agent1 受控检索循环）

- 新增 `prompts/explorer/1.0.0/system.md` 与 `user.md`：
  - 输入：`attack_surface/*.json` + `api_entry_table.json` + 索引摘要 + 可选的初始切片。
  - 输出：`ExplorerObservation`（strict schema），只允许：
    - `chain_proposals[]`：每条含 source、sink、**结构化 `hops` 路径**（见 §5.3）、代码引用、理由、置信度、`hypothesis`（`likely / possible / unlikely`，假设而非裁决）、`impact_proposal`（影响面/攻击场景/漏洞类型描述）、是否需扩片；
    - `read_requests[]`：精确的类/方法/文件引用（get_method_body / get_callees / get_callers / search_symbol）；
    - `component_summary`：组件功能描述；
    - `loop: {done: bool}`：模型在轮末声明“已形成 sink 链”或“需更多上下文”（循环状态机，评审 §4.3）。
  - 禁止：直接下“漏洞成立”结论、直接写正式 facts、输出不可回查的引用。`hypothesis`/`impact_proposal` 语义为“假设 + 依据”，最终裁决由 decision + 人工复核拍板（评审 §4.1）。
- 新增 `backend/app/analysis/explorer.py`：
  - 调度探索 Agent；复用现有 AI runtime、预算、cache、trace 能力。
  - **`explorer.py` 是循环驱动者**（不是模型自循环、不是模型自行调工具），每轮输入输出落盘、可审计；与 `call_tree.py` 构成“检索循环”：Agent 每次只输出结构化读码请求 → 本地取回代码片段 → 拼入下一轮 → 直到 `loop.done` 或预算耗尽。
  - 循环预算：`max_rounds_per_entry=4`、`max_requests_per_entry=20`、每轮 token 复用 `context_budget`；跑满预算强制终止，产出“部分链 + 缺口清单”而非失败。
  - 将 `chain_proposals` 转换为 `ExplorerCandidate`，不写正式 sources/sinks。
- 新增 `prompts/explorer-deep-dive/1.0.0/`（评审 §7.1 决断：新增协议）：
  - 用途：`partially_validated` 候选的 AI 深挖。输入：partial 候选 + 缺失事实清单；输出：可回查的证据/引用；**禁止改写链**。
  - 与 L2 review 职责分离：深挖=补齐事实，L2=独立裁决（supports/refutes/unresolved）。
  - 预算归属：探索检索（explorer 循环）占“探索预算”，深挖与 L2 复核占“复核预算”（核验 agent 为第三本“核验预算”，见 §2.7），分开统计。

#### 2.5 探索候选校验与合流（三档）

- 新增 `backend/app/analysis/explorer_validation.py`：
  - 对每条 proposal 做确定性回查：
    - 引用的类/方法/行号是否存在；
    - source/sink 是否命中现有 taxonomy（未命中允许标记 `custom_sink_proposal`，不直接否决）；
    - **hops 逐跳回查**：每跳 `from_method_id` / `to_method_id` / `call_site_line` 对 `analysis.sqlite3` `call_sites` 表验证（`resolved_target_id`、`resolve_status='resolved'`）；
    - 是否被 Guard/authorization 直接阻断。
  - 输出三档：
    ```text
    validated            → 归一化后并入正式候选，走现有 L2/evidence/decision
    partially_validated  → 有引用但 dataflow 不完整，送 explorer_deep_dive 深挖或人工高优
    unverified           → 保留人工队列，不占 AI 预算，按置信度/引用完整度排序
    ```
- **归一化（Phase 0 产出字段级映射表）**：`validated` 候选归一化为现有 candidate 形状（`schemas/candidate.schema.json` required 共 10 项：`rule_id` / `rule_version` / `component` / `severity_hint` / `confidence_tier` / `evidence_level` / `locations` / `sources` / `sinks` / `blocking_gaps`）。映射：`rule_id` 固定 `EXPLORER_AGENT`（funnel identity 已排除该字段，不跨源合并）、`rule_version` = 探索协议版本、`severity_hint` ← `impact_proposal`、`confidence_tier` ← 三档校验档位、`evidence_level` 固定 `L2`、`locations` ← hops 的 `evidence_refs`、`sources`/`sinks` ← 校验通过的 hops 链首/链尾、`blocking_gaps` ← 三档校验结果。
- **最终判定复用 L2 链路**：归一化候选 → funnel L2 路由 → 切片 → L2 AI 复核（独立裁决，不受 Agent1 `hypothesis` 左右）→ 证据回查 → DecisionEngine → `review_state=pending_manual`（待人工复核）→ MD 报告（探索假设与确定性证据分离展示）。**Agent1 负责“提出并描述”，L2 链路负责“验证并定状态”**（环节级映射见评审文档 §5.7）。
- **custom sink 升级闭环**：人工确认 custom sink → 版本化扩展 sink taxonomy → 候选重校验 → `validated` → 进 golden 集（探索发现 → 人工验证 → 规则化 → 回归固化）。
- 合流点放在 `candidate_funnel` 之前：
  ```text
  现有规则候选
          +
  探索候选（经过三档校验）
          ↓
  CandidateFunnel.process()
          ↓
  现有 code_slicing / ai_analysis / evidence / aggregation
  ```

#### 2.6 Funnel 扩展

- `CandidateFunnel` 增加 `candidate_source` 字段支持：
  - 规则候选（`rule`）、探索候选（`explorer`）、人工导入候选（`manual`）。
- 探索候选默认 disposition：
  - `validated` → `explorer_promoted`，进入 L2 或按规则候选同等路由；
  - `partially_validated` → 按 `explorer_partial` 路由，可送 AI 深挖；
  - `unverified` → `explorer_unverified`，不送 AI，人工可查看。
- identity 计算包含 `candidate_source`，避免探索候选与规则候选错误合并。

#### 2.7 核验 Agent（L2 agent 化演进，2026-08-21 增补）

- **定位**：不是在 L2 之外新增一层，而是 L2 review 的**agent 化演进形态**——验证导向的受控取证循环。输出仍是 strict observation（`verdict` / `flaw_holds` / `exploitability` / `evidence_refs`），最终裁决仍由 DecisionEngine + 人工完成，"确定性终裁"红线不变。
- **命题清单输入（claims）**：验证任务由确定性代码结构化为待证命题清单（入口可达？传播成立？Guard 有效？授权阻断？），从候选的 sources/sinks/Guard 事实与探索候选的 hops 生成，**不从 Agent1 的描述生成**。
- **盲验（防锚定）**：核验 agent 输入只含可回查事实层（hops、`evidence_refs`、确定性 facts），剥离探索轨的 `hypothesis` / `impact_proposal` / `confidence` / `reasoning`，避免被提出者倾向带偏（LLM 确认偏误）；核验结论与探索假设的冲突项在人工视图标记为重点。
- **受控取证循环**：复用 explorer 的循环模式（代码驱动、每轮落盘、轮数预算），但终止条件是"命题全部判定"而非模型自声明；预算耗尽产出"已证命题 + 缺口清单"并降级。
- **分流（M2 试点）**：探索 `validated` 候选必进；规则 L2 候选以核验 agent 替代单轮 L2 review（单轮 L2 保留为 A/B 对照与降级基线）；**L1 攻击面典型验证为 M4 评估后的扩展项**——L1 无待证命题（`l1_skip_ai=true` 的实证依据），直接"验证 L1"属于探索职责，启用前须先由确定性代码将 L1 暴露面命题化并设抽样上限。
- **降级回退**：agent 失败/预算耗尽自动回退现有单轮 L2，主链永不因新能力不可用而阻塞；核验预算独立记账（第三本账，batch 帽覆盖）。
- **对 `ai_likely_supported` 的影响**：该档位**保留**——它是 DecisionEngine 对"证据不完整时的诚实处理"，非 AI 协议产物。agent 化消除的是"上下文不足"型不完整（该部分候选升级为 `supported` 或被确定性否决）；"静态不可判定"型（反射/运行时/跨进程）仍诚实落入 `ai_likely_supported` 待人工。采信标准（四要素 + 确定性数据流）不随 AI 能力漂移，M4 以该档位占比变化作为核验 agent 效果实测指标之一（预期下降、不归零）。

**验收（2026-08-21 修订为“三加一”口径，评审 §4.10）**：

1. **覆盖**：health/shop 两个已知 APK 开启探索轨，至少各产出 5 条 `validated` 或 `partially_validated` 候选；已知 8 项动态终审成立漏洞中探索轨覆盖 ≥ 6 项，其中 ≥ 4 条为 `validated`、其余为 `partially_validated`（“同一链”匹配口径：候选与 ground truth 的 source 组件与 sink 方法一致即视为同一链）。
2. **负样本**：V-04/V-05/V-06、shop 140 控制流共现、OwnSystem 未选择等负样本不出现在探索轨 supports/候选池；未通过校验的引用 0 条进入正式 finding。
3. **成本**：记录探索轨 AI 调用数与 wall-time 成本基线（探索 / 复核 / 核验三本账分开统计，核验账本见 §2.7）。
4. **性能**：call_tree on-demand 在 health 上单入口查询延迟和内存可控（深度 ≤ 8、节点 ≤ 500 默认预算内）。
5. **回归**：全量既有测试通过，默认配置（`explorer.enabled=false`）下探索轨不改变现有输出。

---

### Phase 3：自动报告、PoC 骨架与修复建议（约 2 周）

**目标**：在人工确认后生成交付级报告，补齐 Agent2 的产出能力。

**方案**：

1. 新增 `prompts/report/1.0.0/system.md` / `user.md`：
   - 输入：已确认 finding + 证据 + 代码引用 + 决策事实。
   - 输出：`ReportDraft`（漏洞详情、影响、复现步骤/触发条件、修复建议、参考）。
   - 仅可在 `review_status=confirmed` 或 `manual_confirmed` 后触发。
2. 新增 `backend/app/findings/report_generator.py`：
   - 将 `ReportDraft` 与确定性 `evidence` 合并，生成 Markdown/JSON 报告。
   - 报告必须包含 `evidence_refs` 回查结果和 `generated_by_ai=false` 的确定性字段。
3. 新增 `backend/app/findings/poc_skeleton.py`：
   - 根据组件类型生成非可执行 PoC 骨架：
     - Activity/Service/Receiver → Intent/Action/Extra 描述；
     - Provider → URI/权限/query 描述；
     - Binder → transaction code/参数类型描述。
   - 默认不生成可执行脚本；`allow_executable_poc=false` 默认关闭。
4. 前端报告面板增加“生成报告草稿”“生成 PoC 骨架”按钮，展示 AI 草稿与确定性证据分离状态。

**验收**：

- 对 2 个已确认 finding 生成报告草稿与 PoC 骨架，字段完整、引用可回查。
- 未确认 finding 无法触发报告生成。
- 默认不产生任何可执行文件。

---

### Phase 4：评估与持续回归（约 2 周，可与 Phase 2/3 并行）

**目标**：把“优化是否有效”变成可量化指标。

**任务**：

1. 扩展 golden 集：
   - 正样本：动态终审 8 项成立漏洞。
   - 负样本：V-04/V-05/V-06、shop 140 控制流共现、OwnSystem 未选择等。
   - 增加“探索轨命中率”指标：`explorer_hit_count / confirmed_vulns`。
2. 增加批量评估：
   - `evaluation/runner` 支持多 APK 输入，输出 precision/recall/F1、AI 调用数、人工复核时长估算。
3. 增加“报告质量”检查：
   - 报告草稿中 AI 生成内容与确定性事实是否混淆；
   - 所有代码引用是否可回查；
   - PoC 骨架是否与 finding 的 source/sink/组件一致。
4. 建立优化门槛：
   - 新能力默认关闭或 beta，只有 golden 指标不劣于当前基线才可默认开启。

**验收**：

- 在 health/shop 双 APK 上输出完整指标表。
- 与 2026-08-16 验收基线对比：机器闭合率不下降，探索轨命中率 ≥ 6/8。
- 新增负样本零误报进入正式 finding（探索轨未校验候选不计入误报）。

---

## 5. 关键设计细节

### 5.1 API 入口表 Schema（草案）

```json
{
  "schema_version": "1.0.0",
  "package_name": "com.example",
  "api_entries": [
    {
      "entry_id": "act_com_example_SplashActivity_onCreate",
      "kind": "activity",
      "component_name": "com.example.SplashActivity",
      "exported": true,
      "permissions": [],
      "entry_method": "onCreate(Landroid/os/Bundle;)V",
      "intent_filters": [
        {"action": "android.intent.action.VIEW", "scheme": "https"}
      ],
      "source": "manifest"
    },
    {
      "entry_id": "binder_com_example_ISportXms_finishSport",
      "kind": "binder",
      "component_name": "com.example.SportXmsService",
      "exported": true,
      "interface_method": "finishSport",
      "transaction_code": 4,
      "implementation_method_id": "com/example/SportXmsApiImpl.java#finishSport:504",
      "source": "rule_artifact:binder_bindings"
    }
  ]
}
```

> 该表是 Agent1 的“对外暴露 API 揭秘”输入，全部字段由确定性代码生成，不允许 Agent 伪造。

### 5.2 call_tree on-demand 服务

```text
CallTreeService（backend/app/analysis/call_tree.py）
  ├─ get_entry_points(filter)
  ├─ get_method_body(method_id)
  ├─ get_callees(method_id)
  ├─ get_callers(method_id)
  ├─ resolve_invoke_target(expr)
  ├─ class_hierarchy(class_name)
  └─ search_symbol(name)
```

- 每个入口构建有界子树，默认预算：深度 ≤ 8、节点 ≤ 500、单次查询 token ≤ 上下文预算。
- 可选落盘 `run_dir/api-surface/call_tree/{entry_id}.json`，供人工复核与复现。
- 不预生成全量调用树，避免大 APK 内存/时间爆炸。

### 5.3 探索候选 Schema（草案）

```json
{
  "schema_version": "1.0.0",
  "candidate_id": "expl_<20hex>",
  "source": "explorer_agent",
  "prompt_version": "explorer/1.0.0",
  "model": "deepseek-v4-flash",
  "component": {
    "kind": "activity",
    "name": "com.example.SplashActivity",
    "exported": true,
    "entry_method": "onCreate"
  },
  "api_entry_ref": "act_com_example_SplashActivity_onCreate",
  "chain_proposal": {
    "source": "Intent.getExtras().getString",
    "sink": "WebView.loadUrl",
    "hops": [
      {"from_method_id": "sources/com/example/SplashActivity.java#onCreate:42",
       "to_method_id": "sources/com/example/WebHelper.java#loadUrl:120",
       "call_site_line": 55,
       "arg_positions": [0],
       "resolved_via": "direct_call"}
    ],
    "call_tree_refs": [
      "call_tree/act_com_example_SplashActivity_onCreate.json"
    ],
    "evidence_refs": [
      {"file": "sources/com/example/SplashActivity.java", "line": 42}
    ],
    "confidence": "medium",
    "hypothesis": "likely",
    "impact_proposal": "外部 Intent 可控制 WebView 加载 URL，可能构成任意 URL 加载攻击面",
    "reasoning": "外部 intent 可控制 URL 并传入 loadUrl，未见 scheme 校验"
  },
  "read_requests": [
    {"class": "com.example.SplashActivity", "method": "onCreate"}
  ],
  "validation": null
}
```

> 注意：`source`/`sink` 字段在草案中只是 `chain_proposal`，不能直接成为正式 `sources/sinks`；确定性校验通过后由代码转换成正式字段。`hops` 为结构化路径（字符串 `path` 已废弃，评审 §4.2），校验器逐跳对 `analysis.sqlite3` `call_sites` 表回查；`hypothesis` / `impact_proposal` 是“假设 + 依据”，不是裁决（评审 §4.1）。

### 5.4 三档校验与 Funnel 分支

```text
ExplorerCandidate
  ├─ validated（引用回查通过 + 链可验证）
  │     └─ explorer_promoted → L2 候选（走现有 AI/evidence/decision）
  ├─ partially_validated（引用存在但 dataflow/ taxonomy 不完整）
  │     └─ explorer_partial → 送 explorer_deep_dive 深挖或人工高优，不直接进正式 finding
  └─ unverified（引用不可回查或信息不足）
        └─ explorer_unverified → 人工队列，不占 AI 预算
```

### 5.5 配置草案

```yaml
explorer:
  enabled: false
  max_candidates_per_run: 50
  auto_promote: false
  allow_external_code: true
  prompt_version: explorer/1.0.0
  max_rounds_per_entry: 4        # 检索循环轮数上限（评审 §4.3）
  max_requests_per_entry: 20     # 每入口读码请求上限（评审 §4.3）
  max_requests_per_candidate: 4
  deep_dive_prompt_version: explorer-deep-dive/1.0.0  # partial 候选深挖协议（评审 §7.1 决断）
  call_tree:
    max_depth: 8
    max_nodes: 500

verify:                                # 核验 agent（L2 agent 化演进，见 §2.7）
  enabled: false
  prompt_version: verify/1.0.0
  max_rounds_per_candidate: 4          # 取证循环轮数上限
  max_requests_per_candidate: 12       # 每候选读码请求上限
  fallback_to_single_turn_l2: true     # 失败/预算耗尽回退现有单轮 L2

api_surface:
  enabled: false
  include_binder: true
  include_webview_jsbridge: true

assets:
  enabled: false
  max_concurrent_runs: 2
  data_root: .ai-apk-tracer/assets

batch:
  max_concurrent_runs: 2
  max_ai_calls: 0          # 0 = 沿用 run 级；>0 = batch 总 AI 预算帽（Phase 1 即生效，评审 §4.12）
  max_wall_seconds: 0      # 0 = 不限；>0 = batch 墙钟上限

report:
  allow_executable_poc: false
  require_confirmed_finding: true
```

---

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| 探索 Agent 引入大量低质候选 | 默认关闭、低信任、不占 AI 预算；只在人工队列显示；用 `explorer_hit_rate` 控制 |
| `unverified` 候选被埋没 | 三档校验保留人工队列，按置信度/引用完整度排序；`partially_validated` 送 `explorer_deep_dive` 深挖（占复核预算；探索 / 复核 / 核验三本账分开统计） |
| 预生成全量调用树导致资源爆炸 | 强制 on-demand 有界构建，默认深度 ≤ 8、节点 ≤ 500 |
| 探索候选与规则候选身份冲突 | identity 计算加入 `candidate_source`，不跨源合并 |
| 批量扫描资源耗尽 | 全局并发上限、每个 APK 独立 run、失败隔离可重跑 |
| API 入口表/攻击面产物过期 | 与 decompile/index/rule_prescan 同 run 生成，随 run 版本固化 |
| AI 生成报告引入幻觉修复建议 | 报告草稿必须与确定性 evidence 分离展示；引用回查失败则标记不可信 |
| PoC 被滥用 | 默认只生成非可执行骨架；可执行 PoC 需显式配置 + 合法授权确认 |
| 范围膨胀、影响现有确定性核心 | 全部新能力默认关闭；每个 Phase 有回归门禁，不通过不放行 |

---

## 7. 非目标（本方案不做）

- 不做真实设备动态执行/模糊测试（仍作为外部动态验证，不并入默认流程）。
- 不建设多用户、RBAC、企业审计后台（延续个人版定位）。
- 不让探索 Agent 直接写正式 finding 或最终 `review_status`。
- 不承诺自动生成 100% 可利用 PoC；只生成可审查的骨架。
- 不把“全量 Xiaomi 资产”作为当前仓库默认任务，只提供资产导入/批量接口。

---

## 8. 建议里程碑

| 阶段 | 时间 | 交付物 |
|---|---|---|
| Phase 0 | 第 1 周 | Schema（explorer / deep-dive / verify 核验 / 规则产物 / api_entry_table / attack_surface）、归一化映射表、配置开关、接口设计 |
| Phase 1 | 第 2-3 周 | 资产/批量扫描可用（含 batch 预算帽与迁移机制），前端页面 |
| Phase 2 | 第 4-7 周 | 规则产物导出 + API 入口表 + call_tree on-demand + attack_surface 导出 + 探索轨（含 deep_dive）+ 核验 agent 试点 + funnel 分支 |
| Phase 3 | 第 7-8 周 | 报告生成 + PoC 骨架 + 修复建议 |
| Phase 4 | 第 9-10 周 | 批量评估 + golden 扩展 + 优化门槛 |

> 建议先做 Phase 0 + Phase 1，因为资产批量是后续探索轨和报告批量化的基础；探索轨在单 APK 上验证效果后再推广到批量。
