# 人工审查优化实施进度

## 总体状态

本轮已完成优化方案中的 **P0 实现与自动化验收闭环**。

- P0 实现：100%；
- P0 自动化验证：完成；
- 原人工审查 APK 真实复测：待重新上传样本执行；
- P1：已建立部分基础设施，但不计入本轮 P0 完成度。

## P0 已完成

### 1. 统一可信度与完整性闸门

- JADX partial、索引跳过文件、规则失败、AI 跳过、失败和未完成统一生成 coverage gap；
- run、candidate、Finding、UI 和报告使用同一完整性事实；
- AI 返回 `analysis_complete=false` 且没有有效扩片请求时标记 `ai_incomplete`；
- AI 成功和失败候选聚合时输出 `ai_partial`，成功成员不能掩盖异常；
- 聚合只使用 primary 候选自身的 Source、Sink、传播和确定性状态，禁止跨候选拼接闭合链；
- Source、Sink 和位置均须回查共享索引；只有确定性链、方法内或跨方法传播、有效 Guard 状态和无关键 gap 同时满足时才能晋级 L3。

### 2. 结构化调用点索引

共享 SQLite 索引升级为 `2.2.0`，新增：

- `call_sites`：receiver、方法名、参数、赋值目标、起止行、表达式类型；
- `summary_json`：参数到返回值、参数到 Sink、字段读写、Guard 和副作用摘要；
- Java/Kotlin 调用点只从方法可执行区域提取；
- Smali `invoke-*` 转换为统一调用点；
- 注释、字符串、import 和方法声明不会进入调用点事实。

### 3. Binder/AIDL 专项闭环

- 从 `onBind()` 返回实例或返回字段追踪 Binder 类；
- 普通 `extends Binder` 不再自动视为远程 AIDL；
- 要求 `*.Stub`、`IInterface` 或真实 `onTransact` 分派事实；
- 解析 `TRANSACTION_* = N`、`case N`、接口方法调用；
- 记录 Parcel read/write 类型；
- 只有 transaction 到敏感接口方法闭合时才设置 `deterministic_chain_verified=true`；
- 组件权限或有效调用者 Guard 会关闭或降级候选。

### 4. FileProvider 专项闭环

- 解析 Provider meta-data 引用的 `res/xml/*paths*.xml`；
- 记录 files/cache/external 等可访问根类型；
- 识别 `r/w/wa/rw/rwt` 访问模式和只读/可写能力；
- 联合 component/read/write/path permission、URI grant 和 grant-uri-pattern 判断授权；
- canonical 目录边界区分：
  - 安全：equals root 或 `startsWith(root + separator)`；
  - 不安全：仅字符串 `startsWith(root)`；
- 安全 canonical 边界不再被标记为已确认路径穿越；
- authority 按分号拆分，并识别交集冲突；
- 重复 authority 作为关键阻塞条件，使报告保持待确认。

### 5. Provider、权限和广播规则收紧

- Provider 空实现、固定返回和 `UnsupportedOperationException` 不构成 mutation；
- openFile 只读模式与写模式分离；
- 未知平台权限不再自动视为 signature 强保护；
- normal/dangerous 权限按条件保护处理；
- 隐式广播必须证明同一 Intent 变量从敏感 `putExtra` 进入 `sendBroadcast`；
- 动态 Receiver 保留调用点、API/flag、Receiver 绑定和副作用证据；无法绑定时形成覆盖缺口。

### 6. 报告、复核和 UI 状态闭环

- 报告标题在 critical gap 存在时增加“待确认：”；
- 报告展示 JADX、索引跳过、规则失败 N/18、AI 状态和跳过原因；
- 报告携带 coverage/blocking gaps 和实际动态验证状态；
- `report_payload.schema.json` 已同步状态枚举和必填字段；
- 人工确认和误报都必须填写理由；
- Pydantic 校验错误上下文被安全序列化为 422；
- 任务详情显示扫描不完整告警和 coverage gap；
- 时间线显示 JADX、规则和 AI 阶段统计；
- Finding 列表显示 AI 状态，详情显示 coverage/blocking gaps；
- UI 支持 `ai_incomplete` 和 `ai_partial`。

## 自动化验证

```text
后端测试：38 passed
规则契约：18 passed
TypeScript：通过
Vite 生产构建：通过
生产构建耗时：1.47s
```

新增或强化的 P0 回归覆盖：

- 调用点排除注释、字符串和声明；
- AIDL transaction code、接口方法与 Parcel read/write；
- LocalBinder 负例；
- safe/unsafe canonical 边界；
- 分号 authority 冲突；
- openFile 只读模式；
- Source/Sink 无效位置阻止 L3；
- 混合 AI 状态显示为 partial；
- critical gap 报告待确认；
- 人工确认缺少理由返回 422。

## 仍需执行的操作性验收

原人工审查 run 的 APK 和共享索引已被清理，当前无法直接重算真实样本指标。需要重新上传同一 APK，执行：

1. 核对 13 个历史错误 high 是否全部消失；
2. 核对 SportXmsService transaction 是否完整列出；
3. 核对 WidgetControlFileProvider paths、模式和 authority 冲突；
4. 核对 V-01 至 V-05 的自动发现或复核优先级；
5. 记录真实误报下降数据。

这属于真实样本复测，不影响 P0 代码与自动化验收已经完成的结论。

## P1 边界

以下能力属于后续 P1，不作为 P0 阻塞项：

- 完整 Java/Kotlin AST 与 CFG；
- 通用字段、返回值和跨方法污点固定点传播；
- 复杂虚调用、接口多实现、反射和 Native 解析；
- 完整 Guard 支配分析；
- started Service 通用状态机；
- 动态 Receiver 到复杂 `onReceive()` 业务副作用的完整绑定。
