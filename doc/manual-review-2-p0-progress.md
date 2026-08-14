# 第二轮人工审查 P0 实施进度

## 状态

第二轮 P0 五项已完成代码实现、自动化回归和真实大型 APK Binder 性能验证。

## 1. AI preflight 与任务级熔断

- 在发送任何代码切片前，先发送不含代码的最小 JSON 请求；
- 分类鉴权失败、模型不存在、请求不兼容、限流和瞬时故障；
- 401/403、模型和请求格式问题立即打开任务级熔断；
- 429/5xx/网络错误最多重试一次；
- 同一 403 不再对每个 L2 重复请求；
- 没有 L2 候选时不执行 preflight；
- AI 结果只写 `ai_guard_assessment`，不能覆盖确定性 `guard_status`、dataflow 或 authorization。

## 2. Binder 规则性能与覆盖域

- Manifest 先筛选导出且未确认强保护的 Service；
- 使用精确 FQCN 查询，不使用前导通配符；
- 一次 JOIN 批量加载目标文件的方法和调用点；
- 仅扩展 onBind 返回类型的有限四层继承闭包，不扫描整个包；
- 每个 Service 输出 duration/status/gaps；
- 单组件失败形成 component coverage gap，不拖垮其他组件；
- 规则/组件 gap 只阻断对应覆盖域，不自动抹掉其他规则的确定性结论。

## 3. 符号唯一解析

索引 schema 升级为 `2.4.0`：

- 方法标识：`FQCN#name(descriptor)`；
- call site 保存 receiver type、method descriptor、resolved target 和 resolve status；
- 仅同类或同包且描述符唯一时建立调用边；
- 同名、重载或目标不唯一时不任选第一个；
- ContextBuilder 使用 resolved target ID；
- 组件源码优先按 FQCN/精确路径查询，简单名只在全局唯一时回退。

## 4. 类型感知 Sink

- `File.delete` → `file_delete`；
- SQLite/SupportSQLiteDatabase/DAO delete → `database_mutation`；
- ContentResolver delete → `content_mutation`；
- Provider 的 delete 方法声明仅是入口，不是 Sink；
- receiver 类型未知 → `unknown_delete` + critical gap，禁止确定性闭合；
- Provider mutation 同步使用 receiver 类型，不再仅凭方法名分类。

## 5. 证据完整性统计

阶段名称由 `evidence_validation` 改为：

```text
evidence_integrity_validation
```

统计拆分为：

- candidates_checked；
- locations_total / locations_verified；
- sources_total / sources_verified；
- sinks_total / sinks_verified；
- deterministic_chains_closed；
- gradeable_candidates；
- gradeable_findings；
- findings_pending_review。

候选同时记录：

- fact_integrity_status；
- semantic_status；
- exploitability_status。

前端时间线与报告显示拆分统计，不再使用容易误解的“273 verified”。

## 自动化验证

```text
后端测试：59 passed
规则契约：18 passed
TypeScript：通过
Vite 生产构建：通过
生产构建耗时：1.52s
```

新增回归覆盖：

- 403 preflight 只请求一次；
- 429/5xx/网络错误仅重试一次；
- 无 L2 时不调用 AI；
- AI 不能覆盖确定性 Guard/dataflow/authorization；
- 同名 Service 不串源码路径；
- 重载歧义不任选目标；
- File/DB/ContentResolver/Provider entry/unknown delete 分类；
- Binder 批量查询不加载同包噪声文件；
- 规则失败只阻断对应覆盖域；
- 证据完整性统计字段完整。

## 真实大型 APK 验证

使用 run `20260731T064616Z_2a80fc5a8735_24627318` 的 49,091 个反编译文件重新构建 v2.4 索引并单独执行 Binder 规则：

```text
索引版本：2.4.0
索引构建：184.47 秒
索引大小：1810.65 MiB
类：81,268
方法：489,166
调用点：1,713,637
Binder 规则：5.17 秒
Binder 状态：completed
组件诊断：29 个，0 个超时/失败
候选：4 个
```

自动检出的 Binder 组件包括：

- `com.xiaomi.fitness.sport_xms.SportXmsService`；
- `com.xiaomi.fitness.nfc.service.NfcBYDOpenService`；
- `com.xiaomi.xms.wearable.WearableXmsService`；
- `com.miui.tsmclient.sesdk.CardOpenService`。

原规则在 120.025 秒后超时；改造后 5.17 秒完成，最严重的 SportXmsService 已进入候选。

代价是结构化调用点使索引增长到约 1.81 GiB、构建约 184 秒。该成本不影响 Binder P0 验收，但后续应在 P2 通过调用点列存储/压缩、增量索引或只物化高价值调用进一步优化。

## 尚未纳入本轮 P0

- RouterActivity 校验后覆盖和值版本化；
- Fragment 外部类名反射专项；
- 完整权限矩阵与 Guard 局部支配；
- ProxyData、设备协议和复杂状态机影响建模；
- 通用跨方法固定点污点传播。

以上属于已确认的 P1。
