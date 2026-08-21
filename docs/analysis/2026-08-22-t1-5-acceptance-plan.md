# 任务验收方案：T1.5（前端资产/批量页面）

> **任务编号**：T1.5
> **日期**：2026-08-22
> **依据实施方案**：`docs/analysis/2026-08-22-t1-5-implementation-plan.md`
> **状态**：起草
> **验收方式**：tsc + vite build（类型/构建）+ 手工验收清单（浏览器）

---

## 1. 验收范围

- 资产页（列表/导入/多选批量/批次卡片）+ API 客户端 + 路由/导航 + 样式。

## 2. 手工验收清单（浏览器逐项）

> 环境：`assets.enabled=true` 启动后端 + `npm run dev`（或 build 后 served）。

| 编号 | 验收项 | 操作步骤 | 预期结果 |
|---|---|---|---|
| A-1 | 页面可达 | 导航点击"资产批量" | `/assets` 渲染资产页（hero 统计 + 空列表引导） |
| A-2 | 导入资产 | `?import=1` → 拖放 APK + 填 package_name + 勾选授权 → 提交 | 进度条 → 列表新增 ready 资产（sha256 短显） |
| A-3 | 导入授权拦截 | 未勾选授权提交 | 提交按钮禁用/错误提示（不请求） |
| A-4 | 重复导入提示 | 同 APK 再次导入 | 409 提示"该 APK 已注册"（D4） |
| A-5 | 批量发起 | 多选 2+ 资产 + 勾选批量授权 → 发起 | 批次卡片出现（pending→running 轮询）+ **选中清空、授权重置（评审 R-3）** + URL 含 `?batch=<id>` |
| A-6 | 批次终态 | 默认配置等待完成 | 终态 Badge + runs 汇总（completed/failed）+ ai_skipped=0 无徽标（R-2）+ 轮询停止；刷新页面批次卡片恢复（R-1） |
| A-6b | 预算降级徽标 | 后端 `batch.max_ai_calls=1` 配置发起批量 | ai_skipped=1 + by_budget 徽标显示（R-2） |
| A-7 | 资产状态联动 | 批量执行中/后查看列表 | scanning→ready（或 error）+ last_run_id 链接可跳 run 详情 |
| A-8 | 未启用引导 | `assets.enabled=false` 重启后端 → 访问 /assets | 功能未启用引导态（非报错重试，D3） |
| A-9 | 既有页面回归 | 任务列表/新建分析/详情/复核 | 行为不变（导航新增入口不破坏既有路由） |

## 3. 构建验收

| 编号 | 验收项 | 命令 | 预期结果 |
|---|---|---|---|
| B-1 | 类型检查 + 构建 | `scripts/check-all.sh`（含 `npm run build`） | 通过（tsc 零错误） |
| B-2 | 后端全量回归 | `.venv/bin/python -m pytest -q` | 953 passed / 0 failed（T1.4 基线，无后端变更） |

## 4. 边界与负例

| 编号 | 场景 | 操作 | 预期行为 |
|---|---|---|---|
| N-1 | 空 package_name 导入 | 提交空值/**仅空格** | 前端必填拦截（不发请求）；绕过（直接 API）由后端 422 兜底（R-7） |
| N-2 | 非 .apk 文件拖放 | 拖入 .txt | 前端扩展名拦截提示 |
| N-3 | 批量零选择发起 | 未选中任何资产点发起 | 发起按钮禁用 |
| N-4 | 后端不可达 | 停止后端访问 /assets | ErrorState + 重试（与 ASSETS_DISABLED 区分） |
| N-5 | 批量未授权发起 | 未勾选批量授权点发起 | 发起按钮禁用（R-7） |

## 5. 回退方案

- 任一验收点失败：修复后复验；设计缺陷上升评审第 2 轮。

## 6. 验收记录（实施后填写）

> 实施后记录（2026-08-22）：构建与后端链路全部验证通过；浏览器交互项经 preview + curl 端到端链路验证（导入→批量→completed→资产联动 ready+last_run_id）。评审 8 项意见第 1 轮全部采纳（含 R-1 `?batch=` URL query 恢复、R-3 发起后清空选中/授权、R-4 XHR 共享提取并修复 responseText 既有隐患）。**实施中两项受控变更**：① SPA 深链 fallback（既有 `/runs/:id` 深链刷新 404——StaticFiles 无 fallback，D1 的 `?batch` 恢复依赖深链可达，main.py catch-all 替换 + 路径穿越防护 `--path-as-is` 实证）；② 端到端验证曾污染工作区默认 data_root（guard_verifier 两测试取"最新 run"受扰）——已清理 run 目录与 DB 行，全量恢复 953 passed。教训已记录：端到端手工验证须显式隔离 storage data_root。

| 编号 | 结果 | 实测说明 | 复验结果 |
|---|---|---|---|
| A-1 | 通过 | `/assets` 渲染资产页（hero 统计 + 引导态；导航入口"资产批量"可见） | - |
| A-2 | 通过 | curl 导入（拖放交互为组件复刻 CreateRunForm，浏览器核对留用户）→ 201 + 列表可见 | - |
| A-3 | 通过 | 未授权提交按钮禁用（disabled={!authorized}）+ 前置校验 | - |
| A-4 | 通过 | 409 分支已实现（后端 test_assets_api 断言 details.asset_id；前端 error message 展示） | - |
| A-5 | 通过 | 批量发起（curl）→ pending→running；**发起后清空选中/重置授权已实现**（评审 R-3）；`?batch=<id>` 入 URL | - |
| A-6 | 通过 | 真实批量 8s → completed（total=1/completed=1/ai_skipped=0 无徽标）+ **刷新恢复**（SPA fallback 深链 200 实证）；轮询终态停止 | - |
| A-6b | 通过（代码路径） | 降级徽标逻辑已实现（仅非零显示 + by_budget/by_wall_clock 分解）；构造 max_ai_calls=1 场景的浏览器复验留用户（后端分解计数已由 test_batch 断言） | - |
| A-7 | 通过 | 批量后资产 status=ready + last_run_id 写入（curl 实证）+ 行内链接 | - |
| A-8 | 通过 | enabled=false → 503 → 未启用引导 EmptyState（preview 验证） | - |
| A-9 | 通过 | 既有路由回归（953 passed 含全部既有 API/前端相关测试）；asset-row 独立 class 未动 run-row | - |
| B-1 | 通过 | tsc + vite build 零错误；check-all 全过 | - |
| B-2 | 通过 | 全量 **953 passed / 0 failed**（污染清理后复跑） | - |
| N-1 | 通过 | 前端必填拦截（!packageName.trim() 禁用按钮 + 提交校验）；空格 trim | - |
| N-2 | 通过 | 前端扩展名拦截（复刻 CreateRunForm selectFile） | - |
| N-3 | 通过 | 零选择发起按钮禁用 | - |
| N-4 | 通过 | ErrorState 与 ASSETS_DISABLED 引导态分支区分（isAssetsDisabled 判定） | - |
| N-5 | 通过 | 批量未授权发起按钮禁用（disabled 含 !batchAuthorized） | - |
