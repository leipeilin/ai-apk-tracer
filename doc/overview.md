# AI-APK-Tracer P1 语义分析概览

## 已完成

- 索引升级到 `2.7.0`：ordered flow IR、调用 ordinal、FQCN+descriptor、resolved target、压缩 IR/参数和 external-content FTS；
- 方法内 value version、strong update/kill、Intent/Bundle key-slot 和 validation-state；
- 跨方法参数、返回值、跨文件对象 mutation 与有限 summary 固定点；
- Router 校验后 extras 覆盖检测和 Fragment 外部类名反射专项；
- Effective Authorization Matrix：组件/Provider/path permission、URI grant、protectionLevel、authority、operation/mode；
- Sink/入口级 GuardCoverage：fail-closed、顺序、catch bypass、唯一 wrapper 与身份来源；
- started Service 事件到跨方法副作用状态机；
- dynamic Receiver registration/action/onReceive/effect 唯一绑定；
- 九类 operation taxonomy；
- 明确授权确认 UI，以及授权矩阵和 Guard 状态展示；
- Manifest-only 规则不再无意义读取组件源码；
- Finding ID 使用 run 作用域，支持同一 APK 多次复测而不发生 SQLite 主键冲突。

## 质量验证

```text
后端测试：119 passed
规则契约：18 passed
TypeScript：通过
Vite 生产构建：通过（1.55 秒）
```

专项覆盖 Router、Fragment、Service、Receiver、validation-state、跨方法传播、权限矩阵、Guard 和 operation taxonomy。

## 真实 APK 结果

最终 run `20260731T140731Z_2a80fc5a8735_24905522` 对 143 MiB APK 完成全链路扫描：

- 总耗时约 6 分 53 秒；
- 49,091 个索引文件、489,166 个方法、1,713,638 个调用点；
- v2.7 索引 1,597,759,488 bytes（约 1523.7 MiB），构建 206.288 秒；
- 相对 v2.6 约 2008 MiB，体积下降约 24%；
- 18 条规则全部完成，无规则级失败；4 个混淆 Binder 返回类型保留组件级 gap；
- 593 个候选，聚合为 293 个 Finding：L1 268、L2 25；
- 16 条确定性链闭合；
- SportXmsService、两个 SportService、WearableXmsService、RouterActivity、WidgetControlFileProvider 和 DeviceProvider 均进入 L2 或攻击面；
- AI 按配置关闭，25 个 L2 保持待确认；
- JADX 仍有 389 个反编译错误，索引跳过 13 个超限文件，因此扫描明确标记不完整。

真实样本仍未自动闭合 RouterActivity 校验后覆盖、两个 CommonBaseActivity Fragment 反射和部分混淆 Service/Receiver 链；它们保留 L1/L2 或 coverage gap，未被错误提升为已确认漏洞。

## 保守边界

- 不提供完整编译器级 AST/CFG、复杂异常边、协程或 Smali SSA；
- 接口多实现、动态代理、复杂反射和 Native/JNI 生成 coverage gap；
- 未收录平台权限或受保护广播保持 unknown；
- AI 关闭/失败不会改变确定性数据流、授权、Guard 或影响事实；
- critical gap 只能生成待确认结论，不能晋级 L3。

详细实施记录见 `p1-semantic-analysis-progress.md`。
