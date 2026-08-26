# 探索轨产出偏差修复计划（2026-08-27）

> **依据**：`acceptance/2026-08-26-explorer-output-gap-analysis.md`（五层根因 + 五项修复方向）
> **状态**：待确认（用户批准后按序实施）
> **总体策略**：F1/F2 先行（度量口径 + 生成侧约束——否则后续验证的度量仍是错的）；F3 并行出评审清单（人工确认环节在用户）；F4 最小化透明化；F5 记 backlog 不在本轮。

---

## F1：golden 评估按组件存在性动态分域（修根因 1——分母错位）

**方案**：不靠静态 apk 标注（需维护、易漂移），改为**评估时动态过滤**——`evaluate_explorer_against_golden` 读该 run 的 `index/manifest.json` 组件清单，`explorer_expected.expectation=hit` 的 case 若其组件（case 的 component 字段）**不存在于 run 组件清单** → 剔除出分母（记入 `excluded_cases`——跨 APK/合成 case 天然被排除）。

**改动**：
| 文件 | 内容 |
|---|---|
| `backend/app/evaluation/runner.py` | `evaluate_explorer_against_golden(run_dir, cases)` 增组件存在性过滤（读 run 的 index/manifest.json，按 case.component 精确匹配）+ 输出 `excluded_cases`/`in_scope_hit_total` |
| `backend/tests/test_evaluation_runner_runs.py` | 过滤逻辑测试（存在/不存在组件的 case 分母处理）+ excluded 透明性断言 |
| `evaluation/baselines/` | 双基线重刷（shop 的 hit_total 6→1 口径修正） |

**验收标准**：
- A1-1 单测：含组件存在 case 与不存在 case 的合成 fixture——分母只计存在者，`excluded_cases` 列出被排除项；
- A1-2 实测：对 `eada0e71`（shop）重跑 `runner --runs`——`explorer_hit_cases_total` 从 6 变 **1**（仅 extra-close 的 MainActivity 在 shop 组件清单），health 的 5 case 进 `excluded_cases`；
- A1-3 全量 pytest 零回归。

## F2：探索 prompt 注入 sink 敏感度约束（修根因 3——B 类无效链）

**方案**：`prompts/explorer/1.0.0/system.md` 新增硬约束 13——把 sink taxonomy 的 **9 类敏感语义**告知模型 + 禁止常规操作当 sink：

> 13. sink 敏感度约束：chain_proposals 的 sink 必须是**敏感操作**——属于以下九类语义之一：UI 导航/反射实例化、连接与会话控制（bindService/connect）、事件注入（sendBroadcast/postValue）、位置与传感器采集、设备协议输出（BLE/USB/NFC 写）、持久状态写（SharedPreferences/Settings）、数据库变更（insert/update）、文件读写、数据披露（隐私数据外发/读取）。**禁止**把 UI 生命周期（finish/onDestroy）、日志（Log.*）、结果回传（setResult）等常规方法当 sink——这类链无安全意义，浪费候选预算。不属于九类但确有敏感性的操作（如隐私数据读取的封装方法）须在 reasoning 中明确论证其敏感性。

**改动**：system.md + registry 哈希同步 + `test_explorer_protocol.py` 断言（九类语义/禁令 token）。

**验收标准**：
- A2-1 协议断言：prompt 含九类语义声明与"禁止…当 sink"禁令 token；
- A2-2 **探针复验**（harness 分钟级）：`probe_explorer_entry.py` 对 shop run 6 入口——产出候选的 sink 中 B 类（finish/Log/setResult 类）**降为 0**（本次基线：44 封顶中约 1/3 为 B 类）；D-3 与 seed_hit_rate 不回退；
- A2-3 全量 pytest + sync --check 零回归。

## F3：sink taxonomy 人工评审扩充（修根因 2——44 封顶候选解封）

**方案**（分三步，人工确认在用户）：
1. **我出评审清单**：44 个封顶 sink 逐个分类——**A 类**（真敏感，建议扩充：给出 taxonomy 类别归属与理由，如 `LoginManager.getPrefEncryptedUserId → data_disclosure`）/ **B 类**（否决：常规操作，理由）——落盘评审文档；
2. **你确认**：勾选同意扩充的条目；
3. **执行扩充**：`scripts/promote_custom_sink.py` 追加（`taxonomy_version` 1.0.0 → 1.1.0，`source: manual`，receiver 约束按评审指定）→ `revalidate_run_candidates` 对 eada0e71 的 50 候选重校验（副本不落盘）→ A 类候选解封 **partial → validated**。

**验收标准**：
- A3-1 评审清单文档（44 条全覆盖，每条 A/B 判定 + 归类 + 理由）；
- A3-2 扩充后 `taxonomy_version` 递增且 `sync`/全量回归零破坏；
- A3-3 **重校验解封数**：A 类候选从 partial 升 validated（预期 5~15 个——取决于你确认的扩充量）；run 级指标 `validated` 从 0 转正；
- A3-4 评审与扩充全程可审计（清单文档 + git 提交记录）。

## F4：入口覆盖透明化（修根因 5——最小化）

**方案**：不改变 50 上限（成本保护合理），只做**透明化**——`explorer.py` 的 `explore_all` 完成后在 stage summary 记录 `entries_total / entries_explored / entries_unexplored`（上限截断可见）。

**验收标准**：
- A4-1 单测：上限触发时 summary 含未探索计数；
- A4-2 eada0e71 口径回填（278 总入口/73 已探索/205 未探索——写入验收记录）。

## F5：目标组件引导（根因 4——backlog 不实施）

探索输入注入规则轨 finding 组件交叉提示（跨轨联动改动大）——记 backlog，待 F1~F4 效果评估后决策。

---

## 实施顺序与依赖

```
F1（口径修正——先行，否则后续度量失真）
  └→ F2（prompt 约束——harness 快验）──┐
F3-1（评审清单——与 F1/F2 并行出）        ├→ F3-3（扩充+解封——依赖你确认）
F4（透明化——独立小改）                  ┘
```

## 总验收（实施完成后）

1. 全量 pytest 零回归（基线 1255）；
2. **可选加分项**：health run 串行跑一次（5 个 health hit case 成真分母——F1 口径下的完整验证 + explore/verify 双轨健康度），约 1.5 小时；
3. 验收记录落盘 `acceptance/2026-08-27-explorer-gap-fix-acceptance.md`。
