# JADX 退出码 3 诊断

## 结论

本次不是 APK 上传失败，也不是找不到 JADX。JADX 1.5.6 已成功读取并处理 APK，但反编译过程中累计出现 389 个错误，最终返回退出码 3。

复现结果：

```text
JADX version: 1.5.6
APK size: 143 MiB
processing total: 44,759
progress: 44,758 / 44,759 (99%)
ERROR - finished with errors, count: 389
return code: 3
```

## 为什么日志显示空原因

JADX 把 `finished with errors, count: 389` 输出到了 stdout，而当前适配器在退出码不被接受时只读取 stderr。此次 stderr 为空，所以最终日志显示：

```text
jadx 失败（退出码 3）:
```

## 当前系统为什么判定整个任务失败

当前反编译适配器只把退出码 0 和 1 当作可继续处理，退出码 3 会抛出 `JADX_FAILED`。实际上任务目录中已经生成 `resources/` 和 `sources/`，说明这是“反编译部分成功并伴随错误”，不是完全没有产物。

## 常见诱因

- APK 很大、DEX 和类数量很多；
- 混淆或加固；
- 异常/复杂控制流；
- JADX 不完全支持的字节码结构；
- 个别类损坏或无法还原。

仅凭错误计数无法确定 389 个错误分别对应哪些类，需要保留 JADX stdout 或使用更详细日志继续定位。
