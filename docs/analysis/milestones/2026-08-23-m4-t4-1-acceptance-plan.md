# 任务验收方案：M4-T4.1

> **任务编号**：M4-T4.1
> **依据实施方案**：`2026-08-23-m4-t4-1-implementation-plan.md`

## 1. 验收点清单

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| A-1 | ExplorerExpectation schema | 单测 model_validate | 字段/约束符合方案；expectation 枚举三值 |
| A-2 | GoldenCase 向后兼容 | 既有 golden 加载测试 | v2 无标注 case 零改动可加载（explorer_expected=None） |
| A-3 | 命中判定：hit | 单测 | source+sink 各含任一键 → True；大小写不敏感 |
| A-4 | 命中判定：miss/无标注 | 单测 | expectation!="hit" 或无标注 → False；单边命中 → False |
| A-5 | explorer_hit 入口 | 单测（fake candidate） | chain_proposal 的 source/sink 文本正确提取 |
| A-6 | 8 个正样本标注 | 数据校验脚本/测试 | 每个 case 有 explorer_expected（hit + 非空键 + notes）；manifest v3 |
| A-7 | 既有评估零回归 | 全量 pytest | 1206+ 全过（golden 加载/规则轨指标不变） |

## 2. 边界与负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | match_keys 为空列表 | schema min_length=1 拒绝 |
| N-2 | 标注键含大小写变体 | 子串匹配命中（不敏感） |
| N-3 | conditional case | 不进二元命中（False） |

## 3. 回退

标注为数据层新增（旧 case 零改动）；代码层字段可选——revert golden.py 即回退。
