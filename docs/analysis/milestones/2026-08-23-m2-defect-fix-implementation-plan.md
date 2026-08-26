# 任务实施方案：M2-DEFECT-FIX（验收发现三缺陷修复）

> **任务编号**：M2-DEFECT-FIX
> **日期**：2026-08-23
> **依据**：`docs/analysis/milestones/2026-08-23-m2-acceptance-runs.md` §4（验收发现新缺陷 4/5/1 三项移交）；M2 实施审查处置记录 §6
> **状态**：已闭合（评审 R-1~R-9 全部采纳，见 `2026-08-23-m2-defect-fix-review.md`——kill 回收二次兜底/默认值动态派生/DependencyError 分类/约束优先级/嵌套结构保证/可选探针/default.yaml 注释/编号统一/措辞精度）
> **前置依赖**：d2f6ed3（PROMPT-FIX）、ea332ee（M2-ACCEPTANCE-CLOSURE）

---

## 1. 任务目标与范围

修复 M2 验收发现的三项缺陷：

| # | 缺陷（验收记录 §4） | 根因 | 修复 |
|---|---|---|---|
| D-1 | `extract_decoded_manifest` 的 `process.communicate()`（两处）与 `shutil.rmtree(.manifest-decode)`（万级文件）均无超时——大 APK 可无限阻塞 run（且 rmtree 同步阻塞事件循环） | manifest_extractor.py:37/62/51/65/72 | communicate 包 `asyncio.wait_for`（超时 kill 进程）；rmtree 走 `asyncio.to_thread` + `wait_for` 总时长 |
| D-2 | AI 调用偶发长挂起（httpx read_timeout=120s 疑似被中间层 keepalive 重置未触发——实测 15 分钟连接无数据未断） | ai_transport.py:109 单次 post 仅依赖 httpx 分项超时 | 单次 HTTP 尝试包 `asyncio.wait_for` 总时长兜底（新配置 `request_timeout_seconds`，默认 = read_timeout+60）；超时归入可重试 network 路径 |
| D-3 | 模型首轮无 code_context 直接产 chain_proposals（hops 编造 → 跳回查必然失败 → validated=0） | prompt 约束不足（硬约束 4"必须来自已见过的上下文"未被遵守） | system.md 硬约束新增：**无 code_context 时禁止输出 chain_proposals**（首轮只输出 component_summary + done=false + read_requests——先读码后产链）；与既有约束 5（done=true 须伴随链）自洽组合 |

**范围（in scope）**：
1. `backend/app/analysis/manifest_extractor.py`——超时保护（D-1）；
2. `backend/app/analysis/ai_transport.py` + `backend/app/config.py`——总时长兜底（D-2）；
3. `prompts/explorer/1.0.0/system.md` + registry 哈希同步——prompt 硬约束（D-3）；
4. 测试：三缺陷各补针对性用例（超时路径/wait_for 兜底/prompt 断言）。

**非范围（out of scope）**：
- 核验（verify）prompt 的同类合规率问题（验收记录 §4②——改动面大，独立任务按 M4 prompt 迭代处理）；
- rmtree 改增量删除/后台清理队列（to_thread + wait_for 已消除阻塞——足够）；
- IPv4 强制 / VPN 层问题（环境层已规避）。

## 2. 现状锚点

- **D-1**：`manifest_extractor.py` 全文 73 行（已通读）；jadx/apkanalyzer 子进程两处 `communicate()` 无 wait_for；三处 `shutil.rmtree(decode_dir)`（:51 同步前置清理 / :65 失败清理 / :72 成功清理）——同步调用直接阻塞事件循环（万级文件时挂起整个 run 的事件循环，是"rmtree 卡死"的直接机制）。JadxAdapter（decompiler.py:96-113）已有 `wait_for(communicate(), 600)` + `start_new_session=True` + kill 进程树先例可对齐。
- **D-2**：`ai_transport.py:109` `response = await self.client.post(url, ...)`——httpx.Timeout 四分项（connect 10/read 120/write 30/pool 10）构造于 :58-66；重试循环 :93-130 的 `except httpx.HTTPError` 捕获网络错误（`asyncio.TimeoutError` 不在其中——wait_for 兜底需并入该分支语义）。AISettings（config.py:65-77）已有 timeout 字段族。
- **D-3**：system.md（PROMPT-FIX 后版本，74 行）硬约束 9 条 + 输出契约；`test_prompt_declares_required_and_enums`（test_explorer_protocol.py:178-220）断言 token 清单——新增约束需同步断言；registry 哈希经 `scripts/sync-ai-protocol.py --write` 同步。
- **测试基建**：manifest_extractor 无直接单测（新写）；transport 测试经 runtime 间接（新写直测用 fake client）；prompt 断言在 test_explorer_protocol.py。

## 3. 详细实现方案

### 3.1 D-1：manifest_extractor 超时保护

```python
_MANIFEST_DECODE_TIMEOUT_SECONDS = 120  # 单次解码子进程墙钟（manifest 远小于全量反编译 600s）
_RMTREE_TIMEOUT_SECONDS = 60            # 万级文件目录清理墙钟

async def _communicate_with_timeout(process, timeout: float) -> tuple[bytes, bytes]:
    """communicate + 墙钟兜底：超时 kill 进程树（对齐 JadxAdapter 模式）。"""
    try:
        return await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)  # 需 start_new_session=True
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        await process.communicate()  # 回收
        raise ValidationError(f"Manifest 解码超时（>{timeout:.0f}s）", "MANIFEST_DECODE_TIMEOUT")
```

- 两处 `create_subprocess_exec` 加 `start_new_session=True`（killpg 可达派生子进程——jadx/apkanalyzer 均为 Java 多进程）；
- 两处 `await process.communicate()` → `await _communicate_with_timeout(process, _MANIFEST_DECODE_TIMEOUT_SECONDS)`；
- 三处 `shutil.rmtree(...)` → `await _rmtree_with_timeout(decode_dir)`：

```python
async def _rmtree_with_timeout(path: Path) -> None:
    """rmtree 不阻塞事件循环（to_thread）+ 墙钟兜底（超时放弃残留——下次 run 前置清理兜底）。"""
    try:
        await asyncio.wait_for(asyncio.to_thread(shutil.rmtree, path, True), _RMTREE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        LOGGER.warning("manifest 解码目录清理超时（残留 %s——下次运行前置清理）", path)
```

注意：:51 前置清理超时残留不视为错误（继续解码——jadx -d 覆盖写）；:65/:72 清理超时同理（warning 即可）。

### 3.2 D-2：AI 调用总时长兜底

**config.py AISettings 新增**：

```python
request_timeout_seconds: float = Field(
    default=180.0, gt=0, le=3600,
    description="单次模型 HTTP 请求总时长兜底（墙钟）——防御中间层 keepalive 重置分项超时的长挂起；超时归入可重试 network 失败",
)
```

**ai_transport.py post_chat_completions 单次尝试改造**：

```python
attempts += 1
request_timeout = float(getattr(self.settings, "request_timeout_seconds", 180.0))
try:
    response = await asyncio.wait_for(
        self.client.post(url, headers=headers, json=payload), request_timeout)
except asyncio.TimeoutError:
    response = None  # 归入下方重试判定路径
if response is None:  # 总时长兜底触发——按网络错误重试
    if attempt + 1 < max_attempts:
        await self.retry_backoff(None, attempt)
        continue
    return AITransportResult(None, attempts, "network")
```

- 不并入 `except httpx.HTTPError`（wait_for 取消后 client.post 内部可能抛 CancelledError 语义混淆——独立分支最直白）；
- failure="network" 复用既有分类（调用方 `_transport_failure_details("network")` 已有处理路径——可重试/降级语义不变）。

### 3.3 D-3：探索 prompt 无上下文禁产链

**system.md 硬约束新增第 10 条**（插在约束 9 之后）：

```markdown
10. 禁止无据产链：输入的 code_context 为 null（尚未读码）时，禁止输出 chain_proposals——此时只输出 component_summary、loop.done=false 与 read_requests（先通过读码获取真实方法 ID 与调用关系，再在后续轮构造链）。chain_proposals 中的每个 method_id 都必须出现在已见过的 code_context 或 entry_json 中。
```

- 与约束 5 自洽：无上下文 → 禁链 → done 必须 false（约束 5 的逆否）；
- 与约束 4 强化呼应（4 是"引用真实"，10 是"没读码就不许产"——把质量门禁前移到生成时）。
- `scripts/sync-ai-protocol.py --write` 同步哈希；`test_prompt_declares_required_and_enums` 增断言 token（"禁止无据产链"/"code_context 为 null"）。

### 3.4 文件变更清单

| 文件 | 变更 | 内容 |
|---|---|---|
| `backend/app/analysis/manifest_extractor.py` | 修改 | 两处 communicate 加 wait_for+killpg；三处 rmtree 加 to_thread+wait_for；start_new_session |
| `backend/app/analysis/ai_transport.py` | 修改 | 单次 post 包 wait_for 总时长（TimeoutError → network 重试路径） |
| `backend/app/config.py` | 修改 | AISettings.request_timeout_seconds |
| `prompts/explorer/1.0.0/system.md` | 修改 | 硬约束 10（无据产链禁令） |
| `prompts/registry.yaml` | 修改 | 哈希同步（--write） |
| `backend/tests/test_manifest_extractor.py` | 新增 | D-1 用例（正常 XML/解码超时路径/rmtree 容错） |
| `backend/tests/test_ai_transport.py` | 新增 | D-2 用例（fake client 永挂 → wait_for 兜底 → network 失败；短请求正常通过） |
| `backend/tests/test_explorer_protocol.py` | 修改 | D-3 断言 token |

### 3.5 风险与回退

| 风险 | 对策 | 回退 |
|---|---|---|
| R-1 wait_for 包 client.post 取消时 httpx 内部状态异常 | httpx 官方支持请求取消（aclose 语义安全）；D-2 用例覆盖取消后复用 client | request_timeout_seconds 设超大值即禁用兜底 |
| R-2 D-3 约束导致模型过度保守（有上下文也不敢产链） | 措辞限定"code_context 为 null 时"（有上下文不受限）；探针冒烟可快速验证 | revert prompt + 哈希重同步 |
| R-3 killpg 在非 POSIX 平台 | 项目运行环境为 macOS/Linux（uvicorn 部署）；OSError 兜底 process.kill() | — |
| R-4 rmtree 超时残留目录累积 | 下次 run 的 :51 前置清理兜底 + warning 可审计 | — |

## 4. 验收方案（摘要）

| 编号 | 验收项 | 方式 |
|---|---|---|
| A-1 | 纯文本 manifest 直通不受影响 | 既有行为用例 |
| A-2 | 子进程解码超时 → MANIFEST_DECODE_TIMEOUT + 进程被 kill | monkeypatch 假 process.communicate 永挂 + 小超时 |
| A-3 | rmtree 慢/超时 → warning 不抛、run 继续 | monkeypatch rmtree 永挂 + 小超时 |
| A-4 | transport 总时长兜底：永挂 post → network 失败（重试后） | fake client |
| A-5 | 正常请求不受兜底影响 | fake client 快速返回 |
| A-6 | prompt 含新约束 token + registry 哈希同步 + --check 0 | 既有断言扩展 |
| A-7 | 全量回归 + check-backend + ruff | 门禁 |

## 5. 依赖与交接

- 修复后 M2 验收记录 §4 处置状态可更新（4/5 已修复、1 部分（探索侧）；核验侧 prompt 迭代仍留 M4）。
