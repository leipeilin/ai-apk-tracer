# 任务评审报告：M3-1（事后补评审）

> **评审对象**：`2026-08-23-m3-report-poc-implementation-plan.md` + backend/app/reporting/ 实施 + API 端点 + `2026-08-23-m3-report-poc-acceptance.md`
> **评审日期**：2026-08-23
> **评审模型**：deepseek-v4-flash（独立只读子 agent）
> **状态**：已闭合（一轮）
>
> **流程偏差声明**：M3-1 实施时跳过了 plan-driven-implementation 阶段 3/4（前置评审与讨论闭合）——方案落盘后直接实施。本评审为**事后补评审**；评审确认其中 R-1/R-2/R-3/R-5/R-6 五项为本可在方案阶段拦截的问题。处置：全部按流程闭合修订（见处置记录），后续任务严格执行六阶段。

## 1. 评审结论摘要

M3-1 交付物整体质量合格：provider 抽象、provenance 诚实标注、零可执行产物的生成侧保证均兑现方案承诺，17 用例 + 真实 V-01/V-02 端到端形成可复算证据链。但存在两处实质缺陷——验收自查②的引用回查断言强度远弱于记录表述，以及 L1 拒绝口径遗漏先例的 informational 分支。若前置评审未跳过，R-1/R-2/R-3/R-5/R-6 五项本可在方案阶段拦截。

## 2. 问题清单与处置记录

| 编号 | 严重度 | 问题摘要 | 处置 | 修订动作（实测） |
|---|---|---|---|---|
| R-1 | 高 | 验收自查②断言弱：`checked > 0` 不存在的引用不失败；方案承诺的"全部引用可回查"未落实 | **采纳（主体）** | 测试改逐条全量断言（每条 path `assert exists`——25 用例全过，V-01/V-02 全部引用命中）；fixture 最小源码树不采纳（真实产物逐条断言已达验收强度且成本更低——skip 条件保留 CI 容错） |
| R-2 | 高 | L1 拒绝口径缺 `severity=="informational"` 分支（先例双条件）——informational finding 可绕过；验收记录"沿先例"表述不实 | **采纳** | generator 补双条件 + 测试用例 + 本文档勘误声明 |
| R-3 | 高 | 大纲 T3.2"探索假设描述种子"（hypothesis/impact_proposal/component_summary 作种子并标注来源）未实现——explorer finding 的 ai_draft 与规则候选无差异 | **采纳（记录性处置）** | 数据源（M1 规则轨 finding）无探索假设字段——投影分支读取 explorer 字段留 M3-2（数据源就绪时）；方案 §6 遗留补记 + 大纲回写义务移交 M3-2 |
| R-4 | 中 | 配置双源不一致：端点 `get_settings()`（每次重建）忽略 `create_app` 注入的 `app.state.settings`；env 可翻转门禁且注入失效 | **采纳** | 端点改 `request.app.state.settings.report`（与 assets 端点同源） |
| R-5 | 中 | 落盘安全弱于先例：无 symlink 防护（预置 symlink 可写穿）；finding_id 无字符白名单；缺 id 时 "unknown" 兜底可致多 finding 覆盖 | **采纳** | save 加 symlink/非常规文件拒绝（REPORT_DRAFT_PATH_UNSAFE）+ finding_id 白名单（FINDING_ID_INVALID）+ 缺 id 拒绝（FINDING_ID_MISSING）+ 3 测试用例 |
| R-6 | 中 | `executable_files_created` 恒空仅是构造约定——pydantic 字段可外部传入非空，M3-2 provider 可绕过 | **采纳** | models 加 `field_validator` schema 级强制 + 绕过测试用例 |
| R-7 | 中 | API 层零集成测试：409/422/404 HTTP 映射未经验证（依赖 main.py handler 组合行为） | **采纳** | 补 TestClient 集成类（fake repository/storage 注入：三拒绝路径状态码 + 正向 200 + 落盘验证 + 404） |
| R-8 | 低 | 函数体内 import 偏离文件惯例 | **采纳** | 移至顶层 |
| R-9 | 低 | 唯一 async 端点内同步 IO 阻塞事件循环 | **采纳（注释处置）** | docstring 说明为 M3-2 async provider 铺路（届时统一异步化） |
| R-10 | 低 | provider_query 命令混入真实 authorities（近乎可执行，违背全占位符承诺）；`type: ignore` 与"无 lint 绕过"相悖 | **采纳** | authorities 占位符化（`content://<AUTHORITY>/<PATH>`）；`_skeleton_kind` 返回 `PocKind` 消除 ignore |
| R-11 | 低 | summary 截断无省略标记；deterministic 投影缺 package_name（先例 _poc_guide 用真实包名） | **采纳** | 截断加 "…"；`_DETERMINISTIC_FIELDS` 补 "app" |

## 3. 认可项（摘）

1. 取舍 1 兑现：provider 抽象隔离 M3-2，衔接点由测试锁定。
2. 取舍 2 兑现：provenance 诚实标注，prompt_version/model 留 None 不冒充。
3. 取舍 3 兑现且 fail-safe：配置违例先于状态检查（部署级故障 fail-fast）。
4. EvidencePointer 偏差论证成立（EvidenceReference 的 context_id/claim 必填已核实）。
5. Binder ADB 限制注记沿先例；异常语义经 AppError handler 正确映射。

## 4. 闭合结论

R-1~R-11 全部采纳（R-1 主体采纳 + 理由说明、R-3 记录性处置归 M3-2）。修订后：专项 **25 passed**（+8 评审闭合用例）；全量 **1203 passed / 0 failed**；ruff 零错误。M3 专项验收 §4.4"全部代码引用可回查"经 R-1 强化后真正达成。
