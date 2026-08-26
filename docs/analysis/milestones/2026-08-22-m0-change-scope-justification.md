# M0 改动面论证补记（响应阶段审查 §4.1）

> **日期**：2026-08-22
> **响应**：`docs/analysis/milestones/2026-08-22-m0-implementation-review.md` §4.1——通用门禁"默认配置下基线 APK 产物 diff 为空"未在 M0 验收中实证，本补记以**改动面论证**作为替代证据（审查建议的方案 B）。
> **范围**：commit `34a3daa~1..26f7b0a`（M0 全部 11 个提交，53 文件，+6930 行）。

---

## 1. 核心论证：运行路径零修改

对 M0 全部变更文件执行运行路径过滤（`orchestrator.py` / `ai.py` / `candidate_funnel.py` / `decision.py` / `evidence.py` / `rule_runner.py` / `repository.py` / `api/routes.py` / `context_builder.py` / `guard_verifier.py` / `aggregation.py`）：

**结果：零命中**——M0 未修改任何 run 执行路径上的代码文件。run 流程（decompile → index → rule_prescan → funnel → slicing → ai_analysis → evidence → decision → aggregation）的字节级行为与 M0 前完全一致。

## 2. 变更文件分类（53 文件）

| 类别 | 文件 | 运行时影响 |
|---|---|---|
| 运行时代码（纯新增，2 个） | `ai_models.py`（新增 24 个模型类 + 三处注册 dict 追加条目，**未修改任何既有模型**）、`config.py`（新增 6 个 Settings 段类 + Settings 字段追加 + `resolved_assets_data_root` 方法，**未修改既有段**） | 新模型/新段无任何调用方（explorer/verify/api_surface/assets/batch/report 功能实现分别在 T2.x/M1/M3）；既有模型与配置段字段值不变 |
| 配置/协议数据 | `default.yaml`（**追加** 6 段，既有段零改动）、`prompts/registry.yaml`（追加 2 条 explorer-deep-dive/verify，既有条目哈希不变——`sync --check` 通过）、`config.schema.json`（追加 6 段）、11 个新 schema 文件 | 新段全部 `enabled: false` / 保守默认；新协议无编排代码调用（先声明后注册守卫测试断言） |
| prompt 模板 | `explorer-deep-dive/1.0.0`、`verify/1.0.0`（各 2 文件） | 新协议，无调用方 |
| 测试（1 新 3 改） | `test_normalization_mapping.py`（新）、`test_ai_models.py`/`test_config.py`/`test_rule_artifacts.py`（追加） | 仅测试层 |
| 依赖 | `pyproject.toml`（dev 加 ruff、test 加 jsonschema） | 不改变运行时依赖 |
| 文档 | 27 份任务三文档 | 无 |

## 3. 默认行为不变的测试证据

- **既有测试全量基线**：M0 前 795 passed + 3 pre-existing guard_verifier 失败；M0 后 903 passed + **同一组** 3 个 pre-existing 失败（T0.1 起每任务 stash 隔离验证并披露）——**既有 795 项中无一项因 M0 由绿变红**。
- **配置基线断言**：`test_default_yaml_loads_with_new_sections` 显式断言 `context_budget.max_output_tokens == 8000`（既有段值不变，以 default.yaml 为准）。
- **Schema 一致性门禁**：`test_committed_schemas_exactly_match_stable_model_generation` 覆盖全部（含既有）schema 文件与模型逐字节一致——既有 schema 未漂移。

## 4. 结论与后续承诺

M0 改动面不含任何运行路径修改、默认配置全关、既有测试基线无回退——**默认配置下 run 产物与 M0 前一致的论证成立**。

**基线双 APK（health/shop）产物 diff 实证**：将在 **M1 T1.1 开工前建立**（T1.1 首次触碰 `repository.py` 迁移，是真正的行为变更点，届时以 diff 实证替代论证），作为 M1 通用门禁 §4.1 的执行记录。
