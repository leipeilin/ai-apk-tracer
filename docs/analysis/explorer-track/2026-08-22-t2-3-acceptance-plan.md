# 任务验收方案：T2.3（attack_surface：四组件攻击面导出）

> **任务编号**：T2.3
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t2-3-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + 集成（TestClient）+ 全量回归

---

## 1. 验收范围

- `build_attack_surfaces`（四文件组装/合并/聚合/映射）+ orchestrator 生成落盘注册 + 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式 | 预期结果 |
|---|---|---|---|
| A-1 | activity 字段与保守导出 | `test_activity_surface_fields` | conditional→True + reason 透传 + entry_methods/refs + schema 通过 |
| A-2 | 敏感能力聚合 | `test_sensitive_capabilities_aggregation` | 组件级 rule_id 聚合；全局规则不入（D3） |
| A-3 | receiver 静态+动态合并 | `test_receiver_merge_manifest_and_dynamic` | source=manifest+dynamic + actions/refs 并集 + exported OR 合并 |
| A-4 | 纯动态 receiver | `test_dynamic_only_receiver` | source=dynamic + exported=externally_reachable 判定 |
| A-5 | provider/service 字段 | `test_provider_and_service_surfaces` | authorities/permission 透传 |
| A-6 | 空类型文件 | `test_empty_kind_file` | components=[] 合法（四文件恒存在 D5） |
| A-7 | entry_table 缺失容错 | `test_missing_entry_table_tolerated` | refs/entry_methods 空 + 阶段不挂 |
| A-8 | 集成 | `test_orchestrator_attack_surface_stage` | 四文件 + artifacts 4 条 + stage completed |
| A-9 | 默认关闭零行为 | 全量回归 | 无 attack_surface 产物/阶段 |
| A-10 | 单测通过 | `pytest tests/test_attack_surface.py -q` | 全部通过 |
| A-11 | 全量回归 | `pytest -q` | 979+ 全部通过（T2.2 基线 979） |
| A-12 | 统一校验 | `scripts/check-all.sh` + `ruff check`（新文件） | 通过 |

## 3. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 空 manifest | components=[] | 四文件全空 components |
| N-2 | 动态 receiver_class=None | opaque 注册 | name=path 推导类名兜底 + exported=False |
| N-3 | entry_table 损坏 JSON | 非法内容 | 容错空 refs（同 A-7 路径） |
| N-4 | 静态 false + 动态 unreachable 合并 | exported OR 语义 | False（均不可达才 False） |

## 4. 回归标准

- [ ] 既有 run 行为零变化（默认关闭）
- [ ] 全量 979 passed / 0 failed
- [ ] api_entry_table 产物不受影响（只读）

## 5. 回退方案

- 任一验收点失败：修复后复验；设计缺陷上升评审第 2 轮。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 7 项意见第 1 轮全部采纳（含 R-1 动态 exported None→True 保守统一、R-2 T0.5 样例勘误声明 + 全局能力可见性移交 T2.5、R-3 entry_methods 含 dynrcv、R-4 真实生成器夹具消漂移、R-5 reason 组合标注、R-6 provider 读写权限透传）。实施中修正：集成测试推导式变量遮蔽笔误；`_dynamic_exported` 简化为 `is not False`（SIM103）。全量 988 passed / 0 failed（+9）。

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | conditional→True + reason 透传 + refs/entry_methods 真实生成器同源 + intent_filters | - |
| A-2 | 通过 | 组件级 3 rule_id 聚合（含 auxiliary）；全局 dynamic:path 不入 | - |
| A-3 | 通过 | source=manifest+dynamic + actions/refs 并集 + exported OR（false+True→True）+ reason 组合标注 + dynamic_registrations 透传 | - |
| A-4 | 通过 | 纯动态三分支：True/False/**None→True**（R-1 统一保守）+ refs/能力 | - |
| A-5 | 通过 | service 权限/protection + provider authorities + read_permission 透传（write None） | - |
| A-6 | 通过 | provider.json components=[] 恒生成（四文件齐全） | - |
| A-7 | 通过 | entry_table 缺失 → refs/entry_methods 空 + 能力聚合不依赖 entry_table | - |
| A-8 | 通过 | 四文件 + artifacts 4 条（component_kind 齐）+ stage completed | - |
| A-9 | 通过 | 默认关闭零行为（全量回归） | - |
| A-10 | 通过 | 9 项全过 | - |
| A-11 | 通过 | 全量 **988 passed / 0 failed**（979+9） | - |
| A-12 | 通过 | check-all + ruff（attack_surface.py/test 零违规） | - |
| N-1 | 通过 | 空 manifest → 四文件空 components（A-6 同构） | - |
| N-2 | 通过 | 纯动态 OpaqueReceiver（reachable=None）→ exported=True（A-4 断言） | - |
| N-3 | 通过 | 损坏 JSON 容错空 refs | - |
| N-4 | 通过 | 静态 false + 动态 None → True（test_receiver_merge_with_dynamic_unknown） | - |
