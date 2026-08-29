"""在受限子进程中发现并执行内置静态分析规则。"""

from __future__ import annotations

import json
import multiprocessing
import os
import platform
import resource
import shutil
import signal
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import yaml

from app.analysis.coverage_domain import coverage_domain_from_facts
from app.analysis.index_store import SCHEMA_VERSION
from app.shared.errors import ValidationError

# 规则产物键白名单（T2.1）：键 → 产物文件内的记录键名（T0.4 schema 顶层键）。
RULE_ARTIFACT_KEYS = ("binder_bindings", "receiver_registrations", "webview_js_bridges")
RULE_ARTIFACT_ENTRY_KEY = {
    "binder_bindings": "bindings",
    "receiver_registrations": "registrations",
    "webview_js_bridges": "bridges",
}
# schema 懒加载缓存（模块级——多 run 复用）
_ARTIFACT_SCHEMAS: dict[str, Any] = {}


def _load_artifact_schema(schemas_root: Path, name: str) -> Any:
    """加载并缓存产物 schema（jsonschema validator 构造有成本）。"""

    if name not in _ARTIFACT_SCHEMAS:
        import jsonschema

        with (schemas_root / f"{name}.schema.json").open(encoding="utf-8") as fp:
            schema = json.load(fp)
        _ARTIFACT_SCHEMAS[name] = jsonschema.validators.validator_for(schema)(schema)
    return _ARTIFACT_SCHEMAS[name]


class RuleRunner:
    """发现可信内置规则，并以有界并发在资源受限的独立进程中执行。"""

    protocol_version = "1.0.0"

    def __init__(self, rules_root: Path, settings: Any):
        """绑定内置规则根目录和单规则资源限制。"""

        self.rules_root = rules_root.resolve()
        self.settings = settings
        self.last_coverage_gaps: list[dict[str, Any]] = []
        # 最近一次 run_all 导出的规则产物清单（对齐 last_coverage_gaps 模式，
        # orchestrator 消费后注册进 run_manifest.artifacts——T2.1）
        self.last_artifacts: list[dict[str, Any]] = []

    def discover(self) -> list[dict[str, Any]]:
        """发现规则根目录内声明为内置且入口安全的规则。"""

        rules = []
        for metadata_path in sorted(self.rules_root.glob("*/*/rule.yaml")):
            if self.rules_root not in metadata_path.resolve().parents:
                continue
            metadata = yaml.safe_load(metadata_path.read_text("utf-8"))
            entry = metadata_path.parent / "detect.py"
            if not entry.is_file() or entry.is_symlink():
                continue
            if metadata.get("builtin") is not True:
                continue
            rules.append({"metadata": metadata, "entry": entry.resolve()})
        return rules

    def run_all(self, run_dir: Path, payload: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        """有界并发执行可信规则，并按 rule_id 稳定汇总候选与失败记录。

        使用 spawn 进程池而非线程，避免并发 ``Popen(preexec_fn=...)`` 的 fork 锁风险；
        每个池进程仍只启动一个受原有资源限制约束的规则子进程。
        """

        if sys.version_info[:2] != (3, 12):
            raise ValidationError(
                f"规则运行时要求 Python 3.12，当前为 {platform.python_version()}",
                "RULE_PYTHON_VERSION_MISMATCH",
            )
        self._validate_index_reference(run_dir, payload)
        self.last_coverage_gaps = []
        candidates: list[dict] = []
        failures: list[dict] = []
        self.last_artifacts = []
        rules = sorted(self.discover(), key=lambda rule: str(rule["metadata"]["id"]))
        max_workers = (
            min(max(1, int(getattr(self.settings, "max_concurrency", 2))), len(rules))
            if rules else 0
        )
        if max_workers <= 1:
            # 运行反馈（track-progress-console）：逐条完成即落盘——详情页进度
            # 计数（progress.processed）依赖 rule-results 文件实时可见
            results = []
            for rule in rules:
                result = self._run_one(run_dir, rule, payload)
                self._persist_result(run_dir, rule, result)
                results.append(result)
        else:
            settings = {
                name: getattr(self.settings, name)
                for name in (
                    "wall_timeout_seconds", "cpu_timeout_seconds", "memory_mb",
                    "stdout_max_mb", "stderr_max_mb", "workdir_max_mb",
                )
            }
            indexed_results: list[tuple[int, dict]] = []
            with ProcessPoolExecutor(
                max_workers=max_workers,
                mp_context=multiprocessing.get_context("spawn"),
            ) as executor:
                # as_completed 逐条回收：每条完成即落盘（运行反馈实时可见）；
                # 索引排序还原 rules 原序——candidates/failures 聚合顺序与
                # executor.map 版本一致（异常传播语义亦同：_run_one 已归一
                # 全部预期失败，仅资源类 OSError 可能抛出）
                future_to_index = {
                    executor.submit(
                        _run_rule_worker,
                        self.rules_root.as_posix(),
                        settings,
                        run_dir.as_posix(),
                        rule,
                        payload,
                    ): index
                    for index, rule in enumerate(rules)
                }
                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    result = future.result()
                    indexed_results.append((index, result))
                    self._persist_result(run_dir, rules[index], result)
            results = [result for _, result in sorted(indexed_results, key=lambda pair: pair[0])]
        for rule, result in zip(rules, results):
            if result["status"] == "completed":
                self._export_rule_artifacts(run_dir, result)
                candidates.extend(result["candidates"])
                for diagnostic in result.get("component_diagnostics", []):
                    critical_gaps = [
                        gap for gap in diagnostic.get("gaps", [])
                        if not isinstance(gap, dict) or gap.get("critical", True)
                    ]
                    if diagnostic.get("status") != "completed" or critical_gaps:
                        self.last_coverage_gaps.append({
                            "code": "RULE_COMPONENT_PARTIAL",
                            "critical": True,
                            "rule_id": result.get("rule_id"),
                            "component_name": diagnostic.get("component_name"),
                            "status": diagnostic.get("status"),
                            "duration_ms": diagnostic.get("duration_ms"),
                            "gaps": critical_gaps,
                            "coverage_domain": diagnostic.get("coverage_domain"),
                        })
            else:
                failures.append(result)
        return candidates, failures

    def _persist_result(self, run_dir: Path, rule: dict, result: dict[str, Any]) -> None:
        """单条规则结果落盘（运行反馈实时可见——track-progress-console）。

        与原 run_all 收尾批量写盘等价，仅提前到该规则完成时点；原子性由
        _write_result（tmp + os.replace）保证。
        """

        result_path = run_dir / "rule-results" / f"{rule['metadata']['id']}.json"
        self._write_result(result_path, result)

    @staticmethod
    def _write_result(result_path: Path, result: dict[str, Any]) -> None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = result_path.with_name(
            f".{result_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                "utf-8",
            )
            os.replace(temporary_path, result_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _export_rule_artifacts(self, run_dir: Path, result: dict[str, Any]) -> None:
        """提取规则产物：jsonschema 校验（T0.4）→ 写 rule-results/{name}.json
        → 记录 last_artifacts（T2.1，§4.11 决断 2：产物 JSON 由规则运行时
        输出、backend 汇总侧落盘——不 import 规则侧代码）。

        per-record 校验粒度（评审 R-3）：单条坏记录剔除 + gap 携带索引与
        摘要，不毒化整产物；产物整体结构错误才整级降级。
        """

        import jsonschema

        artifacts = result.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            return
        schemas_root = self.rules_root.parent / "schemas"
        for name, records in artifacts.items():
            if name not in RULE_ARTIFACT_KEYS or not isinstance(records, list):
                continue
            try:
                validator = _load_artifact_schema(schemas_root, name)
            except FileNotFoundError:
                self.last_coverage_gaps.append({
                    "code": "RULE_ARTIFACT_SCHEMA_MISSING",
                    "critical": False,
                    "artifact": name,
                })
                continue
            entry_key = RULE_ARTIFACT_ENTRY_KEY[name]
            kept: list[dict[str, Any]] = []
            for index, record in enumerate(records):
                try:
                    validator.validate({"schema_version": "1.0.0", entry_key: [record]})
                except jsonschema.ValidationError as exc:
                    self.last_coverage_gaps.append({
                        "code": "RULE_ARTIFACT_RECORD_INVALID",
                        "critical": False,
                        "artifact": name,
                        "record_index": index,
                        "detail": str(exc.message)[:200],
                    })
                    continue
                kept.append(record)
            truncated = any(
                gap.get("code") == "RULE_ARTIFACT_TRUNCATED"
                and gap.get("artifact") == name
                for gap in result.get("artifact_gaps", [])
                if isinstance(gap, dict)
            )
            payload = {
                "schema_version": "1.0.0",
                entry_key: kept,
            }
            self._write_result(run_dir / "rule-results" / f"{name}.json", payload)
            self.last_artifacts.append({
                "type": name,
                "path": f"rule-results/{name}.json",
                "record_count": len(kept),
                "truncated": truncated,
            })

    @staticmethod
    def _validate_index_reference(run_dir: Path, payload: dict[str, Any]) -> None:
        """确认规则共享索引位于当前任务 index 目录且不是软链接。"""

        descriptor = payload.get("index")
        if not descriptor:
            return
        schema_version = str(descriptor.get("schema_version") or "")
        if schema_version != SCHEMA_VERSION:
            raise ValidationError(
                f"INDEX_SCHEMA_REBUILD_REQUIRED: 规则索引版本必须为 {SCHEMA_VERSION}，当前为 {schema_version or 'missing'}",
                "RULE_INDEX_SCHEMA_UNSUPPORTED",
            )
        database_path = Path(descriptor.get("database_path", ""))
        allowed_root = (run_dir / "index").resolve()
        try:
            resolved = database_path.resolve(strict=True)
        except OSError as exc:
            raise ValidationError("规则共享索引不存在", "RULE_INDEX_NOT_FOUND") from exc
        if database_path.is_symlink() or allowed_root not in resolved.parents or resolved.name != "analysis.sqlite3":
            raise ValidationError("规则共享索引路径不安全", "RULE_INDEX_PATH_UNSAFE")
        if Path(descriptor.get("allowed_index_root", "")).resolve() != allowed_root:
            raise ValidationError("规则共享索引允许根目录不匹配", "RULE_INDEX_ROOT_MISMATCH")

    def _run_one(self, run_dir: Path, rule: dict, payload: dict[str, Any]) -> dict:
        """在隔离工作目录执行单条规则，并将资源或协议错误归一为失败结果。"""

        rule_id = rule["metadata"]["id"]
        workdir = run_dir / "rule-work" / rule_id
        if workdir.is_symlink():
            workdir.unlink()
        elif workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True, mode=0o700)
        input_path = workdir / "input.json"
        input_path.write_text(json.dumps({"protocol_version": self.protocol_version, **payload}, ensure_ascii=False), "utf-8")
        os.chmod(input_path, 0o400)
        stdout_path = workdir / "stdout.json"
        stderr_path = workdir / "stderr.log"
        max_stdout = self.settings.stdout_max_mb * 1024 * 1024
        max_stderr = self.settings.stderr_max_mb * 1024 * 1024
        started = time.monotonic()
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": str(self.rules_root),
        }
        # 规则仅获得受限环境和只读输入；父进程持续监控墙钟、输出与工作目录体积。
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, str(rule["entry"]), str(input_path)],
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                start_new_session=True,
                preexec_fn=self._limits(max(max_stdout, max_stderr)),
            )
            failure = None
            while process.poll() is None:
                elapsed = time.monotonic() - started
                stdout_size = stdout_path.stat().st_size
                stderr_size = stderr_path.stat().st_size
                workdir_size = sum(path.stat().st_size for path in workdir.rglob("*") if path.is_file() and not path.is_symlink())
                if elapsed > self.settings.wall_timeout_seconds:
                    failure = ("RULE_TIMEOUT", "规则执行超过墙钟限制")
                elif stdout_size > max_stdout or stderr_size > max_stderr:
                    failure = ("RULE_OUTPUT_LIMIT", "规则 stdout/stderr 超过 10MiB 限制")
                elif workdir_size > self.settings.workdir_max_mb * 1024 * 1024:
                    failure = ("RULE_WORKDIR_LIMIT", "规则工作目录超过限制")
                if failure:
                    self._kill_group(process)
                    break
                time.sleep(0.02)
            return_code = process.wait()
        duration_ms = round((time.monotonic() - started) * 1000)
        if failure:
            return self._failure(rule_id, failure[0], failure[1], duration_ms)
        if stdout_path.stat().st_size > max_stdout or stderr_path.stat().st_size > max_stderr:
            return self._failure(rule_id, "RULE_OUTPUT_LIMIT", "规则输出超过限制", duration_ms)
        if return_code != 0:
            diagnostic = stderr_path.read_text("utf-8", errors="replace")[-2000:]
            return self._failure(rule_id, "RULE_NONZERO_EXIT", f"退出码 {return_code}: {diagnostic}", duration_ms)
        try:
            output = json.loads(stdout_path.read_text("utf-8"))
            self._validate_output(rule_id, output)
            self._normalize_component_diagnostics(rule_id, output)
        except (json.JSONDecodeError, ValidationError) as exc:
            return self._failure(rule_id, "RULE_PROTOCOL_ERROR", str(exc), duration_ms)
        output["duration_ms"] = duration_ms
        output["runtime"] = {
            "python": platform.python_version(),
            "executable": sys.executable,
            "cpu_memory_enforcement": "best_effort",
            "process_group_kill": True,
            "input_bytes": input_path.stat().st_size,
            "shared_index": bool(payload.get("index")),
        }
        return output

    def _limits(self, file_size: int):
        cpu = self.settings.cpu_timeout_seconds
        memory = self.settings.memory_mb * 1024 * 1024

        def apply_limits() -> None:
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
                resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
            except (ValueError, OSError):
                pass

        return apply_limits

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _validate_output(rule_id: str, output: Any) -> None:
        if not isinstance(output, dict) or output.get("protocol_version") != "1.0.0":
            raise ValidationError("规则输出协议版本错误", "RULE_PROTOCOL_ERROR")
        if output.get("rule_id") != rule_id or output.get("status") != "completed":
            raise ValidationError("规则输出身份或状态错误", "RULE_PROTOCOL_ERROR")
        if not isinstance(output.get("candidates"), list):
            raise ValidationError("规则 candidates 必须是数组", "RULE_PROTOCOL_ERROR")
        required = {"rule_id", "rule_version", "component", "evidence_level", "locations"}
        for candidate in output["candidates"]:
            if not isinstance(candidate, dict) or not required.issubset(candidate):
                raise ValidationError("候选字段不完整", "RULE_PROTOCOL_ERROR")
            if candidate["evidence_level"] not in {"L1", "L2"}:
                raise ValidationError("规则只能直接输出 L1/L2", "RULE_PROTOCOL_ERROR")
        diagnostics = output.get("component_diagnostics", [])
        if not isinstance(diagnostics, list) or any(not isinstance(item, dict) for item in diagnostics):
            raise ValidationError("规则 component_diagnostics 必须是对象数组", "RULE_PROTOCOL_ERROR")
        if any(not isinstance(item.get("gaps", []), list) for item in diagnostics):
            raise ValidationError("组件诊断 gaps 必须是数组", "RULE_PROTOCOL_ERROR")
        # 规则产物协议校验（T2.1）：白名单键 + 数组值（宽松——深度校验由
        # 汇总侧 jsonschema 做，见 _export_rule_artifacts）
        artifacts = output.get("artifacts")
        if artifacts is not None and (
            not isinstance(artifacts, dict)
            or any(key not in RULE_ARTIFACT_KEYS or not isinstance(value, list)
                   for key, value in artifacts.items())
        ):
            raise ValidationError("规则 artifacts 必须是白名单键的数组字典", "RULE_PROTOCOL_ERROR")
        artifact_gaps = output.get("artifact_gaps")
        if artifact_gaps is not None and (
            not isinstance(artifact_gaps, list)
            or any(not isinstance(item, dict) for item in artifact_gaps)
        ):
            raise ValidationError("规则 artifact_gaps 必须是对象数组", "RULE_PROTOCOL_ERROR")

    @staticmethod
    def _normalize_component_diagnostics(rule_id: str, output: dict[str, Any]) -> None:
        """Attach fail-closed domains derived from each diagnostic's concrete facts."""

        for diagnostic in output.get("component_diagnostics", []):
            component_name = diagnostic.get("component_name")
            critical_gaps = [
                gap for gap in diagnostic.get("gaps", [])
                if not isinstance(gap, Mapping) or gap.get("critical", True)
            ]
            diagnostic["coverage_domain"] = RuleRunner._component_domain(
                rule_id,
                component_name,
                diagnostic,
                critical=diagnostic.get("status") != "completed" or bool(critical_gaps),
                provenance=[{"source": "rule_output", "fact": "component_diagnostic"}],
            )
            for gap in diagnostic.get("gaps", []):
                if isinstance(gap, dict):
                    gap["coverage_domain"] = RuleRunner._component_domain(
                        rule_id,
                        component_name,
                        gap,
                        critical=gap.get("critical", True),
                        provenance=[{"source": "rule_output", "fact": "component_gap"}],
                    )

    @staticmethod
    def _component_domain(
        rule_id: str,
        component_name: Any,
        value: Mapping[str, Any],
        *,
        critical: Any,
        provenance: Any,
    ) -> dict[str, Any] | None:
        existing = value.get("coverage_domain")
        existing = existing if isinstance(existing, Mapping) else {}
        path_present = "path" in value or "path" in existing
        path = value.get("path") if "path" in value else existing.get("path")
        if path_present and (not isinstance(path, str) or not path.strip()):
            return None
        if not path_present and (not isinstance(component_name, str) or not component_name.strip()):
            return None
        operation = value.get("operation") if "operation" in value else existing.get("operation")
        retained_provenance = (
            existing.get("provenance")
            if "provenance" in existing
            else value.get("provenance", provenance)
        )
        retained_critical = existing.get("critical", critical) if "critical" not in value else critical
        return coverage_domain_from_facts(
            rule_id=rule_id,
            component_name=component_name,
            path=path,
            operation=operation,
            critical=retained_critical,
            provenance=retained_provenance,
        )

    @staticmethod
    def _failure(rule_id: str, code: str, message: str, duration_ms: int) -> dict:
        return {
            "protocol_version": "1.0.0",
            "rule_id": rule_id,
            "status": "failed",
            "error": {"code": code, "message": message},
            "duration_ms": duration_ms,
            "candidates": [],
            "coverage_domain": coverage_domain_from_facts(
                rule_id=rule_id,
                critical=True,
                provenance=[{"source": "rule_runner", "fact": "rule_id"}],
            ),
        }


def _run_rule_worker(
    rules_root: str,
    settings: dict[str, Any],
    run_dir: str,
    rule: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """在 spawn 池进程中执行一条规则，确保 DataFlowAnalyzer 等状态不跨规则共享。"""

    runner = RuleRunner(Path(rules_root), SimpleNamespace(**settings))
    return runner._run_one(Path(run_dir), rule, payload)
