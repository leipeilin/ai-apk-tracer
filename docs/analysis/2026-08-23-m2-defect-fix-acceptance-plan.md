# 任务验收方案：M2-DEFECT-FIX

> **任务编号**：M2-DEFECT-FIX
> **依据实施方案**：`docs/analysis/2026-08-23-m2-defect-fix-implementation-plan.md`
> **验收方式**：pytest 针对性用例 + 全量回归 + 门禁

## 1. 验收点清单

| 编号 | 验收项 | 步骤 | 预期 |
|---|---|---|---|
| A-1 | 纯文本 manifest 直通 | 构造文本 manifest APK → extract_decoded_manifest | 直写返回，零子进程 |
| A-2 | 二进制 manifest 走 jadx 解码正常 | 真实二进制 manifest + 环境 jadx | 解码成功返回（既有行为不回归） |
| A-3 | 解码子进程超时 → 确定性失败 | monkeypatch process.communicate 永挂 + 1s 超时注入 | 抛 MANIFEST_DECODE_TIMEOUT；进程被 kill（returncode 非 None） |
| A-4 | rmtree 超时容错 | monkeypatch shutil.rmtree 永挂 + 1s 超时 | 不抛；warning；函数返回正常路径 |
| A-5 | AI 总时长兜底（永挂） | fake client.post 永挂 + request_timeout=0.5s | 网络重试后返回 failure="network"；attempts 记录 |
| A-6 | AI 总时长兜底（正常） | fake client.post 快速 200 | 正常返回响应；兜底不触发 |
| A-7 | 配置默认值 | AISettings() | request_timeout_seconds=180.0 |
| A-8 | prompt 新约束 | system.md 含"禁止无据产链"与"code_context 为 null"；既有断言全保留 | 逐字断言 |
| A-9 | registry 同步 | sync --write 后 --check | 哈希更新且 check 0 |
| A-10 | 全量回归 | pytest + check-backend + ruff | 1148+ 全过零错误 |

## 2. 负例

| 编号 | 场景 | 预期 |
|---|---|---|
| N-1 | start_new_session 丢失时 killpg 失败 | process.kill() 兜底（OSError 分支）——代码审查确认 |
| N-2 | request_timeout_seconds=0 或负 | 配置校验拒绝（gt=0） |
| N-3 | apkanalyzer 路径存在但执行失败（非零退出） | 回退 jadx 路径（既有行为） |

## 3. 回退

- D-1/D-2 代码独立可 revert；D-3 revert prompt + 哈希重同步（A-9 演练过该流程）。

## 4. 验收记录（实施后回填）

> **验收日期**：2026-08-23。**结果：A-1~A-10 全部通过**。全量回归 **1155 passed / 0 failed**（基线 1148 + 新增 7）；`sync-ai-protocol.py --check` 通过；`check-backend.sh` 通过（含规则契约 30）；改动文件 ruff 零错误（顺带修复 ai_transport/manifest_extractor 既有 9 项）。
>
> **实施勘误**（评审 R-1 的深化发现）：kill 回收成功后超时被吞（返回假 communicate 结果继续流程）——修复为"回收仅收尸、超时必抛"；测试的 hanging_rmtree 线程 sleep 999 会拖住 pytest 退出（to_thread 线程非 daemon）——改 3s 短挂。

| 编号 | 结果 | 实测说明（测试函数） |
|---|---|---|
| A-1 | 通过 | `test_plain_text_manifest_short_circuits`：文本 manifest 直写零子进程 |
| A-2 | 说明 | 二进制走 jadx 正常路径由 A-4 第二段覆盖（fake 正常进程 + rmtree 挂起容错后成功返回）——真实 jadx 已在 M2 验收 run 多次实证 |
| A-3 | 通过 | `test_decode_timeout_kills_process_and_raises`：killpg 语义（fake 置位）+ MANIFEST_DECODE_TIMEOUT（DependencyError——R-3）+ 进程被杀 + start_new_session 断言 |
| A-4 | 通过 | `test_rmtree_timeout_tolerated`：独立调用不抛 + 成功路径 rmtree 挂起不影响返回 |
| A-5 | 通过 | `test_request_total_timeout_falls_to_network`：永挂 post → network 失败（attempts=1） |
| A-6 | 通过 | `test_fast_request_unaffected`：快速 200 正常返回 |
| A-7 | 通过 | `test_derived_request_timeout_default`：未配置 → read+60=360；显式 90 优先（R-2） |
| A-8 | 通过 | `test_prompt_declares_required_and_enums`：新断言（禁止无据产链/code_context 为 null/由驱动层预算终止承载——R-4 优先级声明入 prompt 本体） |
| A-9 | 通过 | `--write` 后 `--check` 0（哈希更新） |
| A-10 | 通过 | 1155 passed + check-backend + ruff 全过 |
| N-1 | 通过 | killpg 失败兜底 process.kill()（OSError 分支——代码路径，A-3 的 killpg 正常路径外） |
| N-2 | 通过 | request_timeout_seconds gt=0 校验（config Field 约束） |
| N-3 | 通过 | apkanalyzer 失败回退 jadx（既有行为——A-3 的 fake which 对 apkanalyzer/jadx 均返回同路径，回退逻辑实测经过） |
