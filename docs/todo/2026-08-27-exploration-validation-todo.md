# 待办清单：探索轨验证阶段（2026-08-27）

> **来源**：F5 闭合后的价值核查与参数机制分析（本轮对话沉淀）。
> **排序原则**：数据依赖优先——多数项以"全量 run（P-1 参数包）"为前置。

---

## T1【高】全量 run 启动 + 三参数**恢复**定参（临时放开后回归）

- **内容**：用 P-1 参数包（候选无上限 / 上下文 40K+保后切前 / read_timeout 240s）+ `.venv-tls` 跑 shop 全量（278 入口，~1-2h）；
- **产出**：三分布数据（上下文尺寸 / 单次调用时长 / 各轮失败率）——**据数据把三参数恢复为合理值**（验证阶段放开是临时的：无上限候选/超大上下文不是常态运行形态——数据到位后定参恢复，如候选上限按产出分布定、上下文按实际使用尺寸定、超时按时长分位数定）；
- **强调**：`max_candidates_per_run: null`、40K 上下文、240s 超时均为**临时验证值**，定参后必须恢复——本项不闭合则参数永久裸奔；
- **前置**：P-1 实施（`2026-08-27-p1-validation-params-implementation-plan.md`）。

## T2【高】A5-8 golden 判决 + 引导域产出观察

- **内容**：全量 run 的 explorer candidates 对 golden 评估——extra-close（shop 唯一域内 golden）是否命中；F5 引导域 13 个此前未探索入口（push 三件套/分享链/插件透明 Activity）的候选产出；
- **判决意义**：golden 破零 → 探索轨"新模式探测器"定位成立；仍零 → 收缩为规则 finding 深挖附件（砍入口预算、只跑 finding 组件域）。

## T3【中】轮失败韧性——"轮失败即弃入口"缺口

- **现状**：单轮 AI 失败（2 次尝试后）终止整个入口，已积累的轮上下文全部丢弃（8/22 run：65/131 入口 error，含 146 个 completed 轮的投入）；
- **决策依据**：T1 全量 run 的 error 率——若新供应商+240s 下 error 归零则不修；仍有则实施"失败轮跳过继续下一轮"或"入口级重试"；
- **关联**：todo T4。

## T4【中】超时类失败跳过重试（重试对超时无效）

- **机制**：超时失败后重试同输入——推理时间不变、退避仅 0.05s 起步——第二次几乎必然同样超时（纯浪费 120-240s 等待）；
- **方案**：transient 细分——网络类（connect/reset）保留重试，超时类（ReadTimeout）直接降档继续或跳过；T1 数据定夺。

## T5【中】OpenSSL 3.6.3 与 siliconflow TLS 不兼容——正式修复

- **现状**：主 venv（Python 3.12 + OpenSSL 3.6.3）对 siliconflow ALB 全 IP TLS 握手失败（WRONG_VERSION_NUMBER——疑似 PQ groups ClientHello 被拒）；`.venv-tls`（Python 3.14 + OpenSSL 3.5.5）绕过；
- **正式方案候选**：backend venv 升级 Python 3.14（需过规则运行时 3.12 版本门禁改造）或固定 OpenSSL 3.5.x 构建；
- **影响**：所有 AI 调用（探索/验证/L1/L2）在主 venv 不可用——全量 run 必须用 `.venv-tls`。

## T6【中】动态注册 receiver 81 入口的暴露性分析

- **现状**：shop run 81 个动态注册 receiver（`exported=None`）完全未被探索轨触达——暴露性取决于注册点 flag（RECEIVER_EXPORTED/RECEIVER_NOT_EXPORTED）与注册 Context；
- **方案候选**：api_surface 阶段消费 `receiver_registrations` 产物解析注册点语义 → 入口表补 exported 判定 → 纳入探索与规则轨；
- **前置**：T2 判决（探索轨定位成立才值得扩面）。

## T7【低】未探索入口价值复验

- **内容**：T1 全量 run 后对照 2026-08-27 核查结论（13 引导域 / 37 敏感语义 / 12 exported 未探索）——F5 排序 + 无上限后这些入口的覆盖与产出变化；
- **意义**：F5 引导有效性的全量实证（探针级已 PASS，全量待证）。

## T8【低】工作区遗留改动处置

- **内容**：sink-taxonomy-sync 遗留（`config/default.yaml` siliconflow 切换 + `rules/sink_taxonomy/versions.yaml` + `docs/analysis/rules-review/2026-08-27-p1-sink-taxonomy-sync.md` + `scripts/check_sink_taxonomy_sync.py`）——评审后提交或处置；
- **注意**：`config/default.yaml` 的 siliconflow 切换与 T5 强相关（正式启用需 TLS 修复先落地）。

## T9【低】gap-fix-plan backlog 复查

- **内容**：F1-F5 全部闭合后复查 gap-fix-plan 剩余项——据 T2 判决结果决定 backlog 去留（定位收缩则部分项作废）。

---

**执行顺序建议**：P-1 实施 → T1 全量 run → T2 判决（分叉点：探测器定位 vs 收缩）→ T3/T4/T6/T7 按数据 → T5/T8 独立。
