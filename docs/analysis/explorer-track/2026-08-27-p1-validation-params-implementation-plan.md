# 任务实施方案：探索参数验证放开（P-1——验证阶段无偏数据采集）

> **任务编号**：P-1（非 gap-fix F 系列——验证阶段参数策略调整，用户指令 2026-08-27）
> **背景**：前期验证阶段三参数（候选上限/上下文封顶/读超时）均为无数据支撑的假设值，
> 造成评估数据截断偏差（278 入口仅探索 131——147 入口零信息）与后段轮失败率爬升
> （round 3/4 失败率 23%/29%，机制：上下文增长 × pro 模型推理 × 120s 超时）。
> **状态**：起草（待批准后实施）

## 1. 目标与范围

验证阶段放开探索参数，采集无偏数据后再回归定参。三项改动 + 一项缺陷修复：

| 参数 | 现值 | 验证值 | 改动性质 |
|---|---|---|---|
| `max_candidates_per_run` | 50 | **无上限（None）** | 新增 None 语义（显式配置） |
| `_MAX_EXPLORE_CONTEXT_CHARS` | 9500 | 40000 | 放宽 + **截断方向反转**（缺陷修复） |
| `read_timeout_seconds` | 120 | 240 | 值放宽（安全边界保留——防长挂起） |

**非范围**：入口数预算（278 全入口本就遍历）；`max_rounds_per_entry`/`max_requests_per_entry`（4 轮/20 请求——先保留，待全量数据）；funnel/L2 改动。

## 2. 详细方案

### 2.1 `max_candidates_per_run` 支持 None（无上限）

- **config.py:204**：`int | None = Field(default=50, ge=1)`——`None` = 无上限；`description` 更新（"验证阶段可设 null 采集无截断数据"）；默认值保持 50（未显式配置不受影响）；
- **explorer.py:179**：`if self._settings.max_candidates_per_run is not None and len(candidates) >= self._settings.max_candidates_per_run: break`——None 短路；
- **config/default.yaml**：`max_candidates_per_run: null`（验证阶段部署值）；
- F4 的入口覆盖透明化统计不受影响（skipped 计数在 break 后自然为 0）。

### 2.2 code_context 封顶 9500→40000 + 截断方向反转（保后切前）

```python
# explorer.py:232-236 现状（缺陷：保前切后——第 4 轮模型看到第 1 轮老代码，
# 最新读码结果被截掉，与轮循环"逐步聚焦"设计意图相反）
joined_context = joined_context[:9500] + "…(earlier context retained, later context truncated)"
# 改为（保后切前——最近上下文优先）
joined_context = "…(earlier context truncated)\n" + joined_context[-40000:]
```

- `attack_surface_json` 截断（explorer.py:243-245）**保持前缀截断不变**——静态能力事实无时序语义，且不随轮变化；
- 40000 字符 ≈ 10-13K token——flash 模型窗口内，单次调用成本上升可接受（换数据）。

### 2.3 `read_timeout_seconds` 120→240

- **config.py:71** Field 默认值 + **config/default.yaml:122** 同步改 240；
- write/connect/pool 超时不动（30/10/10s——40K 字符 body 写入 30s 充足）；
- 超时类失败重试仍保留 1 次（不本任务改——见 todo T4）。

## 3. 风险

1. **AI 成本与时长上升**：278 入口全探索 ≈ 800-1000 次 AI 调用（1-2 小时）+ 40K 上下文的 token 成本——验证阶段明确接受（这是买数据）；
2. **funnel/L2 输入量增大**：无上限候选全部进 funnel（本地确定性秒级——无风险）；L2 复核量随 validated/partial 量上升（`auto_promote` 默认 false——走 L2，成本可控）；
3. **无上限的极端形态**：单 run 候选数 = Σ入口候选（上界 278×4 轮产出）——量级数百，observations/candidates.json 体量 MB 级——可接受；
4. **240s 仍不够**：若全量 run 后段轮失败率再现 → 数据到手（todo T1 回归定参 + T4 重试策略修复——不再盲调）。

## 4. 验收

| 编号 | 验收项 | 方式 | 预期 |
|---|---|---|---|
| P1-1 | None 无上限语义 | 单测（FakeAnalyzer 多候选场景） | `max_candidates_per_run=None` 时 50+ 候选不 break、入口全探索 |
| P1-2 | 截断方向反转 | 单测 | 超 40000 时保留尾部（最新）+ 前缀截断标记；attack_surface 截断保持前缀（不回归） |
| P1-3 | 配置加载 | 单测（yaml null → None） | default.yaml 生效；未配置时默认 50 兼容 |
| P1-4 | 零回归 | 全量 pytest + sync --check | 1271+ 全过 |
| P1-5 | 全量 run 观察点 | `.venv-tls` 跑 shop 全量 | 三分布数据落盘可查：上下文尺寸分布 / 单次调用时长分布 / 各轮失败率分布（P1 数据回归定参依据） |

## 5. 回退

三参数各自独立（config 值 + explorer 判断）——revert 对应块即可；None 语义向后兼容（默认 50 不变）。
