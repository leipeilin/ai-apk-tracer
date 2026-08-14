# P1 确定性语义分析实施进度

## 状态

P1 计划中的方法内数据流、跨方法传播、有效授权矩阵、GuardCoverage、Router/Fragment、started Service、动态 Receiver 和影响分类已完成代码实现及自动化验证。

当前自动化基线：

```text
后端测试：119 passed
内置规则契约：18 passed
TypeScript：通过
Vite 生产构建：通过
生产构建：1.55s
```

## 1. Validation-state 与方法内数据流

索引升级为 `2.6.0`，方法使用等长 raw/masked 文本提取：

- masked 文本负责排除注释、字符串和声明中的伪调用；
- raw 文本保留 Intent/Bundle key 和原始参数；
- call site 记录稳定 ordinal；
- method 记录 ordered flow IR；
- 链式调用的赋值绑定到最外层返回调用。

数据流引擎支持：

- 局部变量 strong update 与旧版本 kill；
- validation 绑定具体 value version；
- Intent/Bundle 对象别名与 key-slot；
- `putExtra` / `Bundle.put*` 精确 key 更新；
- `putExtras` / `putAll` 未知 key 的 may-overwrite；
- `replaceExtras` 清除旧 slot；
- `fillIn` 保守合并；
- receiver 与参数共同参与 Source/Sink 传播；
- `VALIDATED_SLOT_OVERWRITTEN` 与 `ROUTER_VALIDATION_BYPASS` 证据。

旧文件级正则 fallback 只能生成待确认候选，不能闭合确定性链。

## 2. 跨方法摘要传播

`RuleIndexReader.component_flow_scope()` 从精确组件 FQCN 入口沿 `resolved_target_id` 加载最小方法闭包，不再把同文件或同名方法全部加入。

已支持：

- 参数到 callee 参数；
- callee 返回值回传 caller；
- 跨文件参数对象 slot mutation 回写 caller；
- 参数到 Sink 与方法副作用摘要；
- 有限 worklist summary 合成；
- 递归、歧义和未收敛时生成 coverage gap。

同名、重载、多实现或未解析调用不能任选目标；`SYMBOL_TARGET_AMBIGUOUS` 和 `SUMMARY_FIXPOINT_LIMIT` 会阻止确定性闭合。

## 3. Effective Authorization Matrix

新增 `rules/shared/authorization.py`，按组件入口、操作、路径区域和访问模式生成版本化授权矩阵。

覆盖：

- application → component 权限继承；
- activity-alias → target Activity → application；
- Provider generic/read/write permission；
- query/openFile:r 与 insert/update/delete/openFile:w/call/applyBatch；
- path-permission exact/prefix/pattern；
- URI grant 的 read/write 替代授权路径；
- protectionLevel base + flags 和数字形式；
- 平台权限最小版本目录；
- authority 冲突、placeholder 和 reachability gap。

任何未知 protectionLevel、平台目录缺失或无法解析的授权事实保持 `unknown`，不能提升为 protected/strongly_protected。

## 4. Sink 级 GuardCoverage

Guard 不再按“组件内出现过关键词”判定，而是绑定实际入口和 Sink。

已支持：

- `enforce*` 位于 Sink 前且未被 catch 后继续；
- `check*` 返回值进入 fail-closed return/throw 分支；
- `getCallingUid/Pid` 仅作为 identity source，不单独构成 Guard；
- Guard 在 Sink 后不生效；
- 唯一 resolved wrapper Guard；
- 多入口分别计算，不互相覆盖；
- `present_effective`、`present_partial`、`present_bypassable`、`absent`、`unknown`。

授权 unknown 或 Guard unknown/partial 会阻止 L3；effective Guard 会关闭或降级未授权访问候选。

## 5. 高价值组件专项分析

### Router / Fragment

- 检测已验证目标 key 被 `putExtras`、`replaceExtras` 或 `fillIn` 覆盖；
- 记录 old/new version、key、覆盖操作和最终 Sink；
- 追踪外部 className/fragmentName 到 `Class.forName`、`Fragment.instantiate`、FragmentFactory 或反射构造；
- 固定类名和明确 fail-closed allowlist 不报告；
- 复杂反射目标形成 coverage gap。

### Started Service

- 从 `onStartCommand` 提取 action/extras 事件；
- 将事件分支绑定到 resolved 跨方法副作用；
- 输出 event/key、条件位置、method path、影响分类和利用前置条件；
- 仅“副作用可达”但没有外部事件控制关系时，不闭合链并生成 `SERVICE_EVENT_EFFECT_BINDING_UNKNOWN`。

### Dynamic Receiver

- 绑定 `registerReceiver` 调用点、Receiver 类型、IntentFilter action、API/flag 和具体 `onReceive()`；
- 沿精确 `onReceive` 入口追踪到副作用；
- `RECEIVER_NOT_EXPORTED`、LocalBroadcast 和受保护 action 不作为外部攻击面；
- 同名或无法解析的 Receiver 形成 critical gap。

## 6. Operation taxonomy

方法摘要和专项分析统一使用：

- `data_disclosure`；
- `persistent_state_write`；
- `device_protocol_output`；
- `callback_event_injection`；
- `location_sensor_collection`；
- `connection_session_control`；
- `ui_navigation`；
- `file_mutation`；
- `database_mutation`。

类型未知时保留 `unknown_effect`，不能按方法名直接定级。

## 7. 索引存储优化

索引 `2.7.0` 使用：

- external-content FTS：完整伪源码只保存在 `files.content`，FTS shadow table 不重复保存正文；
- zlib 压缩高基数 `flow_ir`、method summary 和 call arguments；
- 稳定紧凑 JSON；
- 移除未使用的长 `symbol_key` / FQCN descriptor B-tree；
- 移除与 `(method_id, ordinal)` 重复的 call-site 索引。

真实 49,091 文件 APK：

```text
v2.6 数据库：约 2008 MiB
v2.7 数据库：1506.28 MiB
体积下降：约 25%
v2.7 构建：203.79 秒
```

该改造保持 immutable/query_only 查询、安全路径和证据回查协议不变。

## 8. 最终真实 APK 验收

最终 run：`20260731T140731Z_2a80fc5a8735_24905522`。

```text
状态：completed
总耗时：约 6 分 53 秒
规则：18/18 完成，规则级失败 0
候选：593
Finding：293（L1 268，L2 25）
确定性链闭合：16
可定级 Finding：0
待复核 Finding：293
```

关键组件：

- `SportXmsService`：Binder L2，跨方法链闭合；
- 两个 `SportService`：均从仅 L1 提升为 Service IPC L2，但受混淆调用/Guard gap 影响未闭合；
- `WearableXmsService`：Binder L2 与动态 Receiver 攻击面；
- `RouterActivity`：进入 Activity 数据流 L2，复杂跨 helper validation overwrite 尚未闭合；
- `WidgetControlFileProvider`：caller check、URI→File、mutation 三类 L2；
- `DeviceProvider`：caller check L2；
- 两个 `CommonBaseActivity`：确认无权限导出 L1，跨混淆 helper 的 Fragment 反射仍保守保留 gap。

JADX partial（389 errors）、13 个超限索引文件、AI 关闭以及 4 个混淆 Binder 返回类型歧义使 run 正确标记为 `analysis_incomplete=true`。系统没有把这些未覆盖范围当成“安全”。

## 9. 多次扫描一致性

Finding 的语义哈希在同一 APK 多次扫描时可能相同。持久化 ID 现使用 `run_id + semantic finding id` 作用域，避免跨 run 主键冲突；payload 同时保留 `base_id` 用于语义对比。旧的无作用域链接仅在全库唯一时兼容解析。

## 10. 自动化覆盖

新增重点回归：

- value version、last reaching definition 和 validation kill；
- Intent/Bundle key-slot 与 wildcard overwrite；
- 跨方法返回与跨文件 slot mutation；
- Router 校验后覆盖及三个阴性对照；
- 权限继承、path permission、URI grant、protectionLevel；
- enforce/check/UID/wrapper/旁路 Guard；
- Fragment 反射阳性与 allowlist 阴性；
- Service 事件控制与仅可达副作用负例；
- Dynamic Receiver 唯一绑定、NOT_EXPORTED、LocalBroadcast、歧义目标；
- 九类 operation taxonomy。

## 仍保守降级的边界

- 完整 Java/Kotlin 编译器 AST 和全 CFG；
- 复杂循环、异常边、协程、Kotlin scope function；
- 接口多实现、动态代理、反射、Native/JNI；
- Smali 寄存器级精确 SSA；
- 复杂跨线程调用者身份与 Binder `clearCallingIdentity`；
- 未收录的平台权限和受保护广播目录。

上述情况必须生成 coverage gap 或保持 pending，不得虚构唯一调用链、有效 Guard 或确定影响。
