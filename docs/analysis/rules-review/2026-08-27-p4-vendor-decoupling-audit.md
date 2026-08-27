# 核验报告：P4 厂商特定条目迁出 shared 层（ruleset-quality-review）

> **任务编号**：P4（E7：厂商解耦——单一事实源架构 + manual 回退）
> **核验日期**：2026-08-27
> **核验对象**：`docs/analysis/rules-review/2026-08-27-p4-vendor-decoupling.md` 及其变更
> **核验模型**：deepseek-v4-pro（独立子 agent，只读评审，79 次工具调用）
> **状态**：第 1 轮（已闭合）

---

## 1. 核验结论摘要（子 agent 原文）

P4 的核心迁移在代码层面基本成立且质量较高：5 条 manual 条目的 method/taxonomy/leaves（11 个）/prefix/verified_arity 集合与报告声明逐一吻合，尾部回退的三态逻辑与 `_signature_checked_effect` 逐分支等价，same_package_leaf 的防 spoofing 边界逻辑正确且有索引侧 FQCN 解析事实支撑（indexer.py:1227），kind="custom_sink" 在 flow 链路与 backend 消费端均无枚举约束可撞，backend 对 verified_arity 的容错（落 meta）与 promote 重写的字段保留均已确认。但 E7 的"残留声明完整性"不成立：除报告声明的 3 处残留外，另发现 4 处未声明的厂商特定硬编码仍留在 shared 层（其中 detector.py 的 Sport|Workout|Account 词表是 E7 评审原文点名过、P4 既未迁移也未声明的项）；sensor 残留注释的保留理由与代码事实矛盾；报告声称的"9 用例"实为 8 个；same_package 宽匹配边界与前缀放宽边界均无直接测试锚定。

## 2. 问题清单（子 agent 提出）

**【R-1】【中】** E7 残留声明不完整：4 处未声明厂商硬编码——index_reader.py:76 的 sport 方法名（FLOW_INTRINSIC）、dataflow.py:35-38 的 VALIDATOR_METHODS shop wrapper 副本、detector.py:2536-2541 的 _SERVICE_SENSITIVE_BINDER_PATTERNS（sport 系 + getDid/issueStart/issueEnd/updateFindDeviceStatus）、detector.py:3464 的 _review_priority Sport|Workout|Account 词表（E7 评审点名项）。
**【R-2】【中】** sensor 残留注释保留理由失真："versions.yaml 条目结构无法表达"与 manual 回退已实现 same_package_leaf 的代码事实矛盾（真实理由：与平台 registerListener 共用 family 分支、拆分收益低）。
**【R-3】【低】** 报告事实性偏差：测试用例数 9 实为 8；§4.1 的 same_package 宽匹配边界未以新用例闭环。
**【R-4】【低】** manual 回退的防 spoofing 边界（跨包含名类）与前缀放宽边界（com.xiaomi.fitness. 域内非 sport 子包）均无直接测试；无缓存复用用例。
**【R-5】【低】** verified_arity 解析失败静默降级为提案形态（闭链能力无声丢失），全链路无结构校验（同步脚本跳过 manual、backend 落 meta 不校验）。
**【R-6】【低】** 前缀放宽动机描述不精确（"覆盖 same_package 形态"——同包实际由 same_package_leaf 覆盖；真实收益是 sport_xms 跨子包/entry 形态召回），放宽事实未落条目溯源。
**【R-7】【低】** taxonomy_version 未随 P4 数据变更递增（仍 1.0.5）。
**【R-8】【低】** 同步脚本对 manual 条目的跳过理由已过时（P4 后 dataflow 经 _manual_sink_table 消费 manual，探针可验证）；same_package_leaf 行号注释漂移。

## 3. 认可项（节选）

1. 迁移条目数据与报告声明逐字段吻合（write {1,3}/startSport {0,1,2,3}/pause·resume {0,1,2}/finishSport {0,1,2,3}、11 leaves、前缀）；全文件 82 条 = 73 base + 9 manual 自洽；
2. 三态回退与 `_signature_checked_effect` 逐分支等价（越界→not_sensitive；缺 descriptor→OPERATION_SIGNATURE_GAP 同构字段；命中→verified=True）；
3. same_package_leaf 防 spoofing 逻辑正确且动因属实（索引侧同包字段类型以 FQCN 落库，indexer.py:1202-1227）；
4. 缓存/降级/路径正确（进程级缓存、yaml 缺失降级空表不抛、规则子进程用 backend venv 的 pyyaml 可用）；
5. 回退位置安全（全部内置 family 之后、resolved_target 早退保证应用内 wrapper 不被闭链）；
6. kind="custom_sink" 下游透传安全（effect_chains 仅 verified 门控自由透传；detector 侧 kind 判断不误入 file_open 特判；backend 无枚举约束）；
7. backend 对 verified_arity 容错且 promote 生命周期不丢失（meta 回写）；
8. 测试锚定核心等价面（verified/gap/越界/不匹配/降级、S1 闭链测试锚定 finishSport arity 3）；
9. 三项已声明残留中两项注释准确。

## 4. 边界检查表（子 agent 原文）

| 检查项 | 结论 |
|---|---|
| 迁移等价性 | 有条件通过（前缀放宽一级为已披露语义放宽） |
| 回退逻辑 | 通过 |
| 残留声明 | 不通过（4 处未声明 + 1 处理由失真） |
| 下游透传 | 通过 |
| 测试覆盖 | 有条件通过（用例数不实、边界无专测） |
| 遗漏检查 | 不通过（R-1 四处） |

**总体判定**：机制实现核验通过可维持；"E7 迁出闭环"的声明 overstated——残留声明需按 R-1/R-2 补全后方可视为收口。

---

## 5. 处置记录（主 agent 回填，2026-08-27）

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 中 | **采纳（已实施）**：4 处补 E7 残留注释（各处标注"E7 残留（P4 核验 R-1）+ 保留理由 + 迁移落点待架构演进"）；报告 §1.3 由 3 项扩至 7 项 | index_reader.py、dataflow.py、detector.py ×2、P4 报告 |
| R-2 | 中 | **采纳（已实施）**：sensor 注释更正为真实理由（可表达、拆分收益低），与报告 §1.3 对齐 | dataflow.py sensor 分支注释 |
| R-3 | 低 | **采纳（已实施）**：报告勘误 9→8；核验后补 3 用例共 11 个 | P4 报告 §1.4 |
| R-4 | 低 | **采纳（已实施）**：3 个边界测试——`test_manual_fallback_rejects_cross_package_spoofing`（含 containing_class 断言跨包排除）、`test_manual_fallback_prefix_widening_within_vendor_domain`（sport_xms 前缀命中）、`test_manual_table_cache_reused_across_calls`（缓存复用） | test_dataflow_multichain.py |
| R-5 | 低 | **采纳（已实施）**：同步脚本对 manual 条目增加 verified_arity 结构校验（非空 int 列表，非法即 CONFLICT exit 1） | check_sink_taxonomy_sync.py |
| R-6 | 低 | **采纳（已实施）**：4 条 sport 条目 note 补记前缀放宽真实动机（sport_xms 跨子包/entry 形态召回） | versions.yaml |
| R-7 | 低 | **采纳（已实施）**：taxonomy_version 1.0.5 → 1.0.6 | versions.yaml |
| R-8 | 低 | **采纳（已实施）**：同步脚本增加 manual 条目回退探针（预期 kind=custom_sink 且 taxonomy 一致——9 条全部探针命中，PASS 计数 82）；docstring 行号注释改为符号引用防漂移 | check_sink_taxonomy_sync.py |

**闭合结论**：R-1~R-8 全部采纳并实施。核验后全量 **1309 passed / 0 failed**（38.77s）；同步校验 **PASS 82 / CONFLICT 0 / ORPHAN 0**（73 base + 9 manual 全探针，含 verified_arity 结构校验）。E7 闭环边界：7 项残留全部注释声明（迁移落点均为"待架构演进"），评审 E7 的数据面迁移（sport/伪平台类 → versions.yaml 单一事实源）完成，启发式面（Binder 敏感度/评分/回查豁免）保留并显式声明。
