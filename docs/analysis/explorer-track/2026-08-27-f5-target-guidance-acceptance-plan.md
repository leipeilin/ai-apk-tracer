# 任务验收方案：F5 目标组件引导

> **验收执行记录（2026-08-27 实施后）**：A5-1~A5-7 全过（1270 全量 + 探针 PASS）；
> A5-8 golden 未命中（模型 4 轮内仍偏 login 方向——加分项不计门槛）；A5-5 探针级复核零复读
> （5 候选 sink 均为 finding 相邻新 sink——SplashPresenter/SubProcessLoginManager/LoginManager 链）。
> 环境注记：主 venv OpenSSL 3.6.3 与 siliconflow ALB TLS 不兼容（PQ groups 疑似），探针经
> Python 3.14 副本 venv（OpenSSL 3.5.5）执行（`backend/.venv-tls`，已 gitignore）。

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| A5-1 | 入口优先级排序 + 覆盖口径不变 | 单测（orchestrator） | 有 finding 组件的入口排前（稳定排序——同级保原序）；**排序不改覆盖口径**——无 finding 组件入口仍在探索序列内（非跳过，仅靠后），`entries_explored` 计数不受排序影响 |
| A5-2 | known_findings 注入 + 撞名归属 | 单测（explorer 轮次 payload） | 该组件有 finding → 注入摘要列表；无 → null；组件无匹配 → 空不注入；**同名组件不互相污染**（`com.a.X` 与 `com.b.X` 各自只匹配本组件 finding——精确字符串相等） |
| A5-3 | prompt 约束 14 + 约束 5 改写 | 协议断言 | 约束 14："目标组件引导"/"相邻攻击面"/"探索独立性红线"/"不得复读" token；**约束 5 为改写**（非并列新增）：新表述"done=true 须伴随 proposal **或** reasoning 含无敏感结论"，旧硬性"必须伴随 chain_proposal"语义解除——断言新表述存在且旧表述不再单独成立 |
| A5-4 | 重复请求检测（完全 + 部分重叠） | 单测（FakeAnalyzer） | 完全重叠 → 终止（no_new_requests）；**部分重叠 → 去重执行**（只执行增量请求，重复请求跳过不消耗执行额度；仅零增量才终止） |
| A5-5 | **反复读守卫（机器兜底）** | 单测（explorer_normalization，增参 known_findings_index） | **三键全同才判复读**：组件 + rule_id + sink 键（`_sink_keys` 口径）全同 → `replayed_finding=true` + 降档 unverified + gap `EXPLORER_FINDING_REPLAY`；**相邻新 sink 不误杀**（同组件同 rule 但 sink 不同 → 不标记——复发检测的合法产出）；**语义级复读探针复核**（机器不拦的同问题换链——探针产物人工抽查非 finding 复述） |
| A5-6 | **探针行为验收** | probe_explorer_entry（`--entries` 显式指定） | **先 dry-run 确认入口集**：从 run 产物提取含 finding 组件的入口 ID（探针默认异构取样不保证覆盖）→ 指定探索。要求：D-3/seed_hit_rate 不回退；B 类 sink 保持归零；**引导有效性**：含 finding 组件入口产出候选率不低于其基线；**对照保护**：无 finding 组件入口（同探针批）产出不回退（确认性偏差未压制常规探索） |
| A5-7 | 零回归 | 全量 pytest + sync --check | 1260+ 全过 |
| A5-8 | **golden 命中（可选加分）** | shop 全量 run（~1h） | extra-close（MainActivity 的 extra_close_url 分支）被探索覆盖（命中或至少候选涉及 extra_close_url/go2CloseSet）——F5 的最终效果验证 |

## 验收原则

- A5-5 是本任务**核心风险验收**（注入的副作用控制）——机器兜底拦"三键全同"确定复读，语义级复读（同问题换链）靠探针级复核，双层口径对齐实施方案 3.3 红线 + 3.5 守卫；
- A5-6 中"引导有效性"须先 dry-run 确认含 finding 组件的入口 ID（探针默认异构取样不保证其在 6 入口内——评审补充项），用 `--entries` 显式指定；"对照保护"（无 finding 入口不回退）防排序注入的确认性偏差（评审 P2）；
- A5-8 全量 run 为加分项（成本 ~1h AI 调用）——用户批准后执行；探针级 A5-6 通过即满足合入门槛。

## 评审修订记录（2026-08-27）

依 `2026-08-27-f5-target-guidance-review.md`（有条件通过）修订：
- P1-1 数据源接线：实施方案 3.1 补参数注入路径（`_run_explorer_stage` 增参，主流程 227 行调用点传入）；
- P1-2 缺口 2 实现层次：复读守卫明确落 `explorer_normalization.py`（3.5 新增 + A5-5 更新）；
- P1-2 缺口 1 判定口径：**部分采纳**——"仅 sink 相同"确与 3.3 红线有缺口（补 rule_id 维度），但审查建议的"组件+问题类型即复读"过宽（误杀相邻新 sink 深挖——F5 复发检测核心价值）。定为三键全同（组件+rule_id+sink 键）；
- P2 确认性偏差：实施方案 3.1 补保护说明 + A5-1 覆盖口径断言 + A5-6 对照保护；
- 补充项：A5-2 撞名精确匹配、A5-3 约束 5 改写语义、A5-4 部分重叠去重执行、A5-6 基线可用性（dry-run 确认 + `--entries` 指定）。

## 回退

入口排序/注入/约束三层各自独立可回退（feature 无开关——回退即 revert 对应提交块）；附带项（重复检测/干净出口）独立成块。
