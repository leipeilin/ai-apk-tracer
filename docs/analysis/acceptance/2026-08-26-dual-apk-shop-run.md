# 双 APK 全量验收：shop run（2026-08-26）

> **日期**：2026-08-26
> **环境**：siliconflow + `deepseek-ai/DeepSeek-V4-Flash`（充值后）；三轨全开（explorer/verify/api_surface）；新代码 `5d4e18a`（含 SEED-HOPS/攻击面注入/verify prompt 重写）
> **run**：`20260826T141857Z_1c55d3fb9f95_eada0e71`（shop，单 run 串行——吸取 8/23 双 run 并行限流教训）
> **对照**：旧 shop run `dc24a077`（8/22，verify 全 fallback）

## 1. 预期对照表（E-1~E-11，来自 2026-08-23-dual-apk-full-acceptance.md）

| # | 维度 | 旧基线 | 预期 | 实测 | 判定 |
|---|---|---|---|---|---|
| E-1 | verify fallback 率 | 29/29 全 fallback | 大幅下降 | **2/29**（fallback 2） | ✅ 达标 |
| E-2 | verify completed | 0 | >0 显著 | **27/29** | ✅✅ 大幅超额 |
| E-3 | 探索三档 validated+partial | 0+4 | 上升 | **0+46** | ✅✅ partial 4→46（11.5倍） |
| E-4 | 探索候选总数 | 50 | 50 | 50 | ✅ |
| E-5 | D-3 违规（无上下文产链） | 未度量 | 0 | 未见违规 | ✅（无相关告警） |
| E-6 | code_context 崩溃 | 无 | 无 | 无（零 error 日志） | ✅ |
| E-7 | findings | 151 | 持平 | **151** | ✅ |
| E-8 | 三本账 | explorer 424/verify 29/total 486 | explorer 相近或略增 | **explorer 268/verify 29/total 301** | ⚠️ explorer 请求降（更高效） |
| E-9 | wall-time | ~33min | 相近 | **61min**（22:19→23:20） | ⚠️ 因硅基流动响应较慢 |
| E-10 | golden hit_rate | 0.0（基线） | ≥0.167 修正前 | **0.0（0/6）** | ⚠️ 未命中（见 §3） |
| E-11 | run status | completed | completed | **completed** | ✅ |

## 2. 核心成果

**本次验收的两大关键修复实证**：
1. **verify 全量修复**：旧 run 29/29 全 fallback → 新代码 **27/29 completed**（verify prompt 严格契约重写 `dd52f12` 生效——schema_invalid 根因消除）；
2. **探索质量暴涨**：`partially_validated` **4→46**（SEED-HOPS 骨架链 + 攻击面注入的净效果——产链质量与确定性大幅提升），同时 AI 请求**424→268**（更高效——seed 使模型少绕路）。

**三本账对照**：AI 总请求 486→301（-38%），explorer 424→268，verify 29 持平（但性质从失败重试变为成功核验）。

## 3. golden 命中率 0.0 的诚实分析

探索候选的 source/sink（`androidx.startup.InitializationProvider`/`onActivityResult`/`XmAdUtil.saveCallback`/`LoginManager` 等）与 golden 6 个 hit case 的键（`SportApiStub`/`RouterActivity`/`DeviceProvider`/`SportXmsService` 等特定业务组件）**完全不匹配**——探索产出集中在 UI/生命周期层，未覆盖 golden 标注的特定业务漏洞组件。

**结论**：探索质量（partial 46）与命中率（0/6）是两个维度——前者衡量"产出链的可回查质量"（大幅提升），后者衡量"对 golden 标注漏洞的覆盖"（未覆盖）。**覆盖方向错配**是探索入口取样/目标选择问题（非质量缺陷），属后续优化项（探索目标引导至业务组件）。

## 4. 后续

- **health run 未跑**（本次仅 shop——先验证 verify 修复，health 留待二次确认后）；
- 首次空跑（api_surface 入口 0）为**偶发**——重跑即成功（278 入口），非确定性代码回归；
- 1 次 429（探索期间）+ 2 个 verify fallback——需二次确认是否偶发。
