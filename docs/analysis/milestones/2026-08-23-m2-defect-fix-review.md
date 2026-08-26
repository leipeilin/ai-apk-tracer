# 任务评审报告：M2-DEFECT-FIX

> **评审对象**：`2026-08-23-m2-defect-fix-implementation-plan.md`、`2026-08-23-m2-defect-fix-acceptance-plan.md`
> **评审日期**：2026-08-23
> **评审模型**：deepseek-v4-flash（独立子 agent，只读评审）
> **状态**：第 1 轮（已闭合）

## 1. 评审结论摘要

方案对三缺陷的根因定位准确、修复路径与既有先例（JadxAdapter 的 wait_for+killpg、failure="network" 分类）对齐度高，总体可行。但 D-1 的 kill 后二次 communicate() 无兜底是残留挂死点、D-2 的默认值硬编码与"默认=read+60"承诺不符是两处需修订的实质缺陷。D-3 的 prompt 组合逻辑自洽但约束间优先级未声明，且验收仅文本断言、无行为级验证。

## 2. 问题清单与处置记录

| 编号 | 严重度 | 问题摘要 | 处置 | 修订动作 |
|---|---|---|---|---|
| R-1 | 高 | kill 后二次 communicate()（回收）无超时——killpg 失败 fallback 只杀直接子进程，Java 派生进程持管道写端时 communicate 永等 EOF；测试 fake 永挂时回收调用同样挂死 | **采纳** | 回收调用包 `wait_for(..., 10)`；测试 fake process 有状态（kill 置位后 communicate 立返空）——实施与测试均按此 |
| R-2 | 高 | request_timeout_seconds 默认 180 硬编码与 §1"默认=read+60"承诺不符——read_timeout 配 300/3600 时兜底先于 read 触发（正常长响应被误杀归 network 重试） | **采纳** | 仿 apply_legacy_read_timeout 加 model_validator：未显式配置时取 read_timeout_seconds + 60 |
| R-3 | 中 | 超时用 ValidationError（422 客户端语义）与同类超时先例（JADX_TIMEOUT 用 DependencyError）分类漂移 | **采纳** | 改 DependencyError("Manifest 解码超时", "MANIFEST_DECODE_TIMEOUT") |
| R-4 | 中 | 约束 10 与约束 6（预算将尽输出部分链）冲突场景未声明优先级——预算尽且无上下文时模型无所适从 | **采纳** | 约束 10 本体补"预算将尽且无可用 code_context 时仍不得产链，仅输出 component_summary + done=false + read_requests，由驱动层预算终止承载" |
| R-5 | 中 | D-2 伪代码嵌套位置不明（response None 分支须在 lease/semaphore 结构内；漏判 None 则 fatal_response_classification(response) AttributeError） | **采纳** | 实施时显式保证：wait_for 在 semaphore 持有期内、TimeoutError 分支内联于 try/finally 与 lease 块中（finally 释放必经）；response None 判定先于消费 |
| R-6 | 中 | D-3 验收仅文本断言无行为验证（缺陷本身是行为级 validated=0） | **采纳（可选项）** | 验收补可选探针项（真实模型单入口首轮观测应无 chain_proposals 且含 read_requests——依赖真实 AI 可延后 M4；文本断言为基线门槛） |
| R-7 | 中 | config/default.yaml 显式维护 AI 超时字段族，新字段缺位致可发现性漂移 | **采纳** | default.yaml 加 request_timeout_seconds 注释条目（注释行——默认值由 validator 动态派生，yaml 不显式设值） |
| R-8 | 低 | 实施方案 §4 与验收方案编号错位一位 | **采纳** | 以验收方案 A-1~A-10 编号为准 |
| R-9 | 低 | 措辞精度：约束 10"（尚未读码）"未涵盖"read 取回为空"；to_thread 超时后删除线程不可取消的幂等性未说明 | **采纳** | 措辞改"无可用 code_context（为 null 或未含已读代码）"；to_thread 泄漏线程与下次前置清理并发 rmtree 同目录在 ignore_errors=True 下幂等安全——方案注明 |

## 3. 认可项（摘）

1. killpg + start_new_session 完整对齐 JadxAdapter 先例；except 兜底 process.kill() 更稳。
2. rmtree 的 to_thread+wait_for 精准命中原缺陷机制（三处同步调用阻塞事件循环）。
3. D-2 独立 TimeoutError 分支不并入 httpx.HTTPError 的取舍正确（取消竞态下两路均通向重试，行为收敛）；failure="network" 复用既有分类，调用方零改动。
4. 约束 10 与约束 5 构成逆否闭环（无链→done 必须 false），质量门禁从校验时前移到生成时。
5. registry 哈希同步可行，A-9 --check 复验构成闭环。
6. 回退分层合理（D-2 超大值软禁用 / D-3 revert+重同步）。

## 4. 闭合结论

R-1~R-9 全部采纳；实施按处置记录执行。验收编号以验收方案为准（A-1~A-10）。
