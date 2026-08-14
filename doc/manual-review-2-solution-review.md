# 第二轮人工审查质量问题方案评审

## 结论

我认同 `manual-vulnerability-analysis2.md` 第 7 节提出的 9 个问题，但不建议按问题逐项增加正则或简单延长超时。更稳健的方案是建立四层边界：

1. **不可变事实层**：FQCN、方法描述符、调用点顺序、类型、权限、Manifest 入口；
2. **专项语义层**：Binder、Router/Intent、Fragment 反射、Provider、started Service、Receiver；
3. **可信度门禁层**：完整性、授权、数据流、Guard、影响和动态状态独立判定；
4. **AI 复核层**：只解释事实、请求扩片和指出 gap，不能生成事实或覆盖确定性字段。

## 九项问题逐条判断

### 1. AI 14/14 HTTP 403

**认同，当前仅完成失败传播，未解决接入可靠性。**

当前系统会把失败写入 coverage gap 并阻止 L3，但仍对每个 L2 分别发送请求，导致同一鉴权错误重复 14 次。

更优方案：

- 扫描进入 AI 阶段前执行一次轻量 preflight；
- 区分 `auth_failed`、`model_not_found`、`request_incompatible`、`rate_limited`、`transient_failure`；
- 401/403、模型不存在、请求格式不兼容触发 task 级 circuit breaker，后续候选直接标记 skipped；
- 429/5xx 可有限重试，记录退避和最终原因；
- 关闭 AI 或 AI 全失败时，确定性字段必须与开启 AI 前完全一致。

### 2. Binder 规则超时

**完全认同；不应通过提高 120 秒限制解决。**

当前规则在约 49,000 文件、49 万方法和 171 万调用点上重复执行组件查询、逐方法 call_sites 查询和继承扩展，形成 N+1 与前导通配符查询。

更优方案：

- Manifest 先筛选 `exported=true` 且授权未确认强保护的 Service；
- 索引新增 `service_entry`、`binder_type`、`class_hierarchy`、`aidl_transactions` 物化事实；
- 一次 JOIN 批量加载目标 Service 的 onBind、返回类型、Stub、transaction 和 Guard；
- 按 `组件 × 入口` 运行并保存 checkpoint；单组件超时只产生组件级 gap；
- 输出最慢组件与 SQL/分析阶段耗时。

### 3. Guard 判断错误

**完全认同；现有实现仍属于保守基线。**

当前已解析部分 Manifest 权限并区分 canonical 安全/不安全边界，但 Guard fail-closed 仍主要依赖方法文本，且 AI 结果仍可覆盖 `guard_status`。

更优方案：

- 建立 Effective Authorization Matrix：application、component、read/write/path permission、URI grant、protectionLevel、targetSdk、authority 冲突；
- 引入平台权限目录，未知权限保持 `unknown`，不能视为 signature；
- Guard 生成确定性摘要：输入身份、比较对象、失败边、保护范围；
- 只有 Guard 的失败分支 return/throw 且支配 Sink 才是 `present_effective`；
- canonical 判断作为 path guard 子类型，支持 `present_bypassable`；
- AI 只能提出 Guard 复核建议，不能直接写最终 Guard 状态。

### 4. 影响建模不足

**完全认同；当前副作用字典覆盖有限。**

当前已识别定位、传感器、运动和前台服务等基础能力，但 ProxyData、设备协议、状态回调和连接状态仍缺乏语义。

更优方案：

- 建立 operation taxonomy，而不是只按方法名：
  - data disclosure；
  - persistent state write；
  - device protocol output；
  - callback/event injection；
  - location/sensor collection；
  - connection/session control；
  - UI/navigation；
- 方法 summary 输出 side_effect、written_fields、callbacks、device_protocol、preconditions；
- 风险等级只绑定实际 operation 和前置条件，不绑定规则 ID。

### 5. 校验后覆盖未建模

**完全认同，这是第二轮反馈中最值得新增的通用能力。**

当前污点传播没有值版本和 Bundle/Intent key 级状态，无法发现“先校验合法 URL，后续 putExtras 覆盖 URL”。

更优方案：Validation-state dataflow：

```text
value/key state = untrusted → validated → overwritten/untrusted
```

需要：

- 对 Intent/Bundle 维护 key-slot，例如 `Intent[URL]`；
- 每次 putExtra/putExtras/replaceExtras/fillIn 形成新版本；
- 全量 `putExtras` 对同名 key 产生 may-overwrite；
- Sink 使用最终 reaching definition，而不是曾经验证过的旧值；
- 校验事实绑定具体 value version，写覆盖后自动失效；
- 增加专项模式：validated target → bulk copy attacker extras → Sink。

### 6. 同名类映射错误

**完全认同；当前仍可能回退到简单类名。**

当前索引有 FQCN，但 ContextBuilder 和规则查询仍存在简单类名回退；DataFlowAnalyzer 也按方法名聚合目标。

更优方案：

- 类唯一键固定为 `FQCN`；
- 方法唯一键固定为 `FQCN#name(descriptor)`；
- 调用边使用 resolved target ID，不只存方法名；
- 只有“同包且唯一”时允许简单名回退；
- 同名或重载无法唯一解析时输出 `SYMBOL_TARGET_AMBIGUOUS`，不得任选第一个；
- evidence 必须保存 resolved symbol ID 和实际 path。

### 7. Sink 分类错误

**完全认同；通用 `delete=database` 不可靠。**

当前 Provider 专项已能识别部分 file delete，但通用数据流表仍把 `delete` 固定归为数据库操作。

更优方案：receiver/type-aware operation classification：

- `File.delete` → file delete；
- `SQLiteDatabase.delete` / DAO delete → database mutation；
- `ContentResolver.delete` → external content mutation；
- Provider 的 `delete(...)` 是入口，不是 Sink；Sink 必须位于方法体内部；
- receiver 类型未知时标记 `operation=unknown_delete`，不能直接定级。

### 8. L1 深挖不足

**认同，但不建议对全部 L1 无差别深挖。**

141 个攻击面中只有少数高价值入口需要专项分析。全量深挖会再次造成超时和噪声。

更优方案：Risk-driven work queue：

1. 导出 Service + onBind/Stub；
2. 导出 Activity + nested Intent/全量 extras/外部 className；
3. Provider + openFile/delete/call/query；
4. 自定义 action Receiver + 设备/状态/文件/回调副作用；
5. started Service + extras/action + 状态写入；
6. 普通 OAuth/支付回调等保留 L1，不自动展开全部调用图。

每个专项任务按组件保存结果和 coverage gap，不依赖 AI 才能形成候选。

### 9. evidence_validation 语义易误解

**完全认同；虽然 L3 门禁已收紧，阶段命名和统计仍不准确。**

`verified: 273` 目前只表示候选完成结构/位置回查，不代表 273 个漏洞成立。

更优方案：

- 阶段重命名为 `evidence_integrity_validation`；
- 统计拆分：
  - candidates_checked；
  - locations_verified；
  - sources_verified；
  - sinks_verified；
  - deterministic_chains_closed；
  - gradeable_findings；
  - findings_pending_review；
- UI 不再显示笼统“verified”；
- `fact_integrity`、`semantic_status`、`exploitability_status` 三个维度分离。

## 对报告建议的两点修正

### 不建议“关键规则失败时整个 run 完全不可定级”

应采用**按覆盖域定级**：

- run 顶层仍为 `analysis_incomplete=true`；
- Binder 规则失败时，Binder 覆盖域 `gradeable=false`；
- 已闭合且不依赖 Binder 的 Provider/Activity Finding 可以保留自身等级；
- 总览不得宣称“扫描未发现其他漏洞”。

这样既不会掩盖缺口，也不会让一个规则失败抹掉其他确定性结论。

### 不建议一步切换到完整编译器级 AST/CFG

推荐分两步：

1. tree-sitter Java/Kotlin + Smali 指令解析，生成统一轻量 IR、基本块、def-use 和调用点；
2. 仅对高价值候选构建局部 CFG、值版本和跨方法 summary。

这样比继续堆正则可靠，也比直接建设全 APK 编译器级分析器更符合个人版的维护成本。

## 推荐实施顺序

### P0：运行可靠性与事实唯一性

1. AI preflight + circuit breaker；
2. Binder 规则 SQL/批量查询/组件 checkpoint；
3. FQCN + 方法描述符唯一解析；
4. receiver/type-aware Sink 分类；
5. evidence_validation 改名和统计拆分。

验收：

- 相同 403 只发起一次 preflight，不再重复 14 次；
- Binder 规则在当前 APK 上不触发 120 秒超时；
- 两个同名 SportService 的 evidence 路径各自正确；
- File.delete、DB delete、Provider entry delete 分类测试全部通过；
- UI 不再出现“273 verified=273 漏洞”的歧义。

### P1：确定性语义链

1. validation-state dataflow，覆盖 putExtras 覆盖；
2. Effective Authorization Matrix；
3. Guard fail-closed 与局部支配；
4. side-effect taxonomy 和方法 summary；
5. Router/Fragment/started Service/Receiver 专项分析器。

验收使用版本化 fixture，不设置正式 Precision/Recall/F1 门禁：

- RouterActivity 校验后覆盖自动检出；
- 两个 CommonBaseActivity 外部 className→Fragment 实例化自动检出；
- SportXmsService、SportService、WearableXmsService、DeviceProvider、PhoneStateReceiver 等已确认链路能检出或进入高优先级待确认；
- CarIconProvider、DumpLogProvider、AirkanService 等既有误报保持关闭。

### P2：质量运营与 AI 受限复核

1. 高价值 L1 工作队列和组件级恢复；
2. prompt/model/输入输出哈希与错误分类；
3. 确定性结果在 AI 开关前后一致性测试；
4. 人工结论转为脱敏 fixture；
5. 按规则和覆盖域输出耗时、gap、候选和关闭原因。

## 建议确认的范围

建议下一轮先实施 **P0 五项**，再实施 P1。原因是：当前最严重的漏报来自 Binder 超时，最明显的 evidence 错误来自符号映射、Sink 分类和统计语义；先修事实层和运行可靠性，后续数据流与专项分析才不会建立在错误索引上。
