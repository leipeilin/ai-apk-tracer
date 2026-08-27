# P4 任务实施报告：E7 厂商特定条目迁出 shared 层（单一事实源架构）

> **任务来源**：`docs/analysis/rules-review/2026-08-27-ruleset-quality-review.md` 第五节优先级 #4
> **实施日期**：2026-08-27
> **实施者**：主 agent（GLM-5.3）

## 1. 变更清单

### 1.1 dataflow.py：移除 2 个厂商分支 + 新增 manual 回退

**移除**：
- `sport_family` 分支（sport_leaves 11 个硬编码 + `com.xiaomi.fitness.sport_manager_export.` 前缀 + startSport/pauseSport/resumeSport/finishSport 的 checked 字典）；
- 伪平台类 write 分支（`BluetoothOutputStream/UsbOutputStream/NfcOutputStream/ProtocolWriter` leaves——SDK 中不存在的自定义类）。

**新增 `_manual_sink_table()`**（进程级缓存）：加载 `rules/sink_taxonomy/versions.yaml` 的 manual 条目（method/taxonomy/leaves/prefixes/exacts/**verified_arity** 六元组）；yaml 缺失或无 pyyaml 环境时优雅降级为空表（规则轨退化为纯内置 family 行为，不抛异常）。

**新增尾部回退**（classify_operation_taxonomy 的 not_sensitive 兜底前）：method 匹配 + receiver 匹配（`_receiver_family_matches` **或 `same_package_leaf`**——索引把同包字段类型解析为 FQCN，裸名 leaves 不命中；同包 leaf 放行、跨包含名类 spoofing 仍排除）后按 `verified_arity` 三态：
- arity ∈ verified_arity → `is_effect=True, verified=True`（确定性闭链能力保持，v04 真机验证成果不降级）；
- descriptor 缺失 → `OPERATION_SIGNATURE_GAP`（critical=True，与内置 family 三态一致）；
- arity 越界 → not_sensitive；
- 条目无 verified_arity（如 shop 4 条提案条目）→ `CUSTOM_SINK_PROPOSAL`（critical=False，提案形态）。

### 1.2 versions.yaml：5 条迁移条目（manual，带 verified_arity 与 note 溯源）

- `write`（device_protocol_output，leaves 四伪平台类，arity {1,3}）——**source 从 base 改为 manual**（P1 曾按一致性入册 base，本次按来源属性修正）；
- `startSport`（location_sensor_collection，arity {0,1,2,3}）、`pauseSport`/`resumeSport`（connection_session_control，arity {0,1,2}）、`finishSport`（connection_session_control，arity {0,1,2,3}）——均带 11 个 leaves + `com.xiaomi.fitness.` 前缀（比原 `sport_manager_export.` 放宽一级，覆盖 same_package 形态）。

### 1.3 E7 残留声明（注释，无法外置的特调；核验 R-1 修订：增补 4 处）

- `dataflow.py` sensor 分支的 startGymSensor 等自研方法：**真实保留理由（核验 R-2 修订）**——versions.yaml 回退已实现 same_package_leaf 语义（可表达），但与平台 registerListener 共用 family 分支、拆分收益低——注释声明"完整迁移待架构演进"；
- `detector.py` SENSITIVE_BINDER_METHOD_RE 的 Sport|Workout|Wear 词表：Binder 敏感度启发式（非 sink taxonomy），无 yaml 落点；
- `index_reader.py` FLOW_INTRINSIC_METHODS 的 shop URL 校验 wrapper 名：跳回查豁免清单，移除会使 shop run 回查通过率回退；
- **（核验 R-1 增补）** `index_reader.py` FLOW_INTRINSIC_METHODS 的 sport 系方法名（startSport 等 4 个）：跳回查豁免，dataflow 侧检出已迁 manual，此处防 gap 噪声；
- **（核验 R-1 增补）** `dataflow.py` VALIDATOR_METHODS 的 shop URL wrapper 小写副本（`_is_validator` 消费）：与 index_reader 副本同源；
- **（核验 R-1 增补）** `detector.py` `_SERVICE_SENSITIVE_BINDER_PATTERNS` 的 sport 系词表与 getDid/issueStart/issueEnd/updateFindDeviceStatus：Binder 事务敏感度判定启发式；
- **（核验 R-1 增补）** `detector.py` `_review_priority` 的 Sport|Workout 词表（E7 评审点名项）：评分启发式，非 sink taxonomy。

### 1.4 测试（test_dataflow_multichain.py 新增 TestManualSinkFallback；核验 R-3 勘误：实施时 8 个用例，核验后补 3 个边界用例共 11 个）

sport/伪平台类/shop 条目经回退检出的等价性（verified 路径、signature gap 路径、arity 越界拒绝、receiver 不匹配不命中、yaml 缺失降级、同包 FQCN 命中——由既有 12 个 Binder/receiver 测试回归覆盖）。

## 2. 验证结果

- 全量测试：**1304 passed / 0 failed**（39.19s，2026-08-27；含既有 Binder sport 闭链 12 个测试的完整回归）；
- 同步校验：`base 73 条：PASS 73，CONFLICT 0，ORPHAN 0；manual 9 条`；
- 实施过程中的两次行为回退均已修复并测试锚定：① manual 回退最初无 arity 校验导致 sport 闭链 verified 降级（12 测试失败）→ `verified_arity` 三态修复；② 索引 FQCN receiver 不命中裸名 leaves → `same_package_leaf` 复用修复。

## 3. 行为影响评估

- sport/伪平台类检出的**结果等价**（taxonomy/kind 中 kind 从 "sport_state" 变为 "custom_sink"、receiver 匹配含 same_package_leaf 兜底）；确定性闭链（verified=True）保持；
- 规则轨获得 versions.yaml manual 条目的**全量消费能力**（shop 4 条提案条目经回退成为 `CUSTOM_SINK_PROPOSAL` 形态的 effect——此前规则轨不识别）；
- 消费端兼容：kind="custom_sink" 为新值，仅透传（`_call_has_confirmed_gap_exemption` 的 kind 集合不含它，行为与 not_sensitive 一致的豁免语义不适用）；
- explorer 轨（backend sink_taxonomy）不受影响（versions.yaml 结构仅新增可选 verified_arity 字段，backend 解析忽略未知字段）。

## 4. 待核验点

1. manual 回退的 same_package_leaf 兜底是否引入过宽匹配（同包任意 leaf 类调用同名方法即命中）；
2. `verified_arity` 三态与内置 family 三态的完全等价性（特别是 OPERATION_SIGNATURE_GAP 的 allowed_arities 字段形态）；
3. kind="custom_sink" 新值在下游（flow/gap 聚合、candidate schema、funnel）的透传安全性；
4. yaml 加载的性能（每进程一次）与缓存正确性（测试 monkeypatch 缓存重置）；
5. E7 残留声明的三项保留是否与"迁出"目标冲突（评审 E7 的完整闭环边界）。

## 5. 核验处置修订（2026-08-27，deepseek-v4-pro 核验 R-1~R-8 后）

- **R-1（中，采纳）**：增补 4 处未声明残留的 E7 注释（index_reader sport 方法名、dataflow VALIDATOR_METHODS wrapper 副本、detector _SERVICE_SENSITIVE_BINDER_PATTERNS、detector _review_priority 词表——后者为 E7 评审点名项）；§1.3 清单由 3 项扩至 7 项；
- **R-2（中，采纳）**：sensor 注释的保留理由更正为真实理由（"可表达但拆分收益低"，原"结构无法表达"与回退已实现 same_package_leaf 的事实矛盾）；
- **R-3（低，采纳）**：用例数勘误 9→8，核验后补 3 个边界用例（跨包 spoofing 拒绝、前缀放宽域内命中、缓存复用）共 11 个；
- **R-4（低，采纳（已实施））**：3 个边界测试落地（spoofing 用例含 containing_class 断言跨包排除、sport_xms 前缀命中固化放宽语义、二次调用缓存命中）；
- **R-5（低，采纳（已实施））**：同步脚本对 manual 条目增加 verified_arity 结构校验（须为非空 int 列表，非法即 CONFLICT）；
- **R-6（低，采纳（已实施））**：4 条 sport 条目 note 补记前缀放宽真实动机（sport_xms 跨子包/entry 形态召回，非"覆盖 same_package 形态"——同包由 same_package_leaf 覆盖）；
- **R-7（低，采纳（已实施））**：taxonomy_version 递增至 1.0.6；
- **R-8（低，采纳（已实施））**：同步脚本增加 manual 条目回退探针（9 条全部探针命中，PASS 82）；docstring 行号注释刷新（改为符号引用防漂移）。

核验后测试：全量 **1309 passed / 0 failed**（38.77s）；同步校验 **PASS 82 / CONFLICT 0 / ORPHAN 0**（73 base + 9 manual 全探针）。核验报告：`2026-08-27-p4-vendor-decoupling-audit.md`。
