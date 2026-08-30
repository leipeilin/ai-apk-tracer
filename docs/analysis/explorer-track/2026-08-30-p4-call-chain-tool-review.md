# 任务审查报告：P-4 复合读码工具 get_call_chain（方案审查）

> **任务编号**：P-4
> **审查日期**：2026-08-30
> **审查对象**：`docs/analysis/explorer-track/2026-08-30-p4-call-chain-tool-implementation-plan.md`、`docs/analysis/explorer-track/2026-08-30-p4-call-chain-tool-acceptance-plan.md`
> **审查模型**：glm-5.3-flash（独立子代理，只读审查）
> **状态**：第 1 轮（待处置）

---

## 1. 审查结论摘要

**总体结论：方向正确、数据可信，但"价值验收"建立在未经核实的前提上，且实现层接线（配置可达、taxonomy 数据源、解压容错、预算三处同源）存在缺口，修订后方可进入实施。**

- **方案价值主张成立但被高估**：v2 把 sink 判定移到 pending 叶子这一修正方向正确（实测 sink 调用边 100%/近 100% pending，v1 沿 resolved 展开必然到不了 sink）。但真实索引上 pending 边的 taxonomy 命中率仅 **0.79%**（2157/274163），按组件 lifecycle 方法以 depth=2/width=3 模拟，仅 **8.14%**（35/430）的入口能产出 ★SINK 叶子——方案未写入任何基线数字，验收却按"必然出现 ★SINK"设计（R-1）。
- **方案存在两处与代码事实直接冲突的示例/验收项**：`getStringExtra`（§2.2 点名的"典型 sink"）与 `getString`（§3.2 示例的 ★SINK 行）在 `rules/sink_taxonomy/versions.yaml` 中**根本不存在**，正确实现下必然"未命中 taxonomy"（R-2）。
- **锚点真实性整体很高**：9 个代码锚点全部属实（行号精确）；§2.2 全部索引实测数据经独立只读复现**逐字节一致**（274163/106183/4261、五张 sink 边表、31254/80831=38.7%）——作者确系实测，非凭记忆书写。
- **审查特别关注项结论**：`receiver_type` 的 smali 形态规范化**已在匹配器内部处理**（`sink_taxonomy.py:112-124` + `:139` 内部调用），pending 边传 `Landroid/content/Context;` 亦可命中 → 该项无系统性失效风险（见认可项 1）；但 **receiver 为空导致的宽松命中占命中总量 20.58%**，构成误标风险（R-3）。
- **审查方法**：①9 个代码锚点逐条读码核验；②真实索引 `analysis.sqlite3` 只读（uri mode=ro）独立复现 §2.2 全部数据，并追加命中率/可达率/体积分布三组独立测算；③沿"协议→schema→registry→prompt→配置→测试"逐环核验演化路径与既有回归锚点。

问题合计 **17 条**：关键 2 / 高 4 / 中 7 / 低 4。

### 核验复现命令（可回查）

```sh
# ① 边状态分布 + sink 边分布 + 可展开性（复现实施方案 §2.2 全部数字）
python3 -c "import sqlite3;p='.ai-apk-tracer/runs/20260829T145430Z_fc0d0e01d0e0_868521fd/index/analysis.sqlite3';c=sqlite3.connect('file:%s?mode=ro'%p,uri=True);print(c.execute('SELECT resolve_status,COUNT(*) FROM call_sites GROUP BY 1 ORDER BY 2 DESC').fetchall());print(c.execute(\"SELECT method_name,resolve_status,COUNT(*) FROM call_sites WHERE method_name IN ('getStringExtra','startService','bindService','execSQL','query') GROUP BY 1,2 ORDER BY 1,3 DESC\").fetchall());print(c.execute(\"SELECT COUNT(*) FROM (SELECT DISTINCT method_id FROM call_sites WHERE resolve_status='resolved')\").fetchone(),c.execute('SELECT COUNT(*) FROM methods').fetchone())"
# 实测输出（与方案完全一致）：
# [('pending',274163),('resolved',106183),('ambiguous',4261)]
# [('bindService','pending',65),('execSQL','pending',29),('getStringExtra','pending',243),
#  ('query','pending',105),('query','resolved',7),('startService','pending',29)]
# (31254,) (80831,)          → 31254/80831 = 38.67%

# ② pending 边 sink 命中率（方案未测；backend/.venv/bin/python，复用 sink_matches_taxonomy）
# 结果：pending 边命中 2157 / 274163 = 0.79%；其中 receiver 为空的宽松命中 444 条 = 20.58%

# ③ 入口可达性模拟（depth=2/width=3，sink-first 排序，430 个组件 lifecycle 方法）
# 结果：35/430 = 8.14% 出现 ★SINK 叶子；直接命中 23/430 = 5.35%；平均展开节点 2.7

# ④ getStringExtra 是否在 taxonomy 中
grep -n "getString" rules/sink_taxonomy/versions.yaml   # 无任何输出 → 不存在
```

---

## 2. 锚点真实性核验

| 方案声称（出处） | 代码/数据事实 | 结论 |
|---|---|---|
| 读码操作枚举 4 操作 `ai_models.py:323`（§2.1） | `ai_models.py:323` 确为 `operation: Literal["get_method_body","get_callees","get_callers","search_symbol"]` | ✅ 属实 |
| `explorer.py:970 dispatch_read` 模块级公共函数、**两轨共用**（§2.1/§5） | `explorer.py:970-991` 确为模块级函数；`verify_agent.py:28-31` 导入并在 `:461/:476` 调用 → **dispatch 共用属实**；但预算截断不共用（见下条 R-8） | ⚠️ 部分属实 |
| `explorer.py:53` 8192 上限、`:817` 执行截断；v1 的 12K 会被静默截断（§2.1/§4.2） | `explorer.py:53` `_MAX_CONTEXT_BYTES_PER_REQUEST = 8*1024`；`:816-818` 对 `json.dumps(payload, ensure_ascii=False)` 按字符数截断 → 12K 预算确被截到 8192 | ✅ 属实 |
| 上述截断"对所有 operation 统一"（§4.2） | `explorer.py:731-733`（深挖路径）是**第二处**同常量截断；`verify_agent.py:39/451/482` 用独立常量 `_MAX_SEGMENT_BYTES=8192` → 共三处 | ⚠️ 不完整（R-8） |
| `call_tree.py:36 MAX_BODY_LINES = 240`（§2.1） | `call_tree.py:36` 确为 `MAX_BODY_LINES = 240` | ✅ 属实 |
| `call_tree.py:168 get_seed_hops` 为"resolved 边直查先例"（§2.1） | `:168-197` 确为 sqlite 直查 `call_sites WHERE resolve_status='resolved'`；但已有更合适的批量 API `index_store.py:569-582 get_call_sites_for_methods`（含 resolve_status/receiver_type/method_name/**已解压 arguments**，内部分片 10000） | ⚠️ 先例选错（R-12） |
| `sink_taxonomy.py:127 sink_matches_taxonomy(method_name, receiver_type, entries)`，"输入恰为 pending 边已有字段 → 零新增判定逻辑"（§2.1） | `:127-156` 签名一致；`:139` 内部调用 `normalize_receiver_type`（`:112-124`，剥 smali `L...;` 含 `/`→`.`、剥泛型）→ smali 形态已在匹配器内处理，无系统性失效 | ✅ 属实（见认可项 1） |
| `index_store.py:21-37 _pack_json/_load_json`，"arguments 摘要需解压"（§2.1） | `:21-37` 属实；`call_sites.arguments_json` 实测 `typeof=blob`（zlib），样本 `startService` → `['intent']` | ✅ 属实 |
| N-7"解压失败/旧版明文 → 复用 `_load_json` 语义降级为空并标注" | `_load_json` 仅 `except zlib.error`，损坏 JSON 抛 `JSONDecodeError`（**ValueError 子类**）→ 被 `explorer.py:989` 捕获为 `{"not_found": target}`，**整链丢失**，非"参数降级" | ❌ 不成立（R-6） |
| 配置挂点 `config.py:206 ExplorerSettings` 新增 6 参数（§4.1/§5） | `config.py:206` 属实；但 `CallTreeService.__init__` 接收的是 `CallTreeSettings`（`call_tree.py:39-42`，`config.py:199-203` 仅 max_depth/max_nodes），orchestrator 在 `:809/:1190` 传 `settings.explorer.call_tree` → 参数**到不了实现层** | ⚠️ 接线缺失（R-4） |
| pending 274,163 / 71.3%、resolved 106,183 / 27.6%、ambiguous 4,261 / 1.1%，总 38.5 万（§2.2） | 实测 `[('pending',274163),('resolved',106183),('ambiguous',4261)]`，总 384,607 | ✅ 完全一致 |
| sink 调用边分布：getStringExtra 243/startService 29/bindService 65/execSQL 29 全部 pending；query pending 105 / resolved 7（§2.2） | 复现输出逐项一致 | ✅ 完全一致 |
| 仅 38.7% 方法（31,254/80,831）拥有 ≥1 resolved 边（§2.2） | 实测 (31254,) (80831,) = 38.67% | ✅ 属实 |
| pending 边实测样本 `method_name=startService / receiver_type=android.content.Context / arguments(zlib) / start_line=141`（§2.2） | 实测 `('startService','android.content.Context','blob',141)` | ✅ 属实 |
| "method_name + receiver_type 正是入参 → **pending 边足以判定 sink**"（§2.2 结论） | 字段确具备；但实测仅 **2157/274163 = 0.79%** 的 pending 边能命中 taxonomy；且 `getStringExtra` 无 taxonomy 条目 → 243 条边全部"未命中" | ⚠️ 结论过强（R-2/R-3） |
| §3.2 响应示例 `getString("url") [pending] ★SINK data_disclosure` | `grep "getString" rules/sink_taxonomy/versions.yaml` 无输出 → 无 `getString` 条目，正确实现下应为"未命中 taxonomy" | ❌ 示例不实（R-2） |
| §3.4"prompt 1.1.0 新目录 + registry 注册，1.0.0 历史产物不受影响" | `prompts/registry.yaml:357-372`：explorer@1.0.0 与 1.1.0 将**共用** `ai_explorer_observation.schema.json`（`output_schema_file`）；`prompt_registry.py:261/263` 校验 schema sha 与模型字节一致 | ⚠️ 不成立（R-10） |

---

## 3. 问题清单（按严重度排序）

**【R-1】【关键】P4-5"真实索引 sink 可达性"不可判定，且可达率基线仅 8.14%——按现文案约 92% 的入口必然失败**

证据：
- 实测（见 §1 命令③④）：全库仅 **1,273/80,831（1.57%）** 方法拥有 ≥1 条直接 pending sink 命中；depth=2 无宽度限制累积 3.86%，depth=6 饱和 5.08%。
- 以 237 个 API entry 的组件 lifecycle 方法（430 个）为根，按 depth=2/width=3 + sink-first 排序模拟：**仅 35/430 = 8.14%** 输出含 ★SINK 叶子（直接命中 5.35%），平均只展开 **2.7 个节点**。
- 验收 `P4-5`：仅要求"对真实入口方法调用 `get_call_chain` → 输出链中出现 ★SINK 标记的 pending 叶子"，**未指定 entry method_id、未给出期望的 (method_name, taxonomy)**。

修订建议：
1. P4-5 改为**固定样本断言**：先用只读脚本枚举出 ≥5 个真实 entry method_id（优先含 `startService`/`execSQL`/`bindService` pending 边的方法），把 `method_id` 与期望的 `(method_name, taxonomy)` 逐条写进验收表，断言响应含对应 ★SINK 行（例：`startService@android.content.Context → connection_session_control`，实测 24/29 命中）；
2. 把 **1.57%（直接命中）与 8.14%（depth2/width3）** 写入实施方案作为价值基线，P4-14 的"链尾 sink 到达率提升"以该基线做前后对比，而非绝对断言；
3. 若坚持随机入口，则 P4-5 断言降级为"响应结构完整（三类边分类 + gaps 标注正确 + 无异常）"，★SINK 出现率作为**观测指标**记录，不作通过条件。

---

**【R-2】【关键】§2.2 与 P4-5 点名的 `getStringExtra`、§3.2 示例的 `getString` 在 sink taxonomy 中不存在，与"★SINK 标记"预期直接矛盾**

证据：
- `grep -n "getString" rules/sink_taxonomy/versions.yaml` → **无任何输出**（taxonomy 共 82 条目 / 75 个方法名，实测）。
- 实测 `getStringExtra` 全部 243 条 pending 边 receiver 分布：`('<empty>',55)`、`('android.content.Intent',186)`、`('com.xiaomi.mipush.sdk.or',2)`，`sink_matches_taxonomy` 全部返回 `None`。
- 实施方案 §2.2 将 `getStringExtra 243（100% pending）`列为"典型 sink（决定性）"，验收 P4-5 明确写"对应实测 100% pending 的 `startService`/`getStringExtra`/`execSQL` 等"。

修订建议：
1. 二选一：①从 §2.2 表与 P4-5 中移除 `getStringExtra`，只保留真实可命中的 `startService`(24/29)、`bindService`(56/65)、`execSQL`(29/29)、`query`(29/112)；②若认为其确属 sink，则按 T2.9 升级闭环在 `versions.yaml` 增补 `getStringExtra`（建议 `receiver_leaves: [Intent]`），**并评估对既有 custom_sink 判定与 M2 指标的影响**；
2. §3.2 示例改为实测可命中形态：`└ :52 startService(intent) [pending] android.content.Context ★SINK connection_session_control`；
3. P4-3 的 mock taxonomy fixture 必须与真实 `versions.yaml` 条目一致（`startService` → `receiver_leaves: [Activity, Context]`，`versions.yaml:53-56`），避免单测通过而真实索引不通过。

---

**【R-3】【高】pending 边 sink 命中率仅 0.79%，且 20.58% 命中依赖 receiver 缺失的"宽松命中"（D2），★SINK 存在系统性误标风险，方案无量化、无降级口径**

证据：
- 实测 pending 边命中 **2157/274163 = 0.79%**；按每方法约 3.4 条 pending 边计，单方法期望命中 ≈ 0.027。
- 命中中 **444 条（20.58%）receiver_type 为空**，依赖 `sink_taxonomy.py:144-145` 的"无 receiver 证据 → 宽松命中"放行；TOP 误标嫌疑：`execute→data_disclosure 76 条`、`remove→persistent_state_write 40`、`delete→database_mutation 35`、`newInstance→ui_navigation 34`、`append→file_mutation 21`、`write→data_disclosure 20`。
- 索引中 receiver 为空的 pending 边共 25,702 条（占 pending 9.4%）。

修订建议：
1. 摘要行区分两种命中：receiver 证据充分的标 `★SINK <taxonomy>`，receiver 缺失的标 `★SINK? <taxonomy>（receiver 缺失·宽松命中）`，gaps 区统计宽松命中条数；
2. 新增配置位 `call_chain_loose_receiver: bool = False`——关闭时，receiver 为空且方法名属通用词表（`execute/remove/delete/append/clear/write/put*/apply/commit/newInstance/openConnection` 等）的 pending 边不标 ★SINK；
3. 验收 P4-4 增补**正例**：receiver 为空 + 宽松命中开关 True/False 两态的行为断言；
4. 实施方案补写"pending 边 sink 命中基线 0.79%、宽松命中占比 20.58%"，避免后续把低命中率误判为实现缺陷。

---

**【R-4】【高】6 个配置参数挂在 `ExplorerSettings`，但实现层 `CallTreeService` 只读 `CallTreeSettings`——配置无法到达实现，方案缺接线定义**

证据：`call_tree.py:39-42`（`settings: CallTreeSettings`）；`config.py:199-203`（CallTreeSettings 仅 max_depth/max_nodes）；`orchestrator.py:809` 与 `:1190`（`CallTreeService(run_dir, reader, ...call_tree)`）；测试侧 4 处构造（`test_call_tree.py:108`、`test_explorer.py:72`、`test_verify_agent.py:59`、`test_sink_taxonomy.py:441`）。实施方案 §4.1 明确写"ExplorerSettings 新增（config.py:206）"，§5 又写"服务 `call_tree.py` 新增 `get_call_chain`"，两者之间无传参定义。

修订建议（二选一，写入实施方案 §5）：
1. 6 参数改挂 `CallTreeSettings`（`config.py:199`），两轨同源、构造签名零改动；或
2. 保留 `ExplorerSettings`，但定义显式签名
   `get_call_chain(self, method_id, *, depth, max_hops, branch_width, char_budget, sink_first, include_pending, sink_entries) -> dict`，由调用方（explorer 读码分发 / verify `_execute_reads`）注入；
3. 明确 Verify 轨（`orchestrator.py:809`）使用哪一份配置，避免两轨参数漂移。

---

**【R-5】【高】sink 判定的 taxonomy 数据源未接线：`CallTreeService` 完全不感知 taxonomy，而 taxonomy 目前只在 orchestrator 归一化阶段加载**

证据：`orchestrator.py:1231-1251` 仅在 `validate_explorer_candidates` 前 `load_sink_taxonomy`；`call_tree.py` 全文无 `sink_taxonomy` 引用；验收 N-6 要求"`load_sink_taxonomy` 返回 None → sink 判定跳过"（`load_sink_taxonomy` 返回 None 的既有语义见 `sink_taxonomy.py:61`、`test_sink_taxonomy.py:142`）。实施方案 §5 只写"复用 `sink_matches_taxonomy`，零新增判定逻辑"，未说明 entries 从哪来。

修订建议：
1. `CallTreeService.__init__` 增加可选参数 `sink_entries: Sequence[SinkTaxonomyEntry] | None = None`（默认 None = 禁用，4 处测试构造零改动），orchestrator 在 `:809` 与 `:1190` 注入已加载的 `taxonomy_entries`；或 `get_call_chain(..., sink_entries=...)` 由调用方传入；
2. N-6 用例直接传 `None` 断言"无 ★SINK 标记、不抛"；
3. §5 表格增补一行"数据源接线：orchestrator → CallTreeService（构造注入）"。

---

**【R-6】【高】N-7"参数解压失败降级"不成立：`_load_json` 只吞 `zlib.error`，损坏数据抛出 `JSONDecodeError`（ValueError 子类）会被 `dispatch_read` 吞成 `not_found`，导致整条链丢失**

证据：`index_store.py:27-37`（`except zlib.error: pass` 后直接 `json.loads(raw)`）；`explorer.py:989-991`（`except (sqlite3.Error, ValueError, TypeError, KeyError)` → `return {"not_found": target}`）；`json.JSONDecodeError` 是 `ValueError` 子类。

修订建议：
1. 在 `call_tree` 内**逐边** `try/except (zlib.error, json.JSONDecodeError, UnicodeDecodeError, TypeError)`，失败时该边 `arguments=[]` 并标注 `arguments_unavailable`，节点与其余边照常输出；
2. N-7 断言改为"该边仍出现在响应中且带降级标记，且**整个响应不是 `not_found`**"；
3. 与 R-12 合并处置：直接复用 `reader.get_call_sites_for_methods`（已解压），在其调用点外包逐边容错。

---

**【R-7】【中】实现细节不足以直接编码：无函数签名、无 BFS 数据结构/伪代码、无返回体字段契约、无 target 解析口径**

证据：实施方案 §3.1-§3.3 仅有分类表与一段示例文本；§5 写"`call_tree.py` 新增 `get_call_chain(method_id, options)`"，`options` 未定义；验收 P4-2/P4-6/P4-7/P4-8 需断言"摘要行/gaps 结构/层级粒度/截断标注"，但返回体字段未定义，无法写出确定性断言。

修订建议：补齐四项——
1. 签名：`get_call_chain(self, method_id: str, *, depth, max_hops, branch_width, char_budget, sink_first, include_pending, sink_entries) -> dict[str, Any]`；
2. 返回体契约（建议）：`{"root": {...}, "nodes": [{"level","method_id","path","lines","body","enter_line","edges":[{"kind":"resolved|pending|ambiguous","line","method_name","receiver_type","arguments","sink": {...}|None}]}], "sink_leaves": [...], "gaps": [...], "truncated": bool, "rendered": str}`（`rendered` 为 §3.2 的文本块，供模型阅读；结构化字段供测试断言）；
3. BFS 伪代码：队列元素 `(method_id, level, enter_line)`、`visited` 集合、每跳的预算检查顺序（先深度/跳数 → 再预算，超额即标注并停止）、一跳前瞻的判定方式（对候选 resolved 目标查其 pending 边是否命中 sink）；
4. `target` 解析口径：`method_id` 直查（对齐 `call_tree.py:120-131` 返回 `None` → `dispatch_read` 映射 `not_found`），不做格式重建（沿用 call_tree 顶部"列值直取"硬约束）。

---

**【R-8】【中】per-operation 上限只改 `explorer.py:817`，Verify 轨与 Explorer 深挖路径不覆盖，"两轨同时受益/预算空间匹配"表述失真**

证据：`verify_agent.py:39` `_MAX_SEGMENT_BYTES = 8*1024`，`:451`（首轮 code_context）与 `:482`（取证读码）各自独立截断；`explorer.py:731-733`（深挖路径）同常量截断。方案 §2.1 只列 `:817`，§4.2 只提 `:817`。

修订建议：
1. 把 operation-aware 上限收敛为一个共享函数（如 `read_result_char_limit(operation) -> int`，置于 `explorer.py` 模块级并供 `verify_agent` 导入），`explorer.py:731`、`explorer.py:817`、`verify_agent.py:451`、`verify_agent.py:482` 四处统一调用；
2. P4-10 增补 Verify 轨断言：Verify 侧 `get_call_chain` 同样 ≤ 16_384，旧 4 操作两轨均仍 8192（零回归）；
3. §2.1 表格结论改为"dispatch 共用；预算上限需四处同源"。

---

**【R-9】【中】协议/schema 挂点写错文件，且 registry 注册缺 sha256 同步步骤**

证据：`operation` enum 实际位于 `schemas/ai_explorer_observation.schema.json` 的 `$defs/ReadRequest/properties/operation`（`enum: ["get_method_body","get_callees","get_callers","search_symbol"]`），**不在** `ai_explorer_input.schema.json`（P4-1 写的是后者）；`prompts/registry.yaml:357-372` 的 explorer 条目字段为 `id/version/system_file/user_file/.../template_sha256/schema_sha256`，**无 `protocol_version` 字段**；`prompt_registry.py:229-231` 校验模板 sha、`:259-264` 校验 schema sha 且要求 schema 字节与 Pydantic 模型一致。

修订建议：
1. P4-1 改为断言 `schemas/ai_explorer_observation.schema.json` 的 `$defs.ReadRequest.properties.operation.enum` 含 5 项；
2. 把方案中的"`protocol_version` 1.0.0→1.1.0"改为"registry 版本 `explorer@1.1.0`"（与 `config.py:213 prompt_version` 一致）；
3. P4-1 增加"执行 `scripts/sync-ai-protocol.py` 后 `prompts/registry.yaml` 的 `template_sha256`/`schema_sha256` 已更新且全量 pytest 通过"（`sync-ai-protocol.py:44-70` 负责重算；不做则 `prompt_registry` 加载即抛错）；
4. 明确 1.1.0 需同时提供 `system.md` **与** `user.md`（`registry.yaml:64-67` 校验路径精确匹配，缺一不可）。

---

**【R-10】【中】"1.0.0 历史产物不受影响"不成立：observation schema 被两个版本共用，enum 扩展会同步放大 1.0.0 协议允许的操作集，且 1.0.0 的 `schema_sha256` 必须改写**

证据：`registry.yaml:365-372`（explorer@1.0.0 的 `output_schema_file: ai_explorer_observation.schema.json`、`schema_sha256.output: bf1b70cc…`）；`prompt_registry.py:261` `_require_hash`、`:263` `raw != expected` 即抛 `PromptRegistryError`。

修订建议（三选一并写入方案与 P4-12）：
1. **接受共享**：明示"1.0.0 允许模型使用 `get_call_chain`（服务端实现已存在），属可接受的操作面放大"，P4-12 断言相应改为"1.0.0 prompt 文本与行为零变化；操作集放大已确认"；
2. **冻结 1.0.0**：为 1.1.0 复制独立输出模型与 schema 文件（如 `ai_explorer_observation_1_1_0.schema.json`），registry 分别指向，1.0.0 字面冻结（代价：registry 条目与 AI_MODEL_REGISTRY 增一个模型）；
3. **服务端兜底**：1.0.0 协议下收到 `get_call_chain` 时降级为 `not_found` 或按四操作语义处理（需在 prompt_version 可获得的调用点实现）。

---

**【R-11】【中】既有"四操作"回归锚点未列入变更清单，1.1.0 prompt 存在约束漂移风险**

证据：`backend/tests/test_explorer_protocol.py:142-157` `test_read_request_four_operations`（docstring 明写"评审 R-2 决断回归锚定：操作面恒四操作"）；`:196-249` `test_prompt_declares_required_and_enums` 断言 1.0.0 `system.md` 含 `get_method_body/get_callees/get_callers/search_symbol` 及 F2 sink 九类语义、M4 骨架链、P-3 空转轮等一长串累计约束。

修订建议：
1. P4-12 明确列出需同步更新的测试：`test_read_request_four_operations` 改名（如 `test_read_request_operations`）+ 改 docstring（五操作，注明"评审 R-2 的四操作决断由 P-4 显式解除"）+ 增加 `get_call_chain` 正向用例；
2. P4-13 断言升级为"1.1.0 `system.md` **⊇** 1.0.0 全部既有约束 token"——直接复用 `:204-249` 的 token 断言循环对两个文件各跑一遍，防止 1.1.0 复制时丢失 F2/M4/P-3 累计约束；
3. 实施方案 §6 记录"R-2 四操作决断的解除与理由"。

---

**【R-12】【中】复用点选错：`get_seed_hops` 的裸 SQL 直查不是最佳先例，已有批量 API `reader.get_call_sites_for_methods`**

证据：`index_store.py:569-582` `get_call_sites_for_methods(method_ids)` 一次返回 `resolve_status/receiver_type/method_name/method_descriptor/resolved_target_id/arguments（已 `_load_json` 解压）/start_line/end_line/expression_kind`，内部按 10_000 分片——正是 BFS 分层所需的全部字段；`call_tree.py:168-197 get_seed_hops` 是"单方法 + `ORDER BY start_line LIMIT n` 窄投影"，适用于 seed 第一跳，不适用于分层批量展开。

修订建议：
1. BFS 每层改用 `self._reader.get_call_sites_for_methods(frontier_ids)`，一层一次查询（利用其分片的批量优势）；`get_seed_hops` 保持不动；
2. §2.1 表格与 §5"服务"行同步改写，删去"沿用 `get_seed_hops` 同模式"；
3. 与 R-6 合并：N-7 的解压容错落在该调用的逐边 `try/except` 上，无需跨模块引用私有 `_load_json`。

---

**【R-13】【中】预算口径未与既有预算机制对齐，且 14_000 字符预算存在被 16_384 二次截断的隐患**

证据：`explorer.py:59` `_MAX_EXPLORE_CONTEXT_CHARS = 40000`、`:65` `_MAX_DEEP_DIVE_CONTEXT_CHARS = 9500`、`verify_agent.py:42` `_MAX_VERIFY_CONTEXT_CHARS = 9500`；`context_budget.py:36-41 estimate_tokens`（UTF-8 字节/4 的既有 token 估算）；`explorer.py:816-818` 的截断是**对 `json.dumps(payload, ensure_ascii=False)` 后的字符串按字符数**比较——`payload` 内含 `rendered` 文本块时，换行转义（`\n`→2 字符）、引号/反斜杠转义会使其长度**大于**渲染字符数。

修订建议：
1. 明确"预算以渲染后字符计"，并把 per-operation 上限设为 `char_budget × 1.35`（≈18_900）留余量；或在 `call_tree` 内以 `len(json.dumps(payload, ensure_ascii=False))` 自校验，超限时回退裁剪 `rendered`；
2. P4-10 增加最坏夹具断言：`len(json.dumps(payload, ensure_ascii=False)) <= _MAX_CONTEXT_BYTES_BY_OPERATION["get_call_chain"]`；
3. 说明单次 14K 占跨轮 40K 的 **35%**，是否需要为 `get_call_chain` 设置轮内配额（如"每轮最多 1 次"）或维持现状并写明理由；
4. 说明与 `estimate_tokens` 的关系（14_000 字符 ≈ 3.5K tokens），避免另起一套成本口径。

---

**【R-14】【中】"两个 bool 零成本回退到 v1 行为"不成立**

证据：实施方案 §4.1"置 False 即退回 v1 行为（纯 resolved 宽度优先）"、§6 与验收 §5 同述；但 v1 从未落地（`call_tree.py` 无 `get_call_chain`），两开关只影响**输出选择**（是否输出 pending 叶子、是否 sink 优先排序），不还原 v1 的 12_000 预算、无 per-operation 上限、无 sink 摘要等旧形态。

修订建议：
1. 表述改为两级回退：**开关级**=关闭 pending 叶子与 sink 优先排序（降级为 v1 的纯 resolved 宽度优先输出形态，用于 A/B 对照与应急，P4-6 已覆盖）；**代码级**=删除 `call_tree.get_call_chain` 与 `dispatch_read` 分支（验收 §5 第 4 条已列，真正意义上的"回到 P-4 之前"）；
2. 验收 §5 明确两级回退的差异与各自验证方式，删除"零成本回退到 v1 行为"的绝对表述。

---

**【R-15】【低】缺响应体积与价值的量化预估，痛点缓解论证只到"轮次 O(1)"**

证据：实测方法体行数 p50=4 / p90=22 / p99=94，**仅 0.15%** 超 240 行；每方法调用边 p50=3 / p90=15 / 均值 7.01；depth=2/width=3 平均仅展开 2.7 个节点（实测）。

修订建议：
1. 补一节"体积/价值预估"：L0 全量（p50 仅 4 行）+ 约 3.4 条 pending 摘要 + 约 2.7 个展开节点 → 典型响应 1–3K 字符（远低于 14K 预算），说明**上下文压力主要来自多轮往返与重复全方法体，而非单体量**，本工具的收益点成立；
2. 同时说明 `branch_width=3` 对 p90=15 条边的方法会丢弃约 80% 的边（"另有 N 条未展开"是常态），需在 prompt 1.1.0 中教会模型：高扇出方法先 `get_call_chain` 定位方向，再对具体分支用 `get_callees`/`get_method_body` 追深；
3. P4-14 增加观测指标：响应字符数 p50/p90、单入口轮次、"另有 N 条未展开"出现率。

---

**【R-16】【低】prompt 1.1.0 的改动点未落到具体行**

证据：`prompts/explorer/1.0.0/system.md:39`（`- operation（string，必填）：仅允许 "get_method_body" / "get_callees" / "get_callers" / "search_symbol"。`）与 `:61-63`（`## 读码操作（read_requests.operation，仅此四种）` 段）是两处需要同步修改的位置；实施方案 §5 只写"system.md（读码工具说明区）"。

修订建议：§5 Prompt 行写明"`system.md:39` 约束行 + `:61-63` 读码操作段 + 新增三类边语义/★SINK 标记读法/'≥2 层深链优先'引导语"；P4-13 断言按位置点名这些 token（如含 `get_call_chain`、`★SINK`、`pending`、`ambiguous`）。

---

**【R-17】【低】`schemas/config.schema.json` 同步未列入变更清单**

证据：`schemas/config.schema.json` 含 `explorer.max_rounds_per_entry` 等字段（`test_config.py:109-121` 断言 explorer 段默认值）；实施方案 §5 配置行只列 `config/default.yaml`；T2.9 评审 R-10 亦要求 schema/default.yaml 同步注记。

修订建议：§5 配置行补 `schemas/config.schema.json`（6 个新字段及默认值/上下界）；P4-1 增加"schema 的 `explorer` 段含 6 个新字段且默认值与 `config.py` 一致"的断言。

> 严重度定义：
> - 关键：会导致验收落空或无法实施（结构缺失、方案自相矛盾、数据源不可达）
> - 高：明显缺陷，需修订后才可实施
> - 中：不完整或存在歧义，应补充
> - 低：建议性/表述性问题，可接受不修

---

## 4. 认可项

1. **`receiver_type` smali 规范化已在匹配器内部处理，无系统性失效风险**：`sink_taxonomy.py:112-124 normalize_receiver_type` 剥 `L...;`（含 `/`→`.`）与泛型，`sink_matches_taxonomy` 在 `:139` 内部调用 → pending 边传 `Landroid/content/Context;` 亦可命中；实测索引中 1,064 条 smali 形态 pending 边因此仍可判定。方案"零新增判定逻辑"成立，**实施时不得在调用侧再做一次规范化（避免双剥）**。
2. **§2.2 全部索引实测数据经独立复现完全一致**（274163/106183/4261；五张 sink 边表；31254/80831=38.67%）——数据可信，非凭记忆书写，这是本方案最扎实的部分。
3. **v2 相对 v1 的方向性修正正确**：sink 判定放在 pending 叶子而非 resolved 目标名。实测 sink 调用边 100%/近 100% pending，v1 "沿 resolved 展开"确实到不了 sink——审查结论成立，不应回退该修正。
4. **§3.4 四项不变项设计合理**：visited 环防护、1 次 `read_requests` 计费（`explorer.py:813` / `verify_agent.py:479`，省轮次激励）、`depth`/`max_hops` 不进协议（防模型滥用、服务端封顶）、版本化隔离。
5. **分层粒度与既有预算对齐**：L0 完整复用 `MAX_BODY_LINES=240`（`call_tree.py:36`），L1 40 行节选与 L2+ 摘要行为新增但有界，超限显式标注（对齐 call_tree 顶部"截断显式标注不静默"约定）。
6. **不修调用图索引、如实标注 pending/ambiguous（§1 非范围）**——边界克制，避免把 P-4 放大成索引重构。
7. **N-1~N-9 负例覆盖面基本齐全**：根不存在、无边叶子、全 pending、全 ambiguous、预算耗尽、taxonomy 禁用、arguments 解压、环+深度叠加、索引缺失均覆盖（解压路径的实现口径见 R-6）。
8. **回退分层思路完整**（行为开关 / 协议 / prompt registry / 代码删除四层），仅需修正"零成本回退 v1"的表述（R-14）。

---

## 5. 边界检查表

| 边界 | 结论 |
|---|---|
| 兼容 | 旧 4 操作行为不变（dispatch 分支式扩展，`explorer.py:980-991`）；**但 observation schema 被 1.0.0/1.1.0 共用，enum 扩展会放大 1.0.0 允许的操作集**（R-10）；`test_read_request_four_operations`（R-2 决断锚点）需显式解除（R-11） |
| 回滚 | 行为级两 bool 只回退输出形态、非 v1 全貌（R-14）；代码级删除 `get_call_chain` + dispatch 分支可回到现状；prompt 层 registry 停用 1.1.0 可行，但须先跑 `sync-ai-protocol.py` 否则 hash 校验抛错（R-9） |
| 异常 | N-1~N-9 覆盖较全；**N-7 解压降级路径不成立**（损坏 JSON 会被 `dispatch_read` 吞成 not_found，整链丢失）（R-6）；**receiver 缺失的宽松命中无负例**（R-3）；索引缺失降级可对齐 `get_seed_hops`（`call_tree.py:186-188`）的既有约定 |
| 回归 | 基线 1370 只增不减可满足；风险集中在 `test_explorer_protocol.py:142/196`、`test_config.py:175`（registry 版本一致性）与 config schema 同步（R-11/R-17）；config 参数若改挂 `CallTreeSettings`，需核对 `test_config.py:213` 等既有断言（R-4） |
| 数据质量 | §2.2 数据可复现 ✅；**但 `getStringExtra`/`getString` 不在 taxonomy**（R-2）、**pending 命中基线 0.79% 与入口可达率 8.14% 未写入方案**（R-1）、**pending receiver 空值 25,702 条（9.4%）影响判定口径**（R-3）——三者均应在方案中量化声明，避免验收期误判 |

---

## 6. 处置记录（主代理回填，2026-08-30）

> 主代理已对关键/高严重度项独立复现核实：pending 边命中率 **0.79%**（2157/274163）、
> 宽松命中占比 **20.6%**（444/2157）、`CallTreeService` 接收 `CallTreeSettings`（`call_tree.py:39`）、
> `call_tree.py` 全文无 taxonomy 引用——四条与审查结论**一致**，故予采纳。
> R-2 经核实为**部分属实**：`getStringExtra`/`getString`/`setResult` 确不在 taxonomy，
> 但 `startService`/`bindService`/`execSQL`/`query` **均在其中**（且其调用边 100% pending），
> v2 核心论据成立，仅示例与验收预期需修正。

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 关键 | **采纳**：P4-5 由"随机真实入口"改为**固定样本断言**（主代理实测枚举 6 个真实 method_id 与期望 taxonomy 三元组，写入验收表）；1.57%/8.14% 作为**基线**写入方案，P4-14 以基线对比而非绝对断言 | 实施方案 §0/§2.3；验收 P4-5 固定样本表、P4-14 |
| R-2 | 关键 | **部分采纳**：①采纳——§2.2 表与 P4-5 移除 `getStringExtra`/`getString`，改用实测可命中的 `startService`/`bindService`/`execSQL`/`query`/`delete`；§3.2 示例改为 `execSQL` 真实形态；P4-3 fixture 对齐 `versions.yaml:53-56`；②**拒绝**——不在 P-4 内增补 `getStringExtra` 到 taxonomy（属 T2.9 升级闭环独立任务，且会影响既有 custom_sink 判定与 M2 指标，超出 P-4 范围） | 实施方案 §1 非范围、§2.2、§3.2；验收 P4-3/P4-5 |
| R-3 | 高 | **采纳**：sink 标记两态（`★SINK` / `★SINK?（receiver 缺失·宽松命中）`）；新增 `call_chain_loose_receiver: bool = False`（默认关闭 + 通用名黑名单）；P4-4 增补两态正例；0.79%/20.58% 写入方案基线 | 实施方案 §3.2/§4.1/§2.3；验收 P4-4 |
| R-4 | 高 | **采纳**：7 个参数由 `ExplorerSettings` **改挂 `CallTreeSettings`**（`config.py:199`），两轨同源、构造签名零改动；实施时核对 `test_config.py:213` 等既有断言 | 实施方案 §2.1/§4.1/§5 |
| R-5 | 高 | **采纳**：`CallTreeService.__init__` 增可选 `sink_entries: Sequence[SinkTaxonomyEntry] \| None = None`（默认禁用，4 处测试构造零改动），orchestrator `:809`/`:1190` 注入；§5 增"数据源接线"行 | 实施方案 §5；验收 N-6 |
| R-6 | 高 | **采纳**：BFS 内**逐边** `try/except (zlib.error, JSONDecodeError, UnicodeDecodeError, TypeError)`，失败置 `arguments=[]` + `arguments_unavailable=True`，节点与其余边照常；N-7 断言改为"响应**不是** `not_found`" | 实施方案 §3.4；验收 N-7 |
| R-7 | 中 | **采纳**：补齐函数签名（9 个 keyword 参数）、返回体字段契约（`root/nodes/edges/sink_leaves/gaps/truncated/rendered`，注明 `rendered` 与结构化字段同源）、BFS 伪代码（队列/visited/hops/预算检查顺序/一跳前瞻）、`target` 列值直查口径 | 实施方案 §3.4 |
| R-8 | 中 | **采纳**：收敛为共享函数 `read_result_char_limit(operation)`（`explorer.py` 模块级，`verify_agent` 导入），`explorer.py:731`/`:817` 与 `verify_agent.py:451`/`:482` 四处统一调用；P4-10 增双轨断言 | 实施方案 §2.1/§4.2；验收 P4-10 |
| R-9 | 中 | **采纳**：enum 挂点改为 `schemas/ai_explorer_observation.schema.json`；"protocol_version"表述改为 registry 版本 `explorer@1.1.0`；P4-1 增加"执行 `sync-ai-protocol.py` 后 hash 更新且 pytest 通过"；明确 1.1.0 须同时提供 `user.md` | 实施方案 §2.1/§5；验收 P4-1 |
| R-10 | 中 | **采纳（方案①接受共享）**：明示 observation schema 被 1.0.0/1.1.0 共用，enum 扩展会放大 1.0.0 操作集，**确认可接受**（服务端实现已存在）；P4-12 断言改为"1.0.0 prompt 文本与行为零变化；操作集放大已确认" | 实施方案 §5；验收 P4-12 |
| R-11 | 中 | **采纳**：P4-12 明确 `test_explorer_protocol.py:142` 改名 `test_read_request_operations` + docstring 明示"P-4 解除 R-2 四操作决断" + 补 `get_call_chain` 正向用例；P4-13 升级为**约束继承断言**（1.1.0 ⊇ 1.0.0 全部既有 token，防止复制时丢失 F2/M4/P-3 累计约束） | 验收 P4-12/P4-13 |
| R-12 | 中 | **采纳**：BFS 每层改用 `reader.get_call_sites_for_methods(frontier_ids)`（批量 + 已解压 + 分片 10000）；删除"沿用 `get_seed_hops` 同模式"表述；与 R-6 合并（逐边容错落在该调用点） | 实施方案 §2.1/§3.4/§5 |
| R-13 | 中 | **采纳**：预算以渲染字符计，per-operation 上限设 **18_900**（14_000 × 1.35 转义余量）；P4-10 增最坏夹具断言；说明单次占跨轮 40K 的 35% 与典型响应仅 1–3K，维持现状不加轮内配额 | 实施方案 §4.2/§6；验收 P4-10 |
| R-14 | 中 | **采纳**：改为**两级回退**表述——开关级（降级为纯 resolved 宽度优先输出形态，非 v1 全貌）+ 代码级（删除方法 + dispatch 分支）；删除"零成本回退 v1"绝对表述 | 实施方案 §6；验收 §5 |
| R-15 | 低 | **采纳**：新增 §2.4 体积/价值预估（p50=4 行/p90=22、典型响应 1–3K、压力源于多轮往返而非单体量）；§1 调整价值主张（主要=省轮次/上下文，sink 发现为附带且受 taxonomy 覆盖面制约）；P4-14 增体积与"另有 N 条未展开"观测指标；prompt 增加高扇出引导 | 实施方案 §1/§2.4/§5；验收 P4-14 |
| R-16 | 低 | **采纳**：prompt 改动点精确到 `system.md:39`（operation 约束行）与 `:61-63`（读码操作段），并新增三类边语义/★SINK 读法/高扇出引导；P4-13 按位置点名 token | 实施方案 §5；验收 P4-13 |
| R-17 | 低 | **采纳**：`schemas/config.schema.json` 同步列入变更清单；P4-1 增加"schema `call_tree` 段含 7 个新字段且默认值与 `config.py` 一致"断言 | 实施方案 §5；验收 P4-1 |

**闭合结论**：R-1~R-17 **全部处置**（16 条采纳 + 1 条部分采纳），**无反驳项**。
唯一拒绝的部分是 R-2 建议②（在 P-4 内增补 taxonomy 条目），理由：属 T2.9 升级闭环的独立任务，
且改动会影响既有 custom_sink 判定与 M2 指标，超出 P-4 范围——已记入方案 §1 非范围。
两份方案已修订为 **v3**（实施方案新增 §0 版本演进、§2.3 命中基线、§2.4 体积预估、§3.4 接口契约；
验收方案 P4-5 改固定样本断言、新增 P4-4 宽松命中两态、P4-10 四处同源、P4-13 约束继承）。
修订后进入实施（阶段 6）。
