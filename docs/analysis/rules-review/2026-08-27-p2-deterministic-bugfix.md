# P2 任务实施报告：E1/E2 确定性 bug 修复 + promote 锚点加固

> **任务来源**：`docs/analysis/rules-review/2026-08-27-ruleset-quality-review.md` 第五节优先级 #2；P1 核验（`2026-08-27-p1-sink-taxonomy-sync-audit.md`）闭合结论移交项 ①②
> **实施日期**：2026-08-27
> **实施者**：主 agent（GLM-5.3）

## 1. 变更清单

### 1.1 E1：`ORDERED_BROADCAST_UNRESTRICTED` 权限判定重写（rules/shared/detector.py）

- 新增 `_ordered_broadcast_has_permission(code)`：对每个 `sendOrderedBroadcast(` 调用用 `_matching_paren_end`（detector.py:146）定位配对括号、`_split_top_level_args`（detector.py:172）拆顶层参数，**第 2 参数非空且非 `null` 字面量**即视为已限制。
- 替换原逐字符否定正则 `sendOrderedBroadcast\s*\([^,]+,\s*[^n][^u][^l][^l]`——它无法处理第一参数内嵌逗号（`sendOrderedBroadcast(new Intent("a","b"), null)` 被误判为有权限 → 漏报）。
- 语义边界（函数 docstring 声明）：receiverPermission 为变量/常量引用时视为已限制（运行时值未知，保守取"有限制"方向；规则本身为 auxiliary 加权信号，不单独成 finding）。
- **行为变化**：嵌套逗号 + null 权限的调用从"静默不报"变为"报 unrestricted"；权限名为 `n?u?l?l` 形态的误判消除。

### 1.2 E2：`getLastKnownLocation` 补录（rules/shared/dataflow.py + versions.yaml）

- `dataflow.py` LocationManager family 的 checked 字典新增 `"getLastKnownLocation": frozenset({1})`（公开签名单参 `String provider`），带评审溯源注释。
- `versions.yaml` 新增 base 条目（leaves [LocationManager] → location_sensor_collection），双源同步。
- 至此纯 framework 位置读取链路（无 GMS 依赖）恢复检出。
- **核验 R-2 修订（既有死条目修正）**：versions.yaml 的 `getLastLocation` 条目原挂 `receiver_leaves: [LocationManager]`——该组合在 framework 中不存在（死组合），而真实组合 `FusedLocationProviderClient.getLastLocation()` 在消费端零命中（消费端 leaf 匹配取 FQCN 尾段，sink_taxonomy.py:140/152-153）。本任务将该条目 receiver 修正为 `leaves [FusedLocationProviderClient] + prefixes [com.google.android.gms.location.]`（参照 getCurrentLocation 条目写法），消除两轨反向缺口的一个实例（check_sink_taxonomy_sync.py:31-33 记录的盲区类型）。dataflow 侧的 getLastLocation 保留在同一 family（exact/prefixes 覆盖 FusedLocation receiver，dataflow 视角无错误）。

### 1.3 promote 锚点加固（scripts/promote_custom_sink.py，P1 核验移交项）

- `_sink_anchor_from_run` 新增**自环拒绝**：链尾 `to_method_id == from_method_id` 时报错退出（旧行为提取出所在方法名而非 sink 方法——saveCallback 候选曾提取出 `loading`）。
- CLI 层新增**无约束条目拒绝**：run 索引反查 receiver 失败且用户未显式提供 `--receiver-exact/leaf/prefix` 时报错退出（旧行为静默生成任意 receiver 命中的无约束条目，消费端 N-6 语义下过宽匹配）。

### 1.4 测试（backend/tests/test_dataflow_multichain.py）

- E2：`test_taxonomy_verifies_known_receiver_families_and_arities` 参数化表加 `("android.location.LocationManager", "getLastKnownLocation", 1, ...)` 行。
- E1：新增 `test_ordered_broadcast_nested_intent_null_permission_reports_unrestricted`（回归用例：嵌套逗号 + null 报 unrestricted）与 `test_ordered_broadcast_permission_detection_by_second_arg`（参数化：权限字符串/常量引用不报、显式 null 报）。

## 2. 验证结果

- promote 自环拒绝实测：对 saveCallback 候选（expl_a369374f4e49477e93f6）运行报错 `链尾跳为自环（from==to）——sink 锚点不可靠`，exit 1 ✓；
- 同步校验：`base 74 条：PASS 74，CONFLICT 0，ORPHAN 0`（E2 新条目与 dataflow 一致）✓；
- 全量测试：**1277 passed / 0 failed**（`backend/.venv/bin/python -m pytest backend/tests/ --tb=no`，39.32s，2026-08-27）。

## 3. 行为影响评估

- E1 是 auxiliary 信号修复：影响 rule-results 候选产物集合（旧漏报形态从无到有），不进 funnel/finding/确定性闭链（核验 R-6 修订措辞：候选 component_name 为 `dynamic:{path}` 前缀，attack_surface 聚合排除、funnel 路由 NONE、finding 聚合跳过）；
- E2 属 sink 分类扩展：纯 framework 位置读取链路从"不成 sink"变为 location_sensor_collection sink——是评审确认的漏检修复方向；
- promote 加固是 CLI 工具行为收紧：既有 4 条 manual 条目不受影响（当时锚点提取正常）；后续 promote 需自环候选改用法 B 显式指定。

## 4. 待核验点

1. `_ordered_broadcast_has_permission` 的边界（变量权限保守视为有限制、1 参调用视为 unrestricted）是否与 auxiliary 信号语义自洽；
2. E2 的 arity {1} 是否覆盖 JADX 反编译形态（有无 0 参/2 参变体）；
3. promote 无约束拒绝是否误伤合法的"方法名本身唯一、receiver 无关紧要"场景（如静态方法 sink）；
4. E1 测试对 FTS 初筛（GLOBAL_RULE_TERMS 的 sendOrderedBroadcast 词项）的依赖是否稳定。
