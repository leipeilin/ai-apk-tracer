# 任务验收方案：T2.9 custom sink 升级闭环

> **任务编号**：T2.9
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-9-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测（加载/匹配/判定/闭环纯函数）+ CLI 冒烟 + 全量回归

---

## 1. 验收范围

- T2.9 全部交付物：versions.yaml 种子与结构、sink_taxonomy.py（加载/匹配/promote/revalidate/golden）、explorer_validation 判定接通与封顶、orchestrator 接线、CLI 工具。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 种子文件合法且可加载 | `load_sink_taxonomy(WORKSPACE_ROOT/rules/sink_taxonomy/versions.yaml)` | ≥30 条 base 条目；每条含 method/taxonomy/source=base；receiver 约束三态至少覆盖 leaves/prefixes/exact 各一例 |
| A-2 | 加载容错 | 缺失/损坏 YAML/结构异常（entries 非列表） | 返回 []（判定禁用——保守 False 兼容旧行为）；不抛 |
| A-3 | 匹配三态 | 构造 entries：leaf/prefix/exact 各一 → `sink_matches_taxonomy` | method+leaf 命中 / prefix 前缀命中 / exact 全名命中；method 不匹配 → None |
| A-4 | receiver 缺失宽松命中 | receiver_type=None → 带约束条目 | 命中（D2：缺失≠失配） |
| A-5 | 同名异义消歧 | query@ContentResolver 与 query@SQLiteDatabase 两条目（taxonomy 不同） | 分别命中对应条目（receiver 区分） |
| A-6 | 判定接通——命中不压档 | 真实索引候选（sink=startService@Context 类调用，hops 全通过）+ 含该条目的 taxonomy | custom_sink_proposal=False；status=validated |
| A-7 | 判定接通——未命中压档 | 同候选但 taxonomy 无该条目 | custom_sink_proposal=True；status=partially_validated（封顶）；notes 含"custom sink 待人工确认" |
| A-8 | 禁用兼容 | taxonomy_entries=None | custom_sink_proposal=False；不压档（T2.6 行为——既有测试全过） |
| A-9 | 畸形锚点不加重 | to_method_id 无 `#` / call_sites 无 receiver 行 | 判定跳过（custom=False）；不抛 |
| A-10 | promote 追加与版本递增 | promote_custom_sink（新方法） | 条目追加（source=manual + 确认元数据 + 溯源）；taxonomy_version 递增；返回 appended |
| A-11 | promote 幂等 | 同参数二次调用 | skipped（不重复追加）；文件条目数不变 |
| A-12 | promote base 升级 | 种子条目参数 promote | source 改 manual + 确认元数据（upgraded） |
| A-13 | 重校验升档对比 | run 预置 custom 压档候选（candidates.json）→ promote 后 revalidate | status_changes 含 {before: partially_validated, after: validated, custom_before: true, custom_after: false}；原文件不被改写 |
| A-14 | golden 用例生成 | generate_golden_case | 对齐 golden/v1/cases 字段（id/label=positive/rule=EXPLORER_AGENT/sources/sinks/provenance 含 explorer-promotion 溯源）；JSON 可序列化 |
| A-15 | orchestrator 接线 | `explorer.enabled` run（真实索引 + taxonomy 文件存在） | 校验路径收到 taxonomy_entries；stage 正常完成 |
| A-16 | CLI 冒烟 | `--method`/`--taxonomy` 用法 B 跑通（临时 taxonomy 文件） | 退出码 0；输出含 appended/版本号 |
| A-17 | 一致性 | 种子条目与 rules 提炼对照（抽样 5 条） | method/taxonomy/receiver 与提炼报告一致 |

## 3. 回归标准

- [ ] `cd backend && .venv/bin/python -m pytest` 全量通过（基线 1120 passed / 0 failed，只增不减）；
- [ ] `scripts/check-backend.sh` 通过；改动文件 ruff 零错误；
- [ ] 默认行为兼容：`rules/sink_taxonomy/versions.yaml` 存在后既有探索测试若因种子命中而行为变化——**预期不变**（测试用的 sink 多为自造方法名，不在种子内；若有命中需逐例核对语义）；测试通过为判据。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | promote 时 taxonomy 文件不存在 | 新路径 | 创建文件（schema 头 + 首条 manual）——闭环冷启动 |
| N-2 | revalidate 时 candidates.json 缺失 | 空 run_dir | 返回 {total: 0, status_changes: []}；不抛 |
| N-3 | 候选无 validation 字段（历史产物） | 旧格式 candidates | 重校验照常（校验器原地填 validation） |
| N-4 | YAML 条目缺 method/taxonomy | 畸形条目 | 该条目跳过 + warning；其余正常加载 |
| N-5 | sink 文本方法名提取失败 | to_method_id 尾段无方法名 | 判定跳过（A-9 同路径） |
| N-6 | promote 无 receiver 约束（裸方法名条目） | 用法 B 不带 --receiver | 条目允许（约束空集=任意 receiver 命中——manual 语义由人工负责） |

## 5. 回退方案

- 删除/清空 `rules/sink_taxonomy/versions.yaml` → 判定禁用（回到 T2.6 保守行为）；代码按文件粒度回退。

## 6. 验收记录（实施后填写）

> **验收日期**：2026-08-22。**结果：全部通过**。全量回归 **1138 passed / 0 failed**（基线 1120 + 新增 18）；`scripts/check-backend.sh` 通过；改动文件 + CLI ruff 零错误；CLI 冒烟（用法 B：appended + 1.0.1 + manual 条目 + 确认元数据落盘）通过。实施勘误：`load_sink_taxonomy` 返回 `list | None`（None=禁用/缺失；**空列表=启用且零已知 sink 全标 custom**——区分文件禁用与空启用两语义）；集成测试隔离按评审 R-8 落地（`custom_sink_taxonomy_path` 指向不存在路径）。

| 编号 | 结果 | 实测说明（测试函数） |
|---|---|---|
| A-1 | 通过 | `test_seed_file_loadable`：48 条 base + 三态约束覆盖 + 抽样对照（startService/execSQL 与 rules 提炼一致） |
| A-2 | 通过 | `test_load_tolerance`：缺失/损坏/结构异常 → None（禁用）；空条目 → []（启用）；畸形条目跳过 |
| A-3 | 通过 | `test_match_three_modes`：leaf（含 smali `Landroid/content/Context;` 剥离）/prefix/exact |
| A-4 | 通过 | `test_receiver_missing_lenient`：None/空宽松命中；有证据（com.example.C）则失配 |
| A-5 | 通过 | `test_same_name_receiver_disambiguation`：query 双 receiver 分别命中 |
| A-6 | 通过 | `test_hit_no_cap`：startService@C 命中 → validated 保持 |
| A-7 | 通过 | `test_miss_caps_to_partial`：空条目 → custom=True + 封顶 partial + notes + verified_hop_count=2 |
| A-8 | 通过 | `test_disabled_compatibility`：None → T2.6 行为 |
| A-9 | 通过 | `test_malformed_anchor_not_flagged`（无 # 锚点 + `test_method_id_parsing` 双形态） |
| A-10 | 通过 | `test_promote_append_and_idempotent`（含 N-1 冷启动：appended + 1.0.1 + manual 元数据） |
| A-11 | 通过 | 同上：二次 skipped + 条目数不变 |
| A-12 | 通过 | `test_promote_upgrade_base`：base → manual（source 改写 + 确认元数据） |
| A-13 | 通过 | `test_revalidate_promotion_lifecycle`：压档 → promote → 重校验 after=validated/custom=False；副本不落盘（原文 validation=None） |
| A-14 | 通过 | `test_golden_case_shape`：**GoldenCase.model_validate 通过**（评审 R-1 重设——label/rule/expected.taxonomy/provenance.reference="r1/expl_x@v1.0.1"） |
| A-15 | 通过 | orchestrator 接线（load → validate 传参）+ `test_disabled_compatibility`（既有集成测试隔离后全过） |
| A-16 | 通过 | CLI 冒烟（用法 B：appended + 版本递增 + 条目结构断言） |
| A-17 | 通过 | `test_seed_file_loadable` 内抽样对照 |
| A-18 | 通过 | `test_deep_dive_excludes_custom`（评审 R-3：custom 压档候选 partial_total=0 零深挖调用） |
| N-1~N-7 | 通过 | N-1 冷启动/N-2·N-7 `test_revalidate_tolerances`（缺失→空报告；索引缺失→degraded）/N-3（无 validation 历史产物）/N-4 `test_load_tolerance`/N-5 `test_malformed_anchor_not_flagged`/N-6 `test_bare_method_entry_matches_any_receiver` |
