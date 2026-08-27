# 评审报告：规则集质量评审报告（ruleset-quality-review）

> **任务编号**：ruleset-quality-review（文档审计，非任务轨）
> **评审日期**：2026-08-27
> **评审对象**：`docs/analysis/rules-review/2026-08-27-ruleset-quality-review.md`
> **评审模型**：deepseek-v4-pro（独立子 agent，只读评审，65 次工具调用）
> **状态**：第 1 轮（已闭合）

---

## 1. 评审结论摘要

该报告整体可信度较高：主线断言（E1-E8 的正则推演与零覆盖断言、E7 特调证据、7 项设计亮点、验收数据转引、基本计数）经独立核实绝大多数成立，行号引用总体精确，公允性良好（亮点与缺陷并陈、缓解因素如实交代）。但存在 2 处与代码/文件现状直接矛盾的错误断言（R-1 的 legacy 回退现状、R-2 的 custom sink 管线落地进度）、1 处机制描述反转（R-4）、2 处行号不可回查（R-5）及 1 处遗漏条目误报（R-3）。报告可用作修复依据，但须先更正上述条目，否则修复执行者会对 legacy 回退现状、custom sink 管线进度产生误判。

**重要时序事实**（主 agent 核实）：被审计报告基于 `versions.yaml` 81 行旧版撰写；报告落盘后提交 `76ac2c4`（feat(taxonomy): F3 sink taxonomy 首批 manual 扩充（4 条））将文件重构为 392 行并入册 4 条 manual 条目。R-2/R-5/R-9 的"矛盾"部分源于此数据源变更，部分源于报告作者真实 diff 失误（R-3）。

## 2. 问题清单

**【R-1】【高】** E8-2 断言与代码控制流相反：Provider flow 规则在无索引时并非"静默返回空、无 legacy 回退"。实际控制流为 `detector.py:458` 的 `elif rule_id in PROVIDER_FLOW_RULES and reader:` 不满足时落入 else 分支，经 `detector.py:500`（`reader.component_files(...) if reader else _component_files(component, legacy_files)`）完成 legacy 回退，再进入 `detector.py:510` 的 `_component_rule`——其中对全部 5 个 provider flow 规则均有专门分支（`detector.py:2007-2065`），可基于 legacy files 正常产出候选。成立的只有半个观察：无索引时不打 `LEGACY_INDEX_SCOPE` critical gap（对比 `detector.py:494`）。
修订建议：E8-2 改写为"回退 `_component_rule` 旧式全文件逻辑，功能可用但语义降级且缺 gap 标记"；修复建议从"补 legacy 回退"改为"补齐 gap 标记"。

**【R-2】【高】** 第二节"应用自封装存储层全部漏掉"与 versions.yaml 当前内容矛盾：文件已收录 4 条 manual 条目（`versions.yaml:356-391`：getPrefEncryptedUserId/getAccountId → data_disclosure、setStringPref/setIntPref → persistent_state_write，confirmed_at '2026-08-26'），恰含报告点名的两个方法。"全部漏掉"仅在 base 条目层面成立，对文件整体不成立，会误导读者认为人工管线尚未生效。
修订建议：改为"base 同步层不含应用自封装存储，经 2026-08-26 起的人工评审管线已入册 4 条 manual 条目，管线已部分生效但仅覆盖 44 个未命中 sink 中的少数"。

**【R-3】【中】** 遗漏条目清单中 `NotificationManager.notify` 不成立：旧版第 39 行/新版 145-149 行均在册（callback_event_injection）。其余 11 项经与 `dataflow.py:2840-2846/2905-2918/3040-3060/3086-3098/3100-3121/3135-3143/3162-3165` 逐项比对真实成立。
修订建议：从遗漏清单删除 notify，清单修正为 11 项。

**【R-4】【中】** E7 "leaves 匹配任意包名"的机制描述与实现相反：`_receiver_family_matches` 的 leaf 匹配要求 receiver_type 为裸简单类名（`dataflow.py:2532-2533` 的 `"." not in normalized and "$" not in normalized`），带包名的 FQCN 一律不匹配 leaves。
修订建议：改为"leaves 仅匹配不含包名分隔符的裸简单类名——索引 receiver_type 为简单名时任意包下同名类均命中（跨 APK 噪声）；为 FQCN 形态时则永不命中（死条目）"。

**【R-5】【中】** 两处 versions.yaml 行号引用不可回查（按当前 392 行版本）：E2 引用的 `versions.yaml:43` 实为 instantiate 条目，getLastLocation 在 160-164 行；"人工同步声明"在 3-5 行 description 而非第 9 行。（主 agent 按实情声明：报告基于 81 行旧版，旧版第 43 行确为 getLastLocation、第 9 行确在 description 块内——行号按旧版无误，但文档应随数据源更新。）
修订建议：行号更正为 versions.yaml:160-164 与 3-5，或注明版本锚点。

**【R-6】【中】** E8-4 示例名 "RECEIVER_EXPORTED_FLAG" 在规则集中不存在（30 条规则无此 ID）；相关常量为 RECEIVER_EXPORTED/RECEIVER_NOT_EXPORTED（`receiver_registration.py:14-15`），相关规则是 DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION。API 33+/targetSdk 34+ 的语义方向正确。
修订建议：改为"DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION 依赖的 RECEIVER_EXPORTED/RECEIVER_NOT_EXPORTED 标志语义只适用 API 33+/targetSdk 34+"。

**【R-7】【中】** 完整性遗漏：未指出两份数据源已发生的 taxonomy 语义冲突——versions.yaml 将 write（receiver_leaves 含 BluetoothOutputStream/UsbOutputStream/NfcOutputStream/ProtocolWriter，新版 342-355 行）归为 file_mutation，而 `dataflow.py:3007-3012` 将同 receiver 的 write 归为 device_protocol_output：同一调用在规则轨与 explorer 轨得到不同 taxonomy。此冲突在 81 行旧版即已存在，是比条目遗漏更强的"人工同步风险已兑现"论据。
修订建议：第二节补"双源 taxonomy 冲突实证"，并列入同步校验脚本的验收用例。

**【R-8】【低】** E7/E6/E8 清单不全：未列 `detector.py:3431` 的 Sport|Workout|Account 评分特调、`dataflow.py:2927-2929` 挂在 SensorManager family 的自研方法（startGymSensor/startStepSensor 等）；E6 未提 HOSTNAME_VERIFIER_ALWAYS_TRUE（`detector.py:3085-3099`）同样只匹配扁平方法体且方法名 verify 过泛；E8 未收录 `detector.py:97-100` 自认"当前无调用点"的 GUARD_RE 死代码。
修订建议：补入对应清单，保持同类问题全量枚举。

**【R-9】【低】** 计数与清单表述不精确：versions.yaml 当前 59 条（55 base + 4 manual，与报告同日入册）；"全部 31 个 rule.yaml"实为 30 个 rule.yaml + 1 个 versions.yaml；"shared 6 模块"只列了 5 个名字（第 6 个是 40B 的 `__init__.py`）。
修订建议：改为"30 个 rule.yaml + versions.yaml（59 条，其中 4 条 manual）"、"shared 5 个实质模块"。

**【R-10】【低】** 四处技术表述瑕疵：① E1 中被误判为权限的 4 字符是 `"b"),`（第 4 个是逗号）而非 `"b")`；② E2 修复建议"补 getLastKnownLocation（arity 1-2）"——公开签名仅 1 参（String provider）；③ E4 "targetSdk>=23 时未声明也默认 true"——allowBackup 默认 true 与 targetSdk 无关，targetSdk>=23 是 Auto Backup 特性门槛而非默认值门槛；④ E7 "均有注释说明"——FLOW_INTRINSIC_METHODS 与 VALIDATOR_METHODS 本身无逐条注释。
修订建议：按事实逐句修正。

## 3. 认可项（经独立核实成立，后续修订不应误改）

1. **E1 全部成立**：正则属实；逐字符否定的语义缺陷属实；嵌套逗号漏报推演确认成立；auxiliary 缓解（rule.yaml:7）；`_split_top_level_args`（detector.py:172）修复方向可行。
2. **E2 核心成立**：`getLastKnownLocation` rules/ 全目录 0 结果（独立 grep）；getLastLocation 挂 LocationManager family 属张冠李戴（FusedLocation API）；纯 framework 位置链路漏检后果正确。
3. **E3 全部成立**：判定条件属实；backend networkSecurityConfig 0 结果（独立 grep）；NSC 覆盖优先级符合官方语义；targetSdk<28 存量风险不覆盖属实。
4. **E4 全部成立**：`manifest.py:84` 仅解析 allowBackup；fullBackupContent/dataExtractionRules 0 结果（独立 grep）。
5. **E5 全部成立**：仅匹配 `"AES/ECB/` 字面前缀；`Cipher.getInstance("AES")` 默认 ECB 漏检正确；crypto 域恰 3 条规则。
6. **E6 全部成立**：`[^}]{0,800}?` 不能跨第一个 `}`（推演确认）；`([^{}]{0,400}?)` 无法匹配嵌套块；`return;` 空实现不命中；两规则均无 SSLContext.init 安装关联（独立验证 0 结果）。
7. **E7 主体成立**：sport_leaves 含小米前缀（注释自认）；finishSport 归类语义牵强；URL 校验 wrapper 名混入；Sport|Workout|Wear 词表；BluetoothOutputStream 等确非 SDK 类。
8. **E8-1/3/4（android_api 部分）/5 成立**：5 词 vs 14 词；search 单 match；30 个 rule.yaml 全部声明 "1-36"（全量验证）；severity 仅存于 RULE_META（全量验证 0 结果）。
9. **第二节转引数据与验收文档一致**：44/46 partial、55 条 taxonomy、validated=0 根因、两个扩展点方法名均与 gap-analysis 原文一致；遗漏清单 11/12 项真实成立（唯 notify 误报）。
10. **第三节 7 项设计亮点全部独立核实成立**：fail-closed（含 critical gap）；arity 三态校验；resolve 失败≠死代码（docstring 完整记录）；SIMPLE_GLOB 语义正确；索引只读边界；Provider CRUD descriptor 形状校验；动态 receiver 仅信任 framework/AndroidX owner。
11. **第四节零覆盖佐证全部成立**：PendingIntent/DexClassLoader/ObjectInputStream/readObject/SecureRandom/setSavePassword/parseUri/allowTaskReparenting/Log.d 在 rules/ 全目录均 0 结果（独立 grep）；route injection 词表确为 5 方法；setAllowFileAccess 只匹配显式 true。
12. **基本事实成立**：恰 30 条规则；detector.py 恰 3678 行、dataflow.py 恰 3382 行（逐行验证）；`backend/tests/test_manifest_fact_rules.py` 存在。
13. **第五节优先级有依据**：P1（taxonomy 扩充）与验收文档修复方向 #2 直接对应且有量化背书；抽查 `_provider_rule_candidates`/`_binder_rule_candidates`/`classify_operation_taxonomy` 全量分支，未发现报告之外更严重的确定性缺陷。

## 4. 边界检查表

| 检查项 | 结论 | 依据 |
|---|---|---|
| 事实准确性 | 有条件通过 | 主线断言准确；E8-2 控制流断言与代码相反（R-1）、"全部漏掉"与现状矛盾（R-2）、notify 误列（R-3）、E7 机制反转（R-4） |
| 证据可回查性 | 有条件通过 | detector/dataflow/authorization/index_receiver/manifest/rule.yaml 引用全部命中；versions.yaml 两处行号因数据源更新失配（R-5） |
| 完整性 | 有条件通过 | 覆盖充分；遗漏双源 taxonomy 冲突实证（R-7）与 E7/E6 同类实例（R-8），无致命漏项 |
| 公允性 | 通过 | 亮点全部核实为真且无夸大；缺陷均交代缓解因素；无压低代码质量抬高报告价值的倾向 |
| 可操作性 | 通过 | 修复建议具体可执行；唯 E8-2 建议需按 R-1 更正事实后调整 |

---

## 5. 处置记录（主 agent 回填，2026-08-27）

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 高 | **采纳**：E8-2 改写为"无索引时经 else 分支回退 `_component_rule`（detector.py:500/510）旧式逻辑，功能可用但语义降级且缺 `LEGACY_INDEX_SCOPE` gap 标记（对照 detector.py:494）"；修复动作改为"补齐 gap 标记" | 原报告 E8 表 #2 |
| R-2 | 高 | **采纳（按实情声明）**：versions.yaml 于报告落盘后经提交 `76ac2c4` 重构并入册 4 条 manual（versions.yaml:356-391，恰含报告点名的两个方法）——"全部漏掉"限定为 base 层，同时如实反映人工管线已部分生效 | 原报告第二节 |
| R-3 | 中 | **采纳**：notify 在旧版 39 行即在册（报告作者 diff 失误，非数据源变更），从遗漏清单删除，清单修正为 11 项 | 原报告第二节 |
| R-4 | 中 | **采纳**：E7 机制描述更正为"leaves 仅匹配裸简单类名（dataflow.py:2532-2533）——简单名形态时任意包同名类命中（噪声）、FQCN 形态时永不命中（死条目）" | 原报告 E7 |
| R-5 | 中 | **采纳（按实情声明）**：报告行号基于 81 行旧版（旧版 43 行即 getLastLocation）；按当前 392 行版本更正为 160-164 与 3-5，并在附录注明版本锚点 | 原报告 E2、第二节 |
| R-6 | 中 | **采纳**：更正为"DYNAMIC_RECEIVER_EXPORTED_NO_PERMISSION 依赖的 RECEIVER_EXPORTED/RECEIVER_NOT_EXPORTED 标志（receiver_registration.py:14-15）只适用 API 33+/targetSdk 34+" | 原报告 E8 表 #4 |
| R-7 | 中 | **采纳**：补"双源 taxonomy 冲突实证"（write 在 versions.yaml 归 file_mutation、在 dataflow 归 device_protocol_output——同一调用两轨不同分类，旧版即存在），并列入优先级 1 同步校验脚本的验收用例 | 原报告第二节、优先级表 #1 |
| R-8 | 低 | **采纳**：E7 补 detector.py:3431 评分特调与 SensorManager family 自研方法；E6 补 HOSTNAME_VERIFIER 同款扁平限制；E8 补 GUARD_RE 死代码（detector.py:97-100） | 原报告 E7/E6/E8 |
| R-9 | 低 | **采纳**：计数更正为"30 个 rule.yaml + versions.yaml（59 条 = 55 base + 4 manual）"、"shared 5 个实质模块" | 原报告头部、附录 |
| R-10 | 低 | **采纳**：四处措辞修正（`"b"),` 4 字符、getLastKnownLocation 单参签名、allowBackup 默认值与 targetSdk 解耦、"均有注释"限定） | 原报告 E1/E2/E4/E7 |

**闭合结论**：R-1~R-10 全部采纳（其中 R-2/R-5/R-9 属数据源时序失配、R-3/R-4/R-10 属报告作者表述失误、R-1 属断言过强、R-7/R-8 属真实遗漏）。原报告 `2026-08-27-ruleset-quality-review.md` 已于 2026-08-27 按处置记录完成修订（14 处编辑：头部修订记录与计数更正、E1/E2/E4/E6/E7/E8 逐条更正、第二节三处更新、优先级表 #1/#6、附录版本锚点）；修订不影响主线结论（2 处确定性 bug、sink taxonomy 封顶、修复优先级排序均经审计确认成立）。

---

## 附：审计方法

- 独立子 agent（deepseek-v4-pro 视角）只读评审，65 次工具调用：逐条打开 E1-E8 引用行号核对代码、逐字符推演正则语义、独立 grep 验证零覆盖断言、全量枚举 30 个 rule.yaml 验证计数与 severity 双源、逐项 diff 双数据源条目、与 gap-analysis 原文比对转引数据。
- 主 agent 复核 R-2 时序声明：git log 确认 `76ac2c4`（F3 sink taxonomy 首批 manual 扩充）晚于报告撰写时的 81 行版本。
