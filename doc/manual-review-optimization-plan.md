# AI-APK-Tracer 人工审查反馈优化方案

## 1. 目标与原则

本方案基于 `manual-vulnerability-analysis.md` 的人工增强复核结论，目标不是简单“增加更多规则”，而是解决两个核心问题：

1. **误报失控**：13 个自动 high Finding 按原描述全部不成立；
2. **高价值漏报/低估**：真正重要的 Binder、FileProvider、started Service 和 Provider 泄露风险，主要由人工从 L1 攻击面继续追踪发现。

优化原则：

- L1 是攻击面事实，不直接等于漏洞，也不能被丢弃；
- L2 必须包含至少方法级传播事实，不再接受“同文件共现”作为链路；
- L3 必须包含可回查的数据流、调用方保护和影响证据；
- AI 是语义复核器，不是证据生成器；AI 失败不得提高风险等级；
- 严重性由“可达性 + 调用者权限 + 实际副作用 + 前置条件”决定，不由规则 ID 或关键词决定；
- 静态不完整必须在任务、Finding 和报告三个层级显式传播。

## 2. 审查反馈与当前实现差距

| 审查问题 | 当前实现 | 判断 |
|---|---|---|
| 641 MiB 索引复制给每条规则 | 已改为单份共享只读 `analysis.sqlite3` | 已解决 |
| JADX 退出码 3 导致整任务失败 | 有可用 Manifest 时标记 `partial` 并继续 | 已解决 |
| 缺少方法级代码切片 | 已有 `ContextBuilder` 和结构化扩片 | 基础能力已解决 |
| Source/Sink 把 import、声明、字符串当调用 | `detector.py` 仍对完整文件正文做正则 | 未解决，P0 |
| 同文件 Source/Sink 被当作传播 | 仍生成“位于同一组件代码范围”的路径 | 未解决，P0 |
| AI 55/55 失败仍出现 high | AI 失败 gap 当前 `critical=false`，严重性仍可继承 hint | 未解决，P0 |
| Provider mutation 把空实现/方法声明当写操作 | 仍使用 `insert/update/delete/applyBatch` 词法匹配 | 未解决，P0 |
| Binder 规则不了解 Stub、transaction 和 Guard | 只检查 `onBind/Binder/Stub` 关键词及任意 Sink | 未解决，P0 |
| 权限语义不完整 | 已解析自定义权限和组件权限，但 read/write/path permission 未统一计算有效强度 | 部分解决，P1 |
| FileProvider canonical `startsWith` 被当安全 | 目前发现任何 canonical/startsWith 就直接排除候选 | 反向误判，P0 |
| 动态 Receiver API 分支与 flag 粗糙 | 仍按文件是否出现 `RECEIVER_NOT_EXPORTED` 推断 | 未解决，P1 |
| 报告未突出分析不完整和 AI 失败 | 正式报告 payload 未携带 run manifest 完整性摘要 | 未解决，P0 |
| “AI候选”误导 | 数据库默认 `review_status=ai_candidate`，与 AI 是否执行无关 | 未解决，P0 |
| evidence 保存全部 Manifest 组件 | `manifest_components` 仍写入全部组件 | 未解决，P2 |
| 人工复核未同步 evidence 快照 | 仅更新 SQLite review 状态 | 未解决，P2 |
| 缺少 prompt/model/version/hash | AI cache 有轨迹，但缺少完整可复现元数据 | 未解决，P2 |

## 3. P0：先阻断错误高危与修复专项漏报

### P0-1 建立统一“分析完整性与可信度闸门”

#### 改造目标

AI 未执行、失败或证据链不闭合时，L2 不能直接继承 `high`；任务 `analysis_incomplete=true` 时，报告和 UI 必须突出覆盖缺口。

#### 数据结构

给每个 candidate/finding 增加：

```json
{
  "analysis_status": "rule_only | ai_completed | ai_failed | ai_skipped | human_confirmed",
  "dataflow_status": "not_proven | intraprocedural | interprocedural | verified",
  "authorization_status": "unprotected | protected | conditional | unknown",
  "impact_status": "potential | statically_confirmed | dynamically_confirmed",
  "analysis_incomplete": true,
  "coverage_gaps": []
}
```

#### 判级规则

- L1：固定 `informational`；
- L2 + `dataflow_status=not_proven`：固定 `pending`；
- L2 + AI 失败/跳过，且没有确定性专项规则证明完整链：固定 `pending`；
- L2 + 方法内 def-use + 未知 Guard：最高 `medium` 或 `pending`；
- L3：允许按实际影响定级；
- 人工确认：允许独立调整等级，但必须记录理由和证据引用。

#### 修改位置

- `backend/app/analysis/orchestrator.py`：把 AI 阶段总状态传播到所有 L2；
- `backend/app/findings/evidence.py`：AI 失败改为关键 gap；
- `backend/app/findings/severity.py`：不再无条件采用 `severity_hint`；
- `backend/app/findings/aggregate.py`：聚合完整性、授权和数据流状态；
- `backend/app/findings/report.py`：报告顶部输出完整性摘要。

#### 验收标准

- 复核报告中的 13 个误报样本不再生成 high；
- 55/55 AI 失败时，所有未被确定性专项规则闭合的 L2 都是 pending；
- UI 明确区分“规则候选”“AI 已复核”“AI 失败”“人工确认”。

### P0-2 从文件级正则改为语法事实匹配

#### 问题

当前规则直接扫描完整文件正文，导致 import、注释、字符串和方法声明误命中。

#### 方案

在索引阶段生成结构化 `expressions`/`call_sites`：

```text
call_sites
- id
- method_id
- receiver_type
- receiver_text
- method_name
- arguments_json
- assigned_to
- start_line/end_line
- expression_kind
```

第一阶段无需立刻引入完整 Java/Kotlin 编译器，可先采用两级实现：

1. **保守语法清洗**：复用 `_strip_comments_and_strings`，排除 package/import、注释、字符串；
2. **方法体调用提取**：只在已解析方法行范围内提取 `methodName(...)` 调用表达式，区分方法声明和调用。

后续可接 tree-sitter-java/tree-sitter-kotlin 做 AST 精确解析；Smali 继续使用指令级解析。

#### Sink 收紧

- `execute` 只有 receiver 类型属于 OkHttp/Call/HttpClient 等网络类型才是网络 Sink；
- `delete` 只有调用对象属于 SQLiteDatabase/ContentResolver/File 或真实 Provider 方法体内存在副作用才算 Sink；
- `AccountManager` import 不算 Sink；必须存在实例方法调用；
- Activity 固定内部跳转只记录“固定目标组件调用”，不视为不可信 Intent 转发。

#### 验收标准

- import、注释、字符串和方法声明命中数为 0；
- `FeedBackDumpLogReceiver` 的线程池 `execute()` 不再被识别为网络；
- `SettingSearchProvider.delete()` 返回 0 不再生成 mutation high。

### P0-3 Provider mutation 专项语义规则

#### 方案

规则进入 `insert/update/delete/applyBatch/openFile/call` 的具体方法体后，识别真实副作用：

- SQLiteDatabase/Room/DAO 写调用；
- ContentResolver 写调用；
- `FileOutputStream`、`ParcelFileDescriptor.open` 写模式、`File.delete`；
- 持久状态更新或系统设置修改；
- 返回常量 0/null、直接抛 `UnsupportedOperationException`、空方法体，标记 `no_effect`。

候选必须记录：

```json
{
  "entry_method": "delete",
  "effect_kind": "file_delete | db_write | no_effect | unknown",
  "effect_evidence": [],
  "dataflow_status": "intraprocedural"
}
```

#### 验收标准

- SettingSearchProvider、DeviceProvider、StatusContentProvider 的空写方法不再报 mutation；
- WidgetControlFileProvider 的 `openFile/delete` 能形成真实文件副作用候选。

### P0-4 Binder/AIDL 专项分析器

#### 分析链

```text
exported Service
→ onBind 返回表达式
→ 返回类的继承关系
→ Binder / IInterface.Stub / onTransact
→ transaction code → 接口方法
→ 方法内调用者 Guard
→ 方法敏感副作用或返回敏感数据
```

#### 索引扩展

- 方法返回表达式与返回类型；
- `extends Binder`、`extends *.Stub`；
- `onTransact` switch code；
- Parcel read/write 类型；
- 接口方法到实现方法映射；
- Guard 调用摘要。

#### Guard 规则

Guard 不能仅用关键词判断，需确认它支配敏感操作或在方法入口失败关闭：

- `Binder.getCallingUid()` 与自身 UID/系统 UID 比较；
- UID → package → signature 校验；
- `checkCallingPermission/enforceCallingPermission`；
- 封装函数的摘要传播。

#### 验收标准

- `SportXmsService` 检出为高价值 Binder 候选，并列出 transaction 及敏感方法；
- `AirkanService` 普通本地 Binder 不再误报为远程 AIDL；
- 有 signature 权限或有效 UID/签名 Guard 的 Service 降级/关闭。

### P0-5 FileProvider 路径与 authority 专项规则

#### 检查项

- 重复 authority：建立 `authority → provider[]` 冲突表；
- `openFile` 支持的模式：r/w/rw/rwt/wa；
- 是否允许公开 `delete`；
- 是否要求 URI grant、read/write permission 或 signature 权限；
- canonical 边界实现：
  - 安全：`path == root || path.startsWith(root + separator)`；
  - 不安全：仅 `path.startsWith(root)`；
- path XML 暴露根及可访问目录类型；
- 写入文件是否进入后续可信消费链。

当前 `getCanonicalFile|startsWith` 即视为安全的逻辑必须删除，改为实现级判定。

#### 验收标准

- `WidgetControlFileProvider` 生成 conditional high/pending 候选；
- 明确输出“重复 authority 需动态解析”的关键阻塞条件；
- 标准安全 FileProvider 不误报路径穿越。

### P0-6 报告与状态语义修正

#### 状态重命名

- `ai_candidate` 改为 `auto_candidate` 或 `pending_review`；
- AI 状态独立展示：未执行/完成/失败；
- 人工复核状态独立展示：待复核/确认/误报。

#### 报告顶部强制字段

```text
扫描完整性：完整 / 不完整
JADX：success/partial，错误数 N
索引跳过文件：N
规则失败：N/18
AI：成功 N，失败 N，跳过原因
动态验证：未执行/通过/失败
```

如果存在 critical gap：

- 标题加“待确认”；
- 风险不得显示为已确认 high；
- POC 和实际影响保持未验证措辞。

## 4. P1：建立真正的数据流与平台语义

### P1-1 方法内 def-use/污点传播

对每个方法构建轻量 IR：

```text
parameter / field / return / constant
assignment
call argument
call return
branch guard
sink argument
```

Source 不是“调用出现”，而是其返回值或参数节点；Sink 必须证明受污染值进入敏感参数。

需要支持：

- 局部变量赋值；
- 类型转换；
- StringBuilder/String.format/字符串拼接；
- Bundle/Intent get/put；
- 字段写入与同类字段读取；
- 简单条件分支和 sanitization。

输出实际路径：

```text
Intent.getStringExtra → local url → dispatch(url) → WebView.loadUrl(url)
```

### P1-2 跨方法摘要传播

为方法生成 summary：

```json
{
  "parameter_to_return": [],
  "parameter_to_sink": [],
  "field_reads": [],
  "field_writes": [],
  "guards": [],
  "side_effects": []
}
```

使用 caller/callee 图迭代传播；遇到反射、接口多实现、Native 或歧义调用时生成 coverage gap，而不是推测唯一目标。

### P1-3 权限语义矩阵

统一计算组件的有效授权：

- application permission；
- component permission；
- Provider read/write permission；
- path-permission；
- 自定义 permission protectionLevel；
- 平台 permission；
- URI grant；
- exported/targetSdk/platform API；
- authority 冲突。

输出：

```json
{
  "authorization_status": "protected",
  "effective_permission": "com.example.SIGNATURE",
  "protection_level": "signature",
  "reason": "provider_read_permission"
}
```

### P1-4 封装 Guard 摘要

从敏感入口反向寻找 Guard：

- 方法入口直接 Guard；
- 调用封装校验函数；
- Guard 返回 false/抛异常后的控制流；
- Guard 是否只校验调用者提供的 packageName/fingerprint；
- Guard 是否支配 Sink。

Guard 状态细分为：

```text
present_effective
present_partial
present_bypassable
absent
unknown
```

### P1-5 started Service 状态机分析

针对导出 Service：

- 将 `onStartCommand(Intent)` extras/action 作为 Source；
- 识别 switch/if 分派常量；
- 追踪到传感器、定位、前台服务、运动状态等副作用；
- 记录 Android 后台启动限制作为前置条件，而不是直接关闭候选。

验收样本：两个 SportService 应检出；WearableXmsService 应生成“外部事件注入，具体设备影响待确认”。

### P1-6 动态 Receiver API 分支语义

必须按调用点分析，不按整个文件关键词：

- registerReceiver 重载签名；
- `RECEIVER_EXPORTED/NOT_EXPORTED` 实参；
- SDK_INT 分支；
- sender permission；
- LocalBroadcastManager/应用内事件总线；
- action 是否系统受保护；
- `onReceive` 的实际副作用。

输出按 API 范围拆分：

```text
API 33+：not exported
API 26–32：无 flag，需权限/副作用判断
```

## 5. P2：形成可复现的人工复核闭环

### P2-1 L1 高价值攻击面排序

L1 不再平铺 108 项，按后续语义信号排序：

1. 导出 Service 返回 AIDL Stub；
2. Provider `openFile/delete/call`；
3. started Service 控制定位/传感器/账户/支付；
4. Provider 返回账户、设备、健康数据；
5. WebView 外部 URL；
6. 普通 OAuth/支付回调或系统集成入口。

排序只决定复核优先级，不改变证据等级。

### P2-2 Evidence 最小闭包

每个 evidence 只保存：

- 当前组件 Manifest 节点；
- 关联 permission 定义；
- 相关 authority 冲突组件；
- 最终切片；
- 模型轨迹摘要；
- 覆盖缺口。

不再复制全部 Manifest 组件。

### P2-3 AI 可复现信息

每次调用保存：

```json
{
  "provider_kind": "openai-compatible",
  "base_url_hash": "...",
  "model": "...",
  "analyzer_version": "...",
  "prompt_template_version": "...",
  "prompt_hash": "...",
  "input_slice_hash": "...",
  "response_hash": "...",
  "http_status": 200,
  "latency_ms": 0
}
```

不保存 API Key，不在日志输出 Authorization。

### P2-4 复核状态同步

人工复核事务完成后同步更新：

- SQLite 当前状态；
- review_history；
- `findings/<id>.json`；
- `reports/evidence/<id>.json`；
- 已生成 Markdown 报告标记为 stale 或重新渲染。

### P2-5 审查样本回归集

将本次审查转化为版本化测试 fixture：

#### 应关闭误报

- SettingSearchProvider 空 mutation；
- DeviceProvider 抛异常 mutation；
- DumpLogProvider 权限/系统校验；
- MenstruationContentProvider signature read permission；
- StatusContentProvider 强 read/write permission；
- AlarmConsoleService signatureOrSystem；
- PushMessageHandler MIPUSH permission；
- AirkanService 本地 Binder；
- FeedBackDumpLogReceiver 线程池 execute；
- Activity import Intent 假 Source。

#### 应检出/排序靠前

- SportXmsService AIDL Binder；
- WidgetControlFileProvider；
- 两个 SportService；
- WearableXmsService；
- DeviceProvider query 泄露；
- IssuerActivity URL 校验可疑链；
- 显式 exported/mixed 动态 Receiver。

## 6. 推荐实施顺序

### 阶段 A：可信度止血

1. 完整性状态传播；
2. AI 失败使未闭合 L2 变 pending；
3. 状态“AI候选”重命名；
4. 报告顶部完整性摘要；
5. 方法声明/import/字符串排除；
6. Provider 空 mutation 识别。

完成后先重新扫描人工样本，目标是 13 个错误 high 全部消失。

### 阶段 B：高价值专项规则

1. Binder/AIDL；
2. FileProvider/authority；
3. started Service 状态机；
4. Provider query 数据分类；
5. 动态 Receiver 调用点语义。

完成后目标是 V-01 至 V-05 被系统自动识别或正确提升为优先复核候选。

### 阶段 C：通用数据流

1. 方法内 def-use；
2. 方法 summary；
3. caller/callee 传播；
4. Guard 支配关系；
5. AI 基于确定性路径做语义补全。

### 阶段 D：运营与复现

1. Evidence 最小闭包；
2. prompt/model/hash；
3. 人工复核同步；
4. 动态验证任务清单；
5. 回归数据持续扩展。

## 7. 总体验收标准

### 误报

- 人工报告列出的 13 个 high，按原描述全部不再输出 high；
- import、声明、字符串导致的 Source/Sink 命中为 0；
- 空 Provider mutation 不再产生漏洞候选；
- LocalBroadcastManager 不再被视为跨应用广播。

### 漏报

- V-01 自动识别 Binder Stub 与 transaction；
- V-02 自动标记重复 authority 和 canonical 边界缺陷；
- V-03 自动形成 Intent extras → 运动采集状态副作用链；
- V-04 输出外部事件注入并保留设备控制阻塞条件；
- V-05 识别 query 数据泄露且不误报 mutation。

### 可解释性

- 每个 L2/L3 都有方法级 Source、传播、Sink 和 Guard 证据；
- 每个路径节点有文件、行号和 context_id；
- 不完整分析不会显示为“已确认高危”；
- 报告能区分规则结果、AI 结果、人工结果和动态验证结果。

## 8. 关键决策建议

1. **先做可信度闸门和专项规则，再做通用污点分析。** 这能最快消除错误 high，同时覆盖人工确认的最严重问题。
2. **不要让 AI 替代数据流引擎。** AI 应解释确定性链路、识别封装 Guard 和提出扩片请求；Source→Sink 基础传播应由代码实现。
3. **保留 L1，但改为攻击面队列。** 本次最严重问题正是从 L1 继续追踪发现，简单压制 L1 会扩大漏报。
4. **等级必须晚绑定。** 在入口、授权、传播、影响未闭合前保持 pending；规则 `severity_hint` 只能作为建议，不能直接成为最终等级。
