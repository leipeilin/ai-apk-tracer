# 任务验收方案：T0.7（配置模型扩展）

> **任务编号**：T0.7
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/explorer-track/2026-08-22-t0-7-implementation-plan.md`
> **状态**：起草
> **验收方式**：pytest 单测 + YAML/schema 加载验证 + 全量回归

---

## 1. 验收范围

- 6 个配置段三方同步（config.py / default.yaml / config.schema.json）+ 测试。

## 2. 验收点清单

| 编号 | 验收项 | 验收方式与命令/步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 6 段默认值正确 | `Settings()` 构造后逐段断言默认值 | explorer.enabled=False、verify.enabled=False、api_surface.enabled=False、assets.enabled=False、batch.max_ai_calls=0、report.allow_executable_poc=False |
| A-2 | 嵌套默认值 | `Settings().explorer.call_tree.max_depth == 8`、`verify.max_rounds_per_candidate == 4` | 通过 |
| A-3 | 环境变量覆盖 | `AI_APK_TRACER_EXPLORER__MAX_ROUNDS_PER_ENTRY=6`、`AI_APK_TRACER_VERIFY__ENABLED=true` | 覆盖生效 |
| A-4 | default.yaml 加载 | `get_settings()` 后 6 段存在且默认值正确；现有段值不变（**基线以 default.yaml 为准**，既有 config.py/yaml 漂移不归因 T0.7；评审 R-7） | 通过 |
| A-5 | config.schema.json 同步 | 顶层 required 含 6 段名；关键字段描述存在；`assets.data_root` 为 string 类型（评审 R-3） | 通过 |
| A-6 | batch 0 语义 | `BatchSettings(max_ai_calls=0, max_wall_seconds=0)` | 通过（0=沿用/不限） |
| A-7 | 测试通过 | `.venv/bin/python -m pytest tests/test_config.py -q` | 全部通过 |
| A-8 | 全量回归 | `.venv/bin/python -m pytest -q` | 除 3 个 pre-existing guard_verifier 失败外全部通过 |
| A-9 | 统一校验 | `scripts/check-all.sh` | 同上，无新增失败 |

## 3. 回归标准

- [ ] 既有配置段（ai/funnel/context_budget 等）测试全部通过，默认值未被改动。
- [ ] `ruff check` 通过。
- [ ] 全部新段默认关闭（enabled=False 或保守默认），不改变现有 run 行为。

## 4. 边界与负例

| 编号 | 场景 | 输入/操作 | 预期行为 |
|---|---|---|---|
| N-1 | 未知段忽略 | `Settings(explorer_bogus={"x":1})`（extra="ignore"） | 不报错，忽略 |
| N-2 | 非法字段类型 | `Settings(batch={"max_ai_calls": -1})` | 抛 `ValidationError`（ge=0）；正向断言：`batch` 未提供时 `max_ai_calls==0`（评审 R-6） |
| N-3 | 非法枚举/范围 | `Settings(explorer={"call_tree": {"max_depth": 0}})` | 抛 `ValidationError`（ge=1） |
| N-4 | YAML 缺新段 | 旧 default.yaml（无新段） | `get_settings()` 用模型默认值，不报错 |
| N-5 | schema Path 类型 | `config.schema.json` 的 `assets.data_root.type == "string"`（评审 R-5） | 断言通过 |
| N-6 | schema 顶层 required | `config.schema.json` 顶层 `required` 含 6 新段名（评审 R-5） | 断言通过 |

## 5. 回退方案

- 任一验收点失败：修复后复验；三方（config.py/yaml/schema）须同步回退。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：全部验收点通过。评审 7 项意见第 1 轮全部处置（含 R-1 协议版本先声明后注册归因）。实施中 ruff 1 处 import 排序自动修复。全量 891 passed + 3 个 pre-existing guard_verifier 失败（同前）。

| 编号 | 结果（通过/失败/部分通过） | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | 6 段默认值断言（explorer/verify/api_surface/assets/batch/report） | - |
| A-2 | 通过 | 嵌套默认值（call_tree.max_depth=8、verify.max_rounds_per_candidate=4） | - |
| A-3 | 通过 | `AI_APK_TRACER_EXPLORER__MAX_ROUNDS_PER_ENTRY=6`/`VERIFY__ENABLED=true`/`BATCH__MAX_AI_CALLS=25` 覆盖生效 | - |
| A-4 | 通过 | `get_settings()` 加载 6 段；基线 `context_budget.max_output_tokens==8000`（以 default.yaml 为准） | - |
| A-5 | 通过 | schema 顶层 required 含 6 段名；`assets.data_root.type=="string"`；关键字段默认值 | - |
| A-6 | 通过 | `BatchSettings(max_ai_calls=0, max_wall_seconds=0)` 通过 | - |
| A-7 | 通过 | test_config.py 13 项全过 | - |
| A-8 | 通过 | 全量 pytest：891 passed + 3 pre-existing | - |
| A-9 | 通过 | check-all：891 passed + 3 pre-existing；ruff 全过 | - |
| N-1 | 通过 | `Settings(explorer_bogus=...)` 忽略（extra="ignore"） | - |
| N-2 | 通过 | `max_ai_calls=-1` → ValidationError；正向缺省 ==0 | - |
| N-3 | 通过 | `call_tree.max_depth=0` → ValidationError（ge=1） | - |
| N-4 | 通过 | 旧 yaml 缺新段 → 模型默认值（get_settings 不经 schema 校验） | - |
| N-5 | 通过 | schema `assets.data_root.type == "string"` | - |
| N-6 | 通过 | schema 顶层 required 含 6 段名 | - |
