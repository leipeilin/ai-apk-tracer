# 探索轨产出偏差分析（shop run eada0e71，2026-08-26）

> **问题**：探索 partial 4→46 大幅提升，但 validated=0 且 golden hit_rate 0/6——产出与预期的偏差根因分析。
> **数据源**：run eada0e71 的 explorer/{candidates,observations}.json + golden v3 标注 + sink taxonomy。

## 根因链（按层递进）

### 根因 1【评估口径】：golden 分母跨 APK 错位——0/6 实为 0/1

6 个 hit case 的 APK 归属（tags 实证）：**5 个是 health APK 的目标**（sport-binder=v-01 health、provider-query=v-03 health、router-validation=v-02 health、fragment-external=P-08 health、remote-aidl=合成语义），**仅 extra-close-url（v-02-shop）是 shop 的**。这些组件（SportApiStub/RouterActivity/DeviceProvider/SportXmsService）**不存在于 shop 的 190 组件清单**（实测 0 匹配）——shop run 上永不命中。

**T4.1 标注集未按 APK 分域**——单 APK run 评估时跨 APK case 稀释分母，0/6 的真实口径是 0/1。

### 根因 2【validated=0 直接原因】：sink taxonomy 封顶——跳回查已 100% 通过

46 partial 的验证 notes 分布：**44 个是"跳回查通过（1/1、2/2、3/3 全过）但 sink 未命中 taxonomy，封顶 partial（custom sink 待人工确认）"**——SEED-HOPS 的跳回查修复**完全生效**（44 候选跳回查 100% 通过，4 个真回查失败仅占 8%）。

瓶颈已从"跳回查失败"（旧 run 的 line_mismatch）**转移到 sink 分类学覆盖**（55 条 taxonomy 不含探索发现的新 sink）。

### 根因 3【sink 质量】：未命中 sink 中相当部分本就不敏感

44 个未命中 taxonomy 的 sink 样本：`finish()`、`Log.e`、`setResultData`（**常规操作，非敏感**——不该成为候选链终点）与 `LoginManager.getPrefEncryptedUserId`（读加密用户 ID）、`PreferenceUtil.setStringPref`（写偏好）、`PushClient.getInstance`（**业务级敏感**——taxonomy 合理扩展候选）混杂。

**探索候选的 sink 选择偏泛**——模型未聚焦敏感操作方向（部分候选链无安全意义）。

### 根因 4【方向覆盖】：shop 唯一目标 extra-close 未被追到

MainActivity（shop2）被充分探索（**22 个候选**，loop_done 正常终止）但集中在 `onActivityResult`→LoginManager/ReactNativeFragment 方向——**未追 onCreate 的 `extra_close_url`→`go2CloseSet`→startActivity 分支**（V-02 漏洞路径）。轮次预算内模型的方向选择 + go2CloseSet 不在 seed 前 8 跳。

### 根因 5【入口截断】：50 候选上限致 73/278 入口后停止

`max_candidates_per_run=50` 触发后探索停止——**205 个入口（74%）未被探索**。上限保护成本但截断覆盖（目标组件若在后段则永不探索）。

### 附：wall-time 61min（旧 33min）

siliconflow 响应较慢（~15-25s/请求）+ verify 不再秒败（走满轮次）——预期内的成本转移。

## 结论

**探索轨的"产出质量"修复已实证生效**（跳回查 100%、partial 11.5 倍、verify 27/29），但"预期产出"受三层结构性因素制约：**评估分母错位**（跨 APK case）、**sink taxonomy 生态缺口**（探索发现的新 sink 需人工扩充管线）、**探索方向选择**（敏感度聚焦与目标组件引导）。

## 修复方向（按优先级）

| # | 方向 | 类型 | 效果预期 |
|---|---|---|---|
| 1 | golden 标注按 APK 分域（case 加 apk 域字段，评估按 run 的 APK 过滤分母） | T4.1 数据+代码小改 | 修正评估口径——health run 验证时 5 个 case 才是真分母 |
| 2 | sink taxonomy 扩充（人工评审 44 个未命中 sink——真敏感的加入，如 LoginManager 隐私读取类） | 数据工作（设计内的 custom sink proposal 管线落点——T2.7） | partial→validated 转化（44 个封顶候选的解封） |
| 3 | 探索 prompt 强化 sink 敏感度约束（sink 须为敏感操作——隐私/IPC/文件/网络/反射等类别语义，非 finish/Log 类常规操作） | prompt 迭代（harness 分钟级验证） | 提升候选有效率（减少无安全意义链） |
| 4 | 入口覆盖策略（50 上限前的业务组件优先级 / 上限提升配合 taxonomy 扩充） | 产品迭代 | 覆盖率提升（73→278） |
| 5 | 目标组件引导（探索入口与规则轨 finding 组件的交叉提示） | 产品迭代（大） | extra-close 类已知漏洞组件的方向覆盖 |
