# P1 任务实施报告：sink taxonomy 扩充（base 层同步 + write 冲突修复 + 同步校验脚本）

> **任务来源**：`docs/analysis/rules-review/2026-08-27-ruleset-quality-review.md` 第五节优先级 #1
> **实施日期**：2026-08-27
> **实施者**：主 agent（GLM-5.3）

## 1. 变更清单

### 1.1 `rules/sink_taxonomy/versions.yaml`（base 55→73 条，taxonomy_version 1.0.4→1.0.5）

**write 双源冲突修复（评审 R-7 验收用例）**：原 write 条目把 `BluetoothOutputStream/UsbOutputStream/NfcOutputStream/ProtocolWriter`（设备协议流）与文件流混在 `file_mutation`——拆为 `file_mutation`（BufferedWriter/FileOutputStream/FileWriter/RandomAccessFile + java.nio.file.）与 `device_protocol_output`（四设备流 leaves）两条，与 `dataflow.py:3007-3012` 对齐。

**新增 18 条 base 条目**（均与 `dataflow.py::classify_operation_taxonomy` 逐条探针核对一致）：

| method | taxonomy | receiver 证据 | dataflow 锚点 |
|---|---|---|---|
| newInstance | ui_navigation | leaves [Constructor] | dataflow.py:2840-2846 |
| getCurrentLocation | location_sensor_collection | leaves [LocationManager] + prefixes [gms] | dataflow.py:2913-2916 |
| startScan / stopScan | connection_session_control | leaves [BluetoothGatt 等 6] | dataflow.py:2968-2971 |
| clear | persistent_state_write | leaves [Editor] + exact | dataflow.py:3043 |
| putInt / putLong / putBoolean | persistent_state_write | leaves [Editor] + exact [$Editor/$Global/$Secure] | dataflow.py:3041-3042, 3057-3060 |
| compileStatement | database_mutation | prefixes [sqlite] | dataflow.py:3090 |
| applyBatch | database_mutation | leaves [ContentResolver/SQLiteDatabase] + prefixes | dataflow.py:3071, 3089 |
| send | data_disclosure | prefixes [okhttp3./retrofit2./org.apache.http.] | dataflow.py:3162-3165 |
| write（okhttp 族） | data_disclosure | prefixes [okhttp3./retrofit2./org.apache.http.] | dataflow.py:3164 |
| FileInputStream / RandomAccessFile / MatrixCursor（构造器） | data_disclosure | leaves 同名 | dataflow.py:3113-3121 |
| truncate | file_mutation | leaves [File/FileChannel/RandomAccessFile] + prefixes | dataflow.py:3138 |
| open | file_mutation | leaves [File/FileChannel/ParcelFileDescriptor] + prefixes | dataflow.py:3100-3112, 3140 |

其中评审报告"11 项遗漏清单"全部落位；`startScan/stopScan/applyBatch`（3 条）与 okhttp `write`（1 条）为校验脚本 COVERAGE 提示后补录的平台级条目；构造器条目的 `expression_kind=constructor` 限定仅存在于 dataflow 侧（versions.yaml 消费端为宽松匹配，语义兼容）。

**范围落档（核验 R-1）**：任务源"含 E2 补录（getLastKnownLocation）"**移交优先级 #2 执行**——E2 需先修 dataflow 侧 family（`dataflow.py:2913` 补 `getLastKnownLocation` 条目）再同步 versions.yaml，属代码修复而非纯数据同步；在 P1 单独补 versions.yaml 侧会造成 ORPHAN（dataflow 无分支）。本节仅完成 versions.yaml ↔ dataflow 现状的一致化。

**既有主动偏离披露（核验 R-8）**：SP 族条目（含本次新增 clear/putInt/putLong/putBoolean）的 `leaves [Editor]` 为消费端宽于 dataflow 的既有主动偏离（dataflow 的 SP family 为 exact-only，`dataflow.py:3036-3038`；宽松口径由 backend 消费端 `app/analysis/sink_taxonomy.py:43-47` 声明），本次新条目沿用，脚本以 info 提示；后续统一时随 §3.2-2 一并处理。

### 1.2 `scripts/check_sink_taxonomy_sync.py`（新建）

双源同步校验脚本，三级检测：

- **CONFLICT**（exit 1）：base 条目的 receiver 证据探针（method_descriptor 留空 → `OPERATION_SIGNATURE_GAP` 路径绕过 arity 校验，只验 family×method 映射）在 dataflow 命中但 taxonomy 不一致；
- **ORPHAN**（warning）：条目全部证据在 dataflow 无分支（`--strict` 时 exit 2）；
- **COVERAGE**（info）：正则粗扫 dataflow 签名分支 method 名，输出不在 versions.yaml 的候选清单。

探针细节：leaves 用裸简单名、prefixes 合成 FQCN、exact 含 `$`→`.` 变体（两源内部类分隔符不统一，见 §3.2）；constructor 条目双形态（call/constructor）探针；manual 条目（per-APK 自定义）跳过。

## 2. 验证结果

- 同步校验：`base 73 条：PASS 73，CONFLICT 0，ORPHAN 0；manual 4 条跳过`——write 冲突清零；
- 全量测试：**1270 passed / 0 failed**（提交基线 1255 + 后续任务新增测试，无回归）。验证命令：`backend/.venv/bin/python -m pytest backend/tests/ --tb=no`（2026-08-27，38.57s）；核验后新增 CI 接入测试 `test_versions_yaml_synced_with_dataflow`（backend/tests/test_sink_taxonomy.py）。

## 3. 44 个未命中 sink 人工评审（run eada0e71）

数据源：`.ai-apk-tracer/runs/20260826T141857Z_1c55d3fb9f95_eada0e71/explorer/candidates.json` 的 44 个"sink 未命中 taxonomy 封顶 partial"候选。

### 3.1 评审结论汇总

数据源：`.ai-apk-tracer/runs/20260826T141857Z_1c55d3fb9f95_eada0e71/explorer/candidates.json` 的 44 个"sink 未命中 taxonomy 封顶 partial"候选（核验 R-2 修订：以 notes 含"sink 未命中 taxonomy"为准；下表另含 4 个顺带评审的非成员候选，已标注）。

| 类别 | 数量 | 明细 |
|---|---|---|
| 已入册（76ac2c4 manual） | 4 | getPrefEncryptedUserId、getAccountId（隐私读取）、setStringPref、setIntPref（偏好写入） |
| **待定**（本轮记录，未入册） | 1 | XmAdUtil.saveCallback——见 §3.2 缺陷 1（非 44 成员，notes 为回查失败类，顺带评审） |
| 不入册：方法粒度非 sink（入口/回调/UI 操作） | 23 | finish()×2、Log.e、setResultData、LoginManager.login、showLicense、initData×3、handleIntent×2、initFragment、initRn×2、onBackPressed×3、showControlsIfCan、updateFinishButton、genBarcodeBitmap、handleLocalImage、takeFinish、attachYrnModule、ImageSelector.onResult（核验 R-2 补审：外部 Intent 结果回调处理，入口形态非 sink） |
| 不入册：工厂方法无数据面 | 6 | PushClient.getInstance、JrManager.getInstance、MiotStoreApi.getInstance、PluginGc.getInstance、WBH5FaceVerifySDK.getInstance、mmkvFromAshmemID |
| 不入册：混淆名 Binder/OAuth 内部处理 | 4 | m16382a（OAuth 回调）、C6007f.m16147c、C1135f.m2144a、C1298la.m3585a（Binder 构造/管理——非数据面） |
| 不入册：弱敏感/遥测/方向另立 | 5 | StatService2.onError（错误上报，入参为内部异常）、syncPluginById×3（动态代码加载——真 sink 为内部 DexClassLoader，属 P5 新规则方向）、SkyTreeUtils.upgradeAfterPermissionRequest |
| 不入册：无代码上下文（非 44 成员，顺带评审） | 2 | Unverified、Unspecified（notes 为"跳均不可回查"） |
| 不入册：入口非 sink | 1 | ReactNativeFragment.onActivityResult（桥转发缺方法粒度） |

合计 44 成员 = 4 已入册 + 23 + 6 + 4 + 5 + 1 + 1（ImageSelector.onResult 计入非 sink 类）；另有 Video playback start（非 44 成员，notes 为回查失败，顺带评审归"方法粒度非 sink"）与 saveCallback/Unverified/Unspecified 共 4 个非成员候选单独标注。

**对验收报告"PushClient.getInstance 为合理扩展候选"的异议**：getInstance 是静态工厂（`decompile/sources/com/xiaomi/mishop/pushapi/PushClient.java:11-13`），无数据读写面；push token 的真实数据面在 `PushClientImpl.register`（token 写入 Bundle 后经 sendCommand 发往 push 服务，`decompile/sources/com/xiaomi/mishop/pushapi/impl/PushClientImpl.java:16-40`，核验 R-3 修订——原稿"registerReport"为不存在的方法名）；候选自评亦为 "no sensitive sink identified"（candidates.json:738）。入册 getInstance 将引入过宽匹配。此判断经核验代理确认成立。

### 3.2 过程发现（缺陷记录）

1. **promote_custom_sink.py 用法 A 锚点提取缺陷**：`_sink_anchor_from_run` 取 `last_hop.to_method_id` 提取方法名——对链尾自环候选（saveCallback 候选的 hop 是 `SplashPresenter.loading:104→loading:104` 自环）提取出 `method: loading` 且**无任何 receiver 约束**（run 索引按 from_method_id+call_site_line 反查 receiver 未命中时不报错、静默生成无约束条目）。该条目已撤销；saveCallback 入册降级为待定（候选链自环质量亦存疑）。**建议修复**：锚点提取失败（无 receiver 或方法名来自自环）时应报错退出而非静默落无约束条目。
2. **两源内部类分隔符不统一**：versions.yaml 的 exact 用 `$`（`android.provider.Settings$Secure`），dataflow 的 exact 用 `.`（`android.provider.Settings.Secure`，dataflow.py:3054）；`_normalize_operation_receiver_type`（dataflow.py:2508-2517）不做 `$`↔`.` 转换。当前靠 dataflow 侧 leaves {Secure, Global} 兜底 + 校验脚本双形态探针豁免。**建议**：统一为一种形态（需评估 backend 消费端 `normalize_receiver_type` 的口径，超出 P1 范围）。
3. **COVERAGE 遗留 17 项**（脚本 info 输出，核验 R-7 修订计数）：弱敏感 7 项（add/replace/show/emit/dispatch/onChanged——FragmentTransaction UI 导航与 LiveData/Flow 回调，暂缓；put——Editor family 的 persistent_state_write（dataflow.py:3041）但裸名过泛且 Map.put 明确 not_sensitive（dataflow.py:3169-3172），暂缓）+ sport/sensor 自研 9 项（startSport/pauseSport/resumeSport/finishSport + startGymSensor/startStepSensor/startAccSensor/restartAccSensor/pauseOrStopSensor——属 E7 评审的"迁出 shared 层"对象，P4 处理）+ toString 噪声 1 项（正则误扫，string_transforms 豁免集合非 sink）。
4. **PFD.open 参数敏感分歧在案（核验 R-5）**：dataflow 对 ParcelFileDescriptor.open 有只读降级分支（实参含 mode_read_only/'r' 且无写模式 → data_disclosure，`dataflow.py:3105-3111`），versions.yaml 的 open 条目固定 file_mutation——消费端仅做命中判定、taxonomy 值不外泄（`backend/app/analysis/explorer_validation.py:140`），影响有限；脚本探针（arguments=[]）不可探测该分支，已在脚本 docstring 声明为结构性边界，以 file_mutation 为准记录在案。

## 4. 待核验点

1. 新增 18 条 base 条目的 taxonomy 与 receiver 证据是否与 dataflow 分支严格一致（脚本已验，请独立复核）；
2. write 拆分后两轨一致性（R-7 验收用例）；
3. 44 sink 评审表的判定是否公允（特别是 PushClient.getInstance 异议与 saveCallback 待定）；
4. §3.2 缺陷 1 的 promote 锚点修复建议是否成立；
5. 校验脚本的探针设计（OPERATION_SIGNATURE_GAP 绕过 arity）是否引入假阴性。
