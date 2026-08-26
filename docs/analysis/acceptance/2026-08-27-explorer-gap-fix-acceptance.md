# 探索轨偏差修复验收记录（F1/F2/F3——2026-08-27）

> **依据**：`explorer-track/2026-08-27-explorer-gap-fix-plan.md` + 各任务子代理核验（deepseek 视角）与主代理处置闭合
> **流程**：每任务实施 → 子代理核验 → 主代理采纳处置 → 提交（用户指定流程）

## F3：sink taxonomy 首批 manual 扩充（76ac2c4）

| 验收 | 结果 |
|---|---|
| A3-1 评审清单 | ✅ 44 条四维判定（A4/存疑6/B34）——`explorer-track/2026-08-27-sink-taxonomy-review-checklist.md` |
| A3-2 版本与回归 | ✅ taxonomy_version 1.0.0→1.0.4；1255 passed（当时基线） |
| A3-3 解封数 | ✅ **4 候选 partial→validated**（revalidate 实时确认——run 级 validated 0→4，探索轨首次） |
| A3-4 可审计 | ✅ promote 用法 A（run/候选溯源）+ 清单文档 + git |

## F1：golden 组件域过滤（7c3ce0a——含核验 V-1~V-6 闭合）

| 验收 | 结果 |
|---|---|
| A1-1 单测 | ✅ 跨 APK 剔除/清单缺失兼容/聚合×域过滤/scope_keys 优先级（+4 用例） |
| A1-2 shop 口径 | ✅ hit_total 6→**1**（excluded 7）——`evaluation/baselines/m4-shop.json` |
| A1-3 回归 | ✅ 全量 **1259 passed / 0 failed** + `sync-ai-protocol.py --check` 退出码 0（本记录为正式留痕——F1 核验 V-7） |
| 核验闭合 | V-1 撞名反向污染→scope_keys 职责分离；V-2 component_scope 透明；V-3/V-4 聚合×域过滤守卫；V-5 死代码；V-6 README；V-7 标注规范入 README |

**health 口径**（核验后确认）：hit_total 6→**4**（excluded 3）——4 个 health 真 hit case 成为 health run 的有效分母。

## F2：探索 prompt sink 敏感度约束（本次提交——含核验闭合）

| 验收 | 结果 |
|---|---|
| A2-1 协议断言 | ✅ 九类 token + 禁令全 token（finish/onBackPressed/Log/setResultData/getInstance/init*/handleIntent/syncPluginById）+ **taxonomy 交叉校验**（解析 versions.yaml 断言 9 值对齐防漂移）+ 论证通道限定断言 |
| A2-2 行为验收（探针 v8，shop dc24a077 同 6 入口） | ✅ **B 类 sink 归零**（产出 2 链 sink 均敏感：saveCallback URI 存储/getAccountId 账号读取——对照 v1 onInitCTA/v7 broadcastLogin 的 A/B）；**validated=1 探针史上首次**（getAccountId 命中 F3 扩充条目——F2+F3 闭环实证）；seed_hit_rate 1.0 不回退；D-3 零违规。**限定说明（核验 V-5 采纳）**：样本 n=2（6 入口单次）——方向性验证，全量统计待下次完整 run；链 1（saveCallback）模型自述 needs_expansion（方法体未读）——敏感性为方法名语义推断 |
| A2-3 回归 | ✅ 全量 **1259 passed / 0 failed** + sync-ok（本记录留痕） |
| 核验闭合 | V-1 论证通道限定（仅隐私封装方法）+ 禁令补业务中间类目；V-3 **探针校验结果持久化**（candidates 回写带 validation + probe-summary.json 落盘）；V-4 断言强化；V-6 prompt ⑧ 示例改库内方法（文件写——去读侧声称）；V-7 本验收记录 |

## 核验发现的 backlog（不阻塞，记录在案）

1. **重复请求空转变体**（F2 核验 V-2）：模型可字面满足空转禁令但重复相同 read_requests（v8 DataMessageCallbackService 4 轮重复）——驱动层重复请求检测 + "无敏感结论"干净出口（与 F5 目标引导一并设计）；
2. **taxonomy file_mutation 读侧缺口**（V-6）：库内无文件读条目——待真实候选出现后按升级闭环扩充；
3. **约束编号非单调**（V-8）：历史追加痕迹——重排会破坏既有引用，不动。

## 总验收状态

- 全量 **1259 passed / 0 failed**；sync --check 0；
- **F4（入口覆盖透明化）待实施**——本记录在 F4 完成后补 F4 节并终验；
- 加分项（health run 全量验证 F1 口径下的 4 case 真分母）待用户指示。
