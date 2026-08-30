# 任务验收方案：P-4 复合读码工具 get_call_chain（v3）

> **任务编号**：P-4
> **日期**：2026-08-30
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-30-p4-call-chain-tool-implementation-plan.md`（v3）
> **状态**：v3（随方案重写——P4-5 改固定样本断言、新增宽松命中/预算同源/接口契约/体积指标验收）
> **验收方式**：pytest 单测（mock 索引）+ **固定样本真实索引验收** + schema/registry 断言 + 全量回归 + 探针 A/B

---

## 1. 验收范围

P-4 全部交付物：协议扩展（enum + schema + registry 同步）、prompt 1.1.0、`call_tree.get_call_chain`
（含接口契约、三类边处理、sink 判定、宽松命中区分、逐边容错）、taxonomy 数据源接线、
四处同源的 per-operation 预算上限、配置项与 config.schema.json。验收通过即视为可进入提交。

**验收数据源**：

- 真实索引：`.ai-apk-tracer/runs/20260829T145430Z_fc0d0e01d0e0_868521fd/index/analysis.sqlite3`
- 真实 taxonomy：`rules/sink_taxonomy/versions.yaml`（82 条目 / 75 方法名）

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与步骤 | 预期结果 |
|---|---|---|---|
| P4-1 | 协议扩展与 schema/registry 同步 | 单测 + 执行 `scripts/sync-ai-protocol.py` 后全量 pytest | `ai_models.py:323` Literal 含 5 操作；**`schemas/ai_explorer_observation.schema.json`** 的 `$defs.ReadRequest.properties.operation.enum` 含 5 项（R-9：非 input schema）；`registry.yaml` 注册 `explorer@1.1.0` 且 `template_sha256`/`schema_sha256` 已更新；1.1.0 目录含 `system.md` **与** `user.md`；`schemas/config.schema.json` 的 `call_tree` 段含 7 个新字段且默认值与 `config.py` 一致（R-17） |
| P4-2 | 三类边分类处理 + 接口契约 | 单测（mock 索引：resolved/pending/ambiguous 各一条） | resolved 节点继续展开；pending 边输出摘要行（`method_name(receiver_type)`+参数+行号）不展开；ambiguous 标注 gaps 与候选数；**返回体字段符合实施方案 §3.4 契约**（`root/nodes/edges/sink_leaves/gaps/truncated/rendered`），且 `rendered` 与结构化字段同源 |
| P4-3 | pending 边 sink 命中标记 | 单测，fixture **用真实 taxonomy 条目** | `startService` + `receiver_type=android.content.Context` → `★SINK connection_session_control`（fixture 对齐 `versions.yaml:53-56` 的 `receiver_leaves: [Activity, Context]`，避免单测过而真实索引不过，R-2） |
| P4-4 | **宽松命中两态**（receiver 缺失） | 单测：`query` + `receiver_type=None`，分别以 `loose_receiver=True/False` 调用 | True → 标 `★SINK? data_disclosure（receiver 缺失·宽松命中）`；False → 通用名（`query/delete/execute/remove/...`）**不标** ★SINK，仅在 gaps 统计。两态均不抛 |
| P4-5 | **固定样本的真实索引 sink 可达性**（价值验收） | 对下表 6 个**实测枚举**的 method_id 调 `get_call_chain` | 每个样本响应含对应的 ★SINK 行（method_name + taxonomy 逐条匹配）。**不使用随机入口**——实测随机入口可达率仅 8.14%（R-1） |
| P4-6 | sink 导向剪枝 | 单测（A 有 5 条出边：2 条 pending 命中 sink + 3 条 resolved 普通）；`sink_first=True/False` | True：sink 边全部输出、resolved 按一跳前瞻排序；False：宽度优先（对照） |
| P4-7 | 链展开与分层粒度 | 单测（mock A→B→C→D） | L0 完整（≤240 行）/ L1 节选（40 行）/ L2+ 摘要行；每跳含进入点标注（`file:line`） |
| P4-8 | 深度/跳数上限 | 单测（4 层链 + `depth=2`；超 `max_hops`） | 超深不展开并标注"深度上限"；超跳数截断标注 |
| P4-9 | 环防护 | 单测（A→B→A） | 环边标注"环——已展开"，不重复展开，无死循环 |
| P4-10 | **预算上限四处同源 + 最坏夹具** | 单测（超长链）+ 断言 `explorer.py:731/817`、`verify_agent.py:451/482` 均调 `read_result_char_limit` | `get_call_chain` 两轨均 ≤ 18_900 且不被二次截断；旧 4 操作两轨均仍 8192（**零回归**）；最坏夹具断言 `len(json.dumps(payload, ensure_ascii=False)) <= 18_900`（R-8/R-13） |
| P4-11 | 计费口径 | 单测 | 1 次 `get_call_chain` = `read_requests` 计 1（两轨一致） |
| P4-12 | 旧协议兼容 + 测试锚点解除 | 全量回归 | 4 旧操作行为零变化；`test_explorer_protocol.py:142` 已改名 `test_read_request_operations` 且 docstring 明示"P-4 解除 R-2 四操作决断"并补 `get_call_chain` 正向用例（R-11）；**1.0.0 prompt 文本与行为零变化；observation schema 共用导致的操作集放大已确认可接受**（R-10） |
| P4-13 | prompt 引导与约束继承 | 协议断言，对 1.0.0/1.1.0 各跑一遍既有 token 断言循环 | 1.1.0 `system.md:39`（operation 约束行）+ `:61-63`（读码操作段）含 `get_call_chain`/`★SINK`/`pending`/`ambiguous` 与"≥2 层深链优先"引导；**1.1.0 ⊇ 1.0.0 全部既有约束 token**（F2 九类语义/M4 骨架链/P-3 空转轮等不丢失，R-11） |
| P4-14 | **探针 A/B 行为验收**（价值验收） | probe（1.1.0 跑 6-10 入口 vs 1.0.0 基线） | `get_call_chain` 使用率 >0；同入口轮次下降（深链入口均轮 3.5 → ≤2.5 目标）；**sink 到达率对比基线**（直接命中 1.57%、depth2/width3 可达 8.14%）；响应字符数 p50/p90、"另有 N 条未展开"出现率作为观测指标（R-15）；D-3/seed_hit 不回退 |

### P4-5 固定样本表（实测枚举，run `20260829T145430Z_..._868521fd`）

| # | method_id（target） | 位置 | 期望 ★SINK 行 |
|---|---|---|---|
| 1 | `androidx/sqlite/p015db/framework/C0912c.java#C0912c.mo3672q:173` | `:175` | `execSQL`(android.database.sqlite.SQLiteDatabase) → `database_mutation` |
| 2 | `androidx/sqlite/p015db/framework/C0912c.java#C0912c.mo3675v:195` | `:198` | `execSQL`(SQLiteDatabase) → `database_mutation` |
| 3 | `androidx/room/MultiInstanceInvalidationClient.java#MultiInstanceInvalidationClient.MultiInstanceInvalidationClient:106` | `:128` | `bindService`(android.content.Context) → `connection_session_control` |
| 4 | `ad/C0072a.java#C0072a.m154e:93` | `:105` | `delete`(java.io.File) → `file_mutation` |
| 5 | `ad/C0073b.java#C0073b.m159a:132` | `:193` | `delete`(java.io.File) → `file_mutation` |
| 6 | `cn/org/bjca/signet/bankcoss/p042b/p043a/C1406a.java#C1406a.m4918a:74` | `:76` | `query`(receiver 空) → `★SINK? data_disclosure`（宽松命中，`loose_receiver=True` 时） |

> 样本 1–5 为 receiver 明确的确定性命中；样本 6 为宽松命中（与 P4-4 联动）。
> 若索引产物不可用于测试（体积/路径），改以 mock 索引构造**等价 fixture**（同 method_name/receiver_type/taxonomy 三元组）。

## 3. 回归标准

- [ ] `cd backend && .venv/bin/python -m pytest` 全量通过（基线 **1370 passed / 0 failed**，只增不减）；
- [ ] `scripts/check-backend.sh` 通过；改动文件 ruff 零错误；
- [ ] `scripts/sync-ai-protocol.py` 执行后 registry hash 校验通过（否则 `prompt_registry` 加载抛错）；
- [ ] 旧 4 操作两轨上下文上限均仍 8192（P4-10 断言）；`explorer.enabled=false` 时无影响；
- [ ] 1.0.0 prompt 文本与既有行为零变化（P4-12）。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 根方法不存在 | 非法 method_id | `not_found` 结构；不抛 |
| N-2 | 无调用边的叶子方法 | 纯计算方法 | 仅 L0 + 空 edges；不抛 |
| N-3 | 全部 pending 边 | 只调框架 API | 只有 sink 摘要、无展开节点（正常形态） |
| N-4 | 全部 ambiguous | 接口调用为主 | 边标注 gaps 与候选数；不展开 |
| N-5 | 预算耗尽 | 超长链 | `truncated=True` + gaps 标注"预算截断"（不静默） |
| N-6 | taxonomy 禁用 | 传 `sink_entries=None` | 无 ★SINK 标记、pending 摘要照常输出、不抛（对齐 `sink_taxonomy.py:61` 禁用语义） |
| N-7 | **arguments 解压失败 / 旧版明文** | 损坏或明文 `arguments_json` | **该边仍出现在响应中**且带 `arguments_unavailable=True`，其余边与节点照常；**整个响应不是 `not_found`**（R-6：修正 v2 的错误预期——`JSONDecodeError` 是 `ValueError` 子类，会被 `explorer.py:989` 吞成 not_found 致整链丢失） |
| N-8 | 环 + 深度上限同时触发 | 环形深链 | 环标注优先，不重复展开 |
| N-9 | 索引缺失/不可读 | 无 analysis.sqlite3 | 空结果降级（对齐 `get_seed_hops` `call_tree.py:186-188`），不抛 |
| N-10 | 高扇出方法 | 15+ 条边 | 仅展开 `branch_width` 条，其余计为"另有 N 条未展开"（常态，非异常） |

## 5. 回退方案（两级，v3 修正 R-14）

1. **开关级**：`call_chain_include_pending=False` + `call_chain_sink_first=False` → 降级为
   "纯 resolved 宽度优先"输出形态（A/B 对照与应急；**注意：非 v1 全貌——v1 从未落地**）；
2. **代码级**：删除 `call_tree.get_call_chain` 与 `dispatch_read` 分支 → 真正回到 P-4 之前；
3. 协议层 enum 扩展向后兼容；prompt 层 registry 停用 1.1.0（**须先跑 `sync-ai-protocol.py`**，否则 hash 校验抛错）。

## 6. 验收记录（实施后填写）

> **验收日期**：<YYYY-MM-DD>。**结果**：。全量回归 ** passed / 0 failed**（基线 1370 + 新增 ）；
> `scripts/check-backend.sh` 通过；改动文件 ruff 零错误；探针 A/B：。

| 编号 | 结果 | 实测说明（测试函数/实测命令） |
|---|---|---|
| P4-1 | | |
| P4-2 | | |
| P4-3 | | |
| P4-4 | | |
| P4-5 | | |
| P4-6 | | |
| P4-7 | | |
| P4-8 | | |
| P4-9 | | |
| P4-10 | | |
| P4-11 | | |
| P4-12 | | |
| P4-13 | | |
| P4-14 | | |
