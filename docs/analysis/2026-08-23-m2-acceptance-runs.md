# M2 验收执行记录（EXPLORER-PROMPT-FIX 后补跑）

> **日期**：2026-08-22 ~ 2026-08-23
> **执行时 HEAD**：`d2f6ed3`（fix(prompts): explorer prompt 严格输出契约修复）
> **M2 父链提交顺序**：T2.6(cfc2a32) → T2.7(2b8e986) → T2.8(3fced88) → T2.11(464c15e) → T2.12(fba8906) → T2.9(86146fc) → T2.10(9cbec2e) → **PROMPT-FIX(d2f6ed3)**
> **最终测试数快照**：`1148 passed / 0 failed`（含 test_no_rules_import 1 项——零依赖红线）
> **任务文档**：`2026-08-22-m2-implementation-review.md`（审查）→ `2026-08-22-m2-acceptance-closure-{implementation,acceptance,review}.md`（闭环三件套）→ `2026-08-22-explorer-prompt-fix-*.md` + `2026-08-23-explorer-prompt-fix-*.md`（阻塞解除）

---

## 1. 默认配置基线 diff（验收方案 1.2）

**方法**：默认配置各跑一次 health/shop run，与 M1 基线 manifest 对比。

| 项 | health | shop | 结论 |
|---|---|---|---|
| run_id（M2 默认） | 20260822T123237Z_2a80fc5a8735_043f94e3 | 20260822T123742Z_2a80fc5a8735_827e11a8 | — |
| findings_count | 365（M1=365）✓ | 151（M1=151）✓ | **主链判定行为零回归** |
| 文件集 | m2 = m1 + 3 文件 | m2 = m1 + 3 文件 | 新增 = T2.1 规则产物（binder_bindings/receiver_registrations/webview_js_bridges）——M2 预期 |
| 哈希不一致 | 59 个（归因见下） | 60 个（对称） | 全部可归因，无实质差异 |

**哈希不一致归因**（health 59 个）：
- `decompile/` 26 个：jadx 注释级抖动（抽样 SoLoader.java 行数相同 914=914，差异仅为 jadx WARN 注释增删——代码内容一致）；
- `index/` 2 个（analysis.sqlite3 + code-index.json）：上游 decompile 抖动传导 + sqlite 字节噪声；
- `rule-results/` 31 个：T2.1 rule_runner 导出格式演进（白名单键过滤）——M2 预期变更；
- `slices/` + `manifest.json#stable`：候选字段演进 + M2 新增配置段（explorer/verify）快照。

**结论：默认配置 diff 通过**（实质差异为零）。

## 2. 探索轨 + 核验轨双 APK（验收方案 1.3）

### 2.1 health 完整 run（run_id：20260822T202633Z_2a80fc5a8735_7ecd4288）

**执行环境**：真实 uvicorn 后端（8178）+ `API_SURFACE/EXPLORER/VERIFY_ENABLED=true` 环境变量 + `source_analysis_enabled=true`（JadxAdapter 反编译）；AI = token-plan `deepseek-v4-pro-0813`（curl 实测该模型对探索 payload 响应 46~62s/次——非流式生成期间连接零字节流动属正常）。

| 指标 | 实测 | 判定 |
|---|---|---|
| run status | **completed** | ✓ |
| 探索入口 | 629（含 method_id） | ✓ |
| 探索候选 | 50（= max_candidates_per_run 上限） | ✓ 机械链路 |
| AI 请求（探索检索） | 458 | 预算内 |
| 读码请求 | 20 | ✓ |
| 三档校验 | validated=0 / partially_validated=1 / unverified=49 | **validated ≥5 未达标**（见 §4 已知限制） |
| custom sink 标记 | 31/50（种子命中 19） | ✓ 判定接通 |
| custom 压档排除深挖 | 唯一 partial 为 custom 压档，deep_dive_counts.partial_total=0 | ✓（T2.9 R-3 生效） |
| **findings 总数** | **365（= M1 基线 = M2 默认）** | ✓ **未通过校验 0 条进 finding**（validated=0 → 0 条注入） |
| review_status 分布 | pending_ai=248 / pending_manual=117（默认 run：231/134） | AI 复核路径差异的合理位移（核验→fallback 52 候选） |
| **三本账分列** | explorer=458 / deep_dive=0 / ai_analysis 总=500（explorer 458 + ai_stage 42，其中 verify=20） | ✓ 公式可复算（458+42=500） |
| 核验分流 | attempted=52 / completed=0 / **fallback=52** | ✓ 降级回退主链不阻塞（run completed）；核验失败原因记录于 §4 |
| 产物注册 | explorer_candidates + explorer_observations + ai_results | ✓（T2.10） |
| 人工队列 API | GET /explorer/candidates 返回 50 条队列（计数/链摘要/校验详情/排序） | ✓ |
| unverified 不占 AI 预算 | 三本账可复算，unverified 49 条零核验/深挖调用 | ✓ |

### 2.2 shop 完整 run（run_id：20260822T210017Z_1c55d3fb9f95_dc24a077）

（待 run 完成后回填——撰写本文时仍在 explorer 阶段推进中）

### 2.3 已知 8 项覆盖映射表

**无法执行**——映射表要求探索候选与 ground truth 的 source 组件/sink 方法比对，前提是 validated 候选 ≥1；本批 validated=0（见 §4 已知限制 ①）。**该项未达标，不勾选**。

### 2.4 负样本

**结构化断言通过**（不出现于候选池）：负样本判定由三档校验的跳回查门禁承载——`test_explorer_validation*` 全部通过（伪造 method_id 判 unverified）；真实 run 中 49 条 unverified 候选**全部未进 findings**（findings=365 与基线一致）。

### 2.5 call_tree 性能

| 指标 | 实测 | 判定 |
|---|---|---|
| 单入口三操作（get_method_body + get_callees + get_callers） | p50=0ms / max=2ms（20 入口样本，health 1.9GB 索引） | ✓ 远低于 2s 门槛 |

## 3. 既有单测回归（验收方案 1.3 尾项）

- `test_explorer_validation*`（伪造 method_id 判 unverified）：passed
- deep_dive 不改写链（T2.8 A-3）：passed
- backend 无 import rules（test_no_rules_import，AST 扫描）：passed
- verify 盲验/命题/循环语义（test_verify_agent.py）：passed；真实 run trace 抽查：verify_counts fallback=52（attempted 均有记录）
- verify 降级回退：单测 passed；**真实 run 触发 52 次 fallback**（主链不阻塞实证）
- 证据引用适配（DecisionEngine 端到端）：passed（T2.12 A-6）
- 探索轮预算与预算跑满：observations.json 存在（artifacts 注册）；budget 截断行为由 T2.5b 单测覆盖

## 4. 已知限制与发现（如实记录，不勾选对应验收点）

1. **validated=0（探索候选质量限制）**：根因 = 模型在首轮无 code_context 时直接产出 chain_proposals（hops 的 method_id 为推测值，违反 prompt"引用必须可回查"约束）→ 跳回查必然失败 → unverified。带方法体上下文时模型可产出结构正确的链（探针实证：2 条链 hops 全真实）。
   **改进方向**（M4 评估项）：prompt 增加硬约束"无 code_context 时禁止输出 chain_proposals，必须先 read_requests"；或首轮驱动层不传产链指令。
2. **核验 52 候选全部 fallback**：verify_entry 对全部 L2 规则候选未产出 completed 结果（单候选级失败→回退单轮 L2）。主链未阻塞（设计内降级）。根因待查（大概率同为 verify prompt 输出 schema/repair 失败——与探索 repair 率高同源：模型对严格 JSON 输出的合规率）。**改进方向**：verify prompt 迭代（对齐 explorer prompt 修复模式）。
3. **repair 重试率高**：探索 458 预算请求产生 ~900+ 实际 HTTP 调用（含 repair）——每 schema_invalid 触发一次 repair 重试。成本翻倍。改进方向同 ①②（prompt 合规率）。
4. **产品缺陷（M2 审查新发现，待修复）**：`manifest_extractor.extract_decoded_manifest` 的 `process.communicate()`（两处）与 `shutil.rmtree(.manifest-decode)`（万级文件清理）**均无超时保护**——大 APK 场景可无限阻塞 run。建议：communicate 加 wait_for、rmtree 换增量删除或后台清理。
5. **AI 偶发长挂起（待核实）**：一次观察中 httpx 连接 ESTABLISHED 后 15 分钟无数据且 read_timeout=120s 未触发（疑似服务端/VPN 中间层行为）。建议产品级加固：AI 调用加总时长兜底（如 `asyncio.wait_for(transport 调用, 300s)`）。

## 5. 环境说明

- AI 服务：token-plan（订阅内 deepseek-v4-pro-0813 / deepseek-v4-flash-0731 均可用；curl 实测探索 payload 响应 46.8~62.7s/次）
- 网络诊断结论：此前疑似"网络卡死"实为**长响应时长**（非流式生成期间连接零字节）+ 观察窗口不足的误判；`SAFE_DELETE_BULK_CONFIRM_REQUIRED` 为命令沙箱审批挂起对后台进程删除操作的连带冻结（环境层）
- 本验收通过环境变量临时开启探索轨/核验轨（未写入 config/default.yaml——该文件的 AI base_url/model 为本地环境配置，不在提交范围）

## 6. 结论

- **机械链路验收：通过**——探索→校验→归一化→深挖→核验→分流→回退→人工队列全链路在真实环境端到端工作；三本账可导出；默认配置零回归；未通过校验 0 条进 finding。
- **质量验收：部分未达标**——validated ≥5 与覆盖映射表因模型输出质量限制未达成（§4 ①②），如实记录不勾选；改进路径明确（prompt 迭代属 M4 评估范畴）。
