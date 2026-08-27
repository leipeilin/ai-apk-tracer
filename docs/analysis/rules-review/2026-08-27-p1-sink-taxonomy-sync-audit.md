# 核验报告：P1 sink taxonomy 扩充（ruleset-quality-review）

> **任务编号**：P1（sink taxonomy 扩充：base 层同步 + write 冲突修复 + 同步校验脚本）
> **核验日期**：2026-08-27
> **核验对象**：`docs/analysis/rules-review/2026-08-27-p1-sink-taxonomy-sync.md` 及其变更（versions.yaml base 55→73、scripts/check_sink_taxonomy_sync.py）
> **核验模型**：deepseek-v4-pro（独立子 agent，只读评审，57 次工具调用）
> **状态**：第 1 轮（已闭合）

---

## 1. 核验结论摘要（子 agent 原文）

达标情况：核心三项成立，任务源范围有一项沉默遗漏。write 双源冲突修复经逐 receiver 核对成立（versions.yaml:453-462 文件流归 file_mutation、463-470 四设备流归 device_protocol_output，与 dataflow.py:3007-3012、3123-3143 逐条一致，yaml 中已无混合 leaves 残留）；base 层 73 条经全量静态逐条探针推演，taxonomy 与 receiver 证据全部一致（含 $/. 双形态、constructor 双形态路径）；同步校验脚本的探针机制（空 descriptor→OPERATION_SIGNATURE_GAP 绕过 arity）经独立推演成立，且能捕获历史 write 冲突。主要风险：评审优先级 #1 明示"含 E2 补录"（getLastKnownLocation），实施与报告均未涉及且无降级记录；脚本未接入任何测试/CI；§3.1 评审表核算失真（混入 4 个非"44 未命中"候选、漏审 1 个真成员）；个别判定论据存在事实错误（registerReport 方法不存在）。

## 2. 问题清单（子 agent 提出）

**【R-1】【高】** E2（getLastKnownLocation 补录）属任务源明示范围（review:143 "含 E2 补录"），被沉默遗漏——实施与报告均未涉及且无降级记录；dataflow.py:2913-2916 仍只有 requestLocationUpdates/getLastLocation/getCurrentLocation，全仓 grep `getLastKnownLocation` rules/ 0 结果。
**【R-2】【中】** §3.1 "44 个未命中 sink 评审表"核算失真：44 个真成员中 ImageSelector.onResult（candidates.json:1122-1156）未出现在评审表；反之混入 4 个非成员（saveCallback:140-205、Video start:2343-2394、Unverified:222-259、Unspecified:664-699——notes 为回查失败类）；"混淆名 3"实列 4 名、"非 sink 21"实列 24 名；表内合计 43≠44。
**【R-3】【中】** PushClient.getInstance 异议的结论成立但论据 "registerReport" 为不存在的方法（全反编译源 0 结果）；真实数据面是 PushClientImpl.register（token 写入 Bundle 后经 sendCommand 外发，PushClientImpl.java:16-40）。
**【R-4】【中】** 同步校验脚本无任何自动化接入（全仓 grep 仅脚本自身与 P1 报告）；评审核心风险"两份数据同步完全靠人工"（review:100/:155）仅在被记得手动运行时才被捕获。
**【R-5】【中】** 脚本探针三类结构性假阴性：① 参数敏感分支（PFD.open 只读降级 dataflow.py:3105-3111 → data_disclosure，yaml 固定 file_mutation，探针 arguments=[] 恒走默认路径）；② receiver 级反向缺口（dataflow 为已有方法新增 family 时 CONFLICT/COVERAGE 均无信号）；③ same_package_leaf 路径（dataflow.py:2776-2779）探针下恒 False。
**【R-6】【低】** 脚本与报告的"versions.yaml 头部声明"属 misattribution——versions.yaml:1-5 无此字样，宽松匹配声明实际位于 backend/app/analysis/sink_taxonomy.py:43-47 与 :134-135。
**【R-7】【低】** §3.2-3 COVERAGE 计数错误：实际 17 项（弱敏感 7 + sport/sensor 9 + toString 1）而非 15/7；`put` 被错误归入"UI 导航与回调"弱敏感类（实为 Editor family 的 persistent_state_write，dataflow.py:3041；Map.put 才是 not_sensitive）。
**【R-8】【低】** 两处披露缺口：① SP 族 leaves [Editor] 宽松偏离（dataflow exact-only，dataflow.py:3036-3038）被新条目继承而报告未披露；② "1270 passed" 无测试输出留痕。

## 3. 认可项（子 agent 核实，节选）

1. write 冲突修复成立（versions.yaml:453-470 与 dataflow.py:3007-3012/3123-3143 逐 receiver 一致，无残留混合条目）；
2. base 73 条静态逐条一致（含双形态探针路径；connect 条目的 HttpURLConnection leaves 经 URL family 命中属正确落位）；
3. "新增 18 条"账目自洽（73−55=18 = 表格 17 方法 + write 拆分净增 1）；评审"11 项遗漏清单"全部落位；
4. 脚本探针核心机制、验收用例有效性（历史 write 冲突可被捕获）、退出码语义均独立推演成立；
5. §3.2 缺陷 1（promote 锚点）成立：自环末跳 + receiver 反查静默失败 + CLI 回退 = 无约束条目，消费端任意 receiver 命中（sink_taxonomy.py:154-155）；修复方向正确，建议同时拒绝 from==to 自环锚点；
6. §3.2 缺陷 2（$/. 分隔符）成立；
7. PushClient.getInstance 不入册结论成立（候选自评 "no sensitive sink identified" 与验收报告判断矛盾，不入册正确）；
8. saveCallback 待定处置恰当（候选链质量差：末跳自环、1/3 跳验证；真实数据面写 setStringPref 已在册，XmAdUtil.java:43-55/:91）；
9. 4 条 manual 条目溯源真实（run_id/candidate_id/confirmed_at 字段与 candidates.json 一致）；
10. 消费端兼容性未破坏（新条目全用既有 9 类 taxonomy，既有测试断言不受影响）。

## 4. 边界检查表（子 agent 原文）

| 检查项 | 结论 |
|---|---|
| 条目一致性（73 条 vs dataflow 分支） | 通过 |
| 冲突修复（write 拆分 + 无残留冲突） | 有条件通过（PFD.open 参数敏感分歧应记录在案） |
| 脚本正确性 | 有条件通过（三类结构性假阴性 + misattribution + 无 CI 接入） |
| 评审公允性 | 有条件通过（核算失真 + 论据事实错误） |
| 遗漏检查 | 有条件通过（E2 沉默遗漏 + Editor-leaf 偏离未披露） |

---

## 5. 处置记录（主 agent 回填，2026-08-27）

| 编号 | 严重度 | 处置 | 落点 |
|---|---|---|---|
| R-1 | 高 | **采纳（落档方案 ②）**：E2 显式移交优先级 #2 执行（需先修 dataflow family 再同步 yaml，纯 yaml 侧补录会造成 ORPHAN）——P1 报告 §1.1 增"范围落档"段 | P1 报告 §1.1 |
| R-2 | 中 | **采纳**：§3.1 表重制——补 ImageSelector.onResult 评审（入口/回调形态，不入册）、4 个非成员候选（saveCallback/Video start/Unverified/Unspecified）标注"非 44 成员、顺带评审"、计数更正（非 sink 23、混淆名 4）、合计口径写明 44 成员 + 4 顺带 | P1 报告 §3.1 |
| R-3 | 中 | **采纳**：异议论据更正为 PushClientImpl.register（PushClientImpl.java:16-40，token 经 Bundle→sendCommand 外发），维持不入册结论 | P1 报告 §3.1 |
| R-4 | 中 | **采纳（已实施）**：backend/tests/test_sink_taxonomy.py 新增 `test_versions_yaml_synced_with_dataflow`（subprocess 调用脚本断言 exit 0）；脚本 docstring 注明 CI 接入点。19/19 通过 | test_sink_taxonomy.py、脚本 docstring |
| R-5 | 中 | **采纳（文档化）**：脚本 docstring 增"探针的结构性不可探测边界"段（参数敏感/receiver 级反向缺口/same_package_leaf 三类）；PFD.open 分歧在 P1 报告 §3.2-4 记录在案（以 file_mutation 为准）；COVERAGE 升级（方法×receiver 粒度）记为中期方向不实施 | 脚本 docstring、P1 报告 §3.2 |
| R-6 | 低 | **采纳（已实施）**：脚本两处文案改为"backend 消费端（app/analysis/sink_taxonomy.py）声明的宽松匹配口径" | 脚本 :16、:153 区域 |
| R-7 | 低 | **采纳**：§3.2-3 更正为 17 项（弱敏感 7 + sport/sensor 9 + toString 1），put 定性更正（Editor family persistent_state_write、裸名过泛暂缓） | P1 报告 §3.2-3 |
| R-8 | 低 | **采纳**：§1.1 补"既有主动偏离披露"段（SP 族 leaves [Editor] 宽于 dataflow exact-only）；§2 附验证命令与耗时（1270 passed，38.57s） | P1 报告 §1.1、§2 |

**闭合结论**：R-1~R-8 全部采纳并落实（R-4/R-6 代码与文案已改、R-1/R-2/R-3/R-5/R-7/R-8 报告已修订）。遗留移交项：① E2 补录 → P2（dataflow.py:2913 + versions.yaml 同步）；② promote_custom_sink.py 锚点修复（拒绝自环锚点 + receiver 反查失败报错）→ 建议随 P2 一并处理；③ COVERAGE 粒度升级 → 中期。核验后测试：test_sink_taxonomy.py 19/19 通过。
