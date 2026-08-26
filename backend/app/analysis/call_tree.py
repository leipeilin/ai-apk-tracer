"""call_tree on-demand 检索服务（T2.4，方案 §2.2）。

复用 analysis.sqlite3 调用边，提供七检索能力与有界子树构建——服务供
Explorer Agent（T2.5）、核验 Agent（T2.11）与人工分析共用，不预生成
全量调用树。

设计：docs/analysis/explorer-track/2026-08-22-t2-4-implementation-plan.md
（含评审 R-1~R-7 修订：树透传歧义 gaps / body 行预算对齐 240 /
method_id 列值直取硬约束 / lifecycle 解析复用公共函数）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.analysis.api_surface import (
    resolve_component_lifecycle_methods,
)
from app.config import CallTreeSettings

LOGGER = logging.getLogger(__name__)


class CallTreeService:
    """call_tree on-demand 检索服务（全部查询有界；截断显式标注不静默）。

    硬约束（T2.4 评审 R-4）：method_id 一律 `methods.id` 列值直取、禁止
    按格式重建——前缀/类名形态差异由列值直取消除。
    """

    MAX_BODY_LINES = 240  # 评审 R-2：对齐 max_lines_per_context 行预算语义
    MAX_SYMBOL_RESULTS = 50

    def __init__(self, run_dir: Path, reader: Any, settings: CallTreeSettings) -> None:
        self._run_dir = run_dir
        self._reader = reader
        self._settings = settings

    # ------------------------------------------------------------------
    # 入口清单
    # ------------------------------------------------------------------

    def get_entry_points(self) -> list[dict[str, Any]]:
        """API 入口清单（读 T2.2 api_entry_table，附 method_id 解析）。

        降级（入口表缺失/损坏）：返回空列表并附 degraded/hint——其余六
        能力不依赖入口表（评审 R-7：Agent1 改用 search_symbol 起步）。
        """

        path = self._run_dir / "api-surface" / "api_entry_table.json"
        if not path.is_file():
            return [{
                "entry_id": "__degraded__",
                "kind": "none",
                "component_name": "-",
                "source": "none",
                "entry_method": None,
                "method_id": None,
                "degraded": "api_entry_table_missing",
                "hint": "入口表缺失：请用 search_symbol 定位组件类后以 lifecycle 方法为起点",
            }]
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            LOGGER.warning("api_entry_table 读取失败（get_entry_points 降级）")
            return []
        entries = payload.get("api_entries")
        if not isinstance(entries, list):
            LOGGER.warning("api_entry_table 结构不符（api_entries 非列表）")
            return []

        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            result.append({
                "entry_id": entry.get("entry_id"),
                "kind": entry.get("kind"),
                "component_name": entry.get("component_name"),
                "source": entry.get("source"),
                "entry_method": entry.get("entry_method"),
                "method_id": self._entry_method_id(entry),
                # 攻击面事实直取（M2 收尾-3 稳定修复：入口可控性信息曾整体
                # 丢失——模型只能从空方法体猜，是 service/receiver 空转与
                # 产链不稳的信息层根因）
                "exported": entry.get("exported"),
                "exported_reason": entry.get("exported_reason"),
                "permissions": entry.get("permissions"),
                "intent_filters": entry.get("intent_filters"),
                "authorities": entry.get("authorities"),
            })
        return result

    def _entry_method_id(self, entry: dict[str, Any]) -> str | None:
        """入口条目的 method_id 解析（列值直取原则）。

        binder：implementation_method_id 直通（同 id 体系）；manifest/dynrcv：
        组件 lifecycle 方法解析；webview：null（注册调用点以 bridge_line 定位）。
        """

        if entry.get("kind") == "binder":
            return entry.get("implementation_method_id")
        if entry.get("kind") in {"activity", "service", "receiver", "provider"}:
            kind = str(entry.get("kind") or "")
            methods = resolve_component_lifecycle_methods(
                self._reader, str(entry.get("component_name") or ""), kind
            )
            return methods[0]["id"] if methods else None
        return None

    # ------------------------------------------------------------------
    # 方法体
    # ------------------------------------------------------------------

    def get_method_body(self, method_id: str) -> dict[str, Any] | None:
        """方法体查询（行切片；超 MAX_BODY_LINES 截断显式标注）。"""

        row = self._reader.db.execute(
            """SELECT m.name, m.qualified_class, m.descriptor, m.start_line, m.end_line,
                      f.path, f.content
               FROM methods m JOIN files f ON f.id = m.file_id
               WHERE m.id = ?""",
            (method_id,),
        ).fetchone()
        if row is None:
            return None
        lines = str(row["content"] or "").splitlines()
        start = max(int(row["start_line"]) - 1, 0)
        end = min(int(row["end_line"]), len(lines))
        body_lines = lines[start:end]
        truncated = len(body_lines) > self.MAX_BODY_LINES
        if truncated:
            body_lines = body_lines[: self.MAX_BODY_LINES]
        return {
            "method_id": method_id,
            "name": row["name"],
            "qualified_class": row["qualified_class"],
            "descriptor": row["descriptor"],
            "path": row["path"],
            "start_line": int(row["start_line"]),
            "end_line": int(row["end_line"]),
            "body": "\n".join(body_lines),
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # 调用边（callees/callers）
    # ------------------------------------------------------------------

    def get_callees(self, method_id: str) -> dict[str, Any]:
        """直接被调方法（resolved 边）+ 歧义 gaps（D4：歧义如实返回）。"""

        relations = self._reader.get_call_relations_for_methods(
            [method_id], include_callers=False, include_callees=True
        )
        callee_ids = relations["callees"].get(method_id, [])
        return {
            "method_id": method_id,
            "callees": self._method_summaries(callee_ids),
            "gaps": relations["gaps"].get(method_id, []),
        }

    def get_seed_hops(self, method_id: str, limit: int = 8) -> list[dict[str, Any]]:
        """骨架链第一跳（M4-SEED-HOPS 评审 R-1/R-6）：直查 call_sites 的
        resolved 边（含 start_line——三要素确定性，按 start_line 确定序）。

        库不可读/无边 → 空列表降级（探索回到现状行为——结构性回退开关）。
        """

        database_path = self._run_dir / "index" / "analysis.sqlite3"
        connection = None
        try:
            connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
            rows = connection.execute(
                "SELECT start_line, resolved_target_id FROM call_sites "
                "WHERE method_id = ? AND resolve_status = 'resolved' "
                "AND resolved_target_id IS NOT NULL "
                "ORDER BY start_line LIMIT ?",
                (method_id, limit),
            ).fetchall()
        except sqlite3.Error:
            LOGGER.warning("seed hops 查询失败（降级为空骨架）", extra={"method_id": method_id})
            return []
        finally:
            # 审查 R-6：with sqlite3.connect 是事务上下文非关闭——显式释放
            if connection is not None:
                connection.close()
        return [
            {"from_method_id": method_id, "to_method_id": str(target), "call_site_line": line}
            for line, target in rows
            if isinstance(line, int) and line >= 1 and target
        ]

    def get_callers(self, method_id: str) -> dict[str, Any]:
        """直接调用方（resolved 边）。"""
        relations = self._reader.get_call_relations_for_methods(
            [method_id], include_callers=True, include_callees=False
        )
        caller_ids = relations["callers"].get(method_id, [])
        return {"method_id": method_id, "callers": self._method_summaries(caller_ids)}

    def _method_summaries(self, method_ids: list[str]) -> list[dict[str, Any]]:
        if not method_ids:
            return []
        placeholders = ",".join("?" for _ in method_ids)
        rows = self._reader.db.execute(
            f"""SELECT m.id, m.name, m.qualified_class, m.descriptor,
                       m.start_line, f.path
                FROM methods m JOIN files f ON f.id = m.file_id
                WHERE m.id IN ({placeholders})""",
            sorted(set(method_ids)),
        ).fetchall()
        return [
            {
                "method_id": row["id"],
                "name": row["name"],
                "qualified_class": row["qualified_class"],
                "descriptor": row["descriptor"],
                "path": row["path"],
                "line": int(row["start_line"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 符号解析与检索
    # ------------------------------------------------------------------

    def resolve_invoke_target(self, expr: str) -> list[dict[str, Any]]:
        """按方法名（可选 descriptor 限定）解析调用目标候选。

        多候选全返回（歧义是事实——D4）；列值直取（禁止按格式重建 id）。
        """

        parts = expr.strip().split("(", 1)
        name = parts[0].strip()
        if not name:
            return []
        rows = self._reader.db.execute(
            """SELECT m.id, m.name, m.qualified_class, m.descriptor, m.start_line, f.path
               FROM methods m JOIN files f ON f.id = m.file_id
               WHERE m.name = ?
               ORDER BY m.qualified_class, m.descriptor
               LIMIT ?""",
            (name, self.MAX_SYMBOL_RESULTS),
        ).fetchall()
        result = []
        for row in rows:
            if len(parts) > 1 and f"({parts[1]}" not in str(row["descriptor"] or ""):
                continue  # descriptor 限定不匹配
            result.append({
                "method_id": row["id"],
                "name": row["name"],
                "qualified_class": row["qualified_class"],
                "descriptor": row["descriptor"],
                "path": row["path"],
                "line": int(row["start_line"]),
            })
        return result

    def class_hierarchy(self, class_name: str) -> dict[str, Any]:
        """类层次：直接父类/接口 + 直接子类（extends 双形态匹配——Java 源码
        侧简单名 vs smali 侧 FQCN 是 index 事实，评审认可双匹配必要性）。"""

        rows = self._reader.db.execute(
            """SELECT qualified_name, extends_name, implements_json
               FROM classes WHERE qualified_name = ? OR name = ?""",
            (class_name, class_name.rsplit(".", 1)[-1]),
        ).fetchall()
        extends: list[str] = []
        implements: list[str] = []
        for row in rows:
            if row["extends_name"]:
                extends.append(str(row["extends_name"]))
            implements.extend(str(item) for item in json.loads(row["implements_json"]))
        simple = class_name.rsplit(".", 1)[-1]
        subclass_rows = self._reader.db.execute(
            """SELECT qualified_name, name FROM classes
               WHERE extends_name = ? OR extends_name = ?""",
            (class_name, simple),
        ).fetchall()
        subclasses = sorted({
            str(row["qualified_name"] or row["name"]) for row in subclass_rows
        } - {class_name})
        return {
            "class_name": class_name,
            "extends": sorted(set(extends)),
            "implements": sorted(set(implements)),
            "subclasses": subclasses,
        }

    def search_symbol(self, name: str) -> list[dict[str, Any]]:
        """符号搜索（方法名/类名 LIKE 前缀匹配；各有界）。"""

        term = f"{name.strip()}%"
        if term == "%":
            return []
        method_rows = self._reader.db.execute(
            """SELECT m.id, m.name, m.qualified_class, m.descriptor, m.start_line, f.path
               FROM methods m JOIN files f ON f.id = m.file_id
               WHERE m.name LIKE ?
               ORDER BY m.name, m.qualified_class LIMIT ?""",
            (term, self.MAX_SYMBOL_RESULTS),
        ).fetchall()
        class_rows = self._reader.db.execute(
            """SELECT qualified_name, name FROM classes
               WHERE name LIKE ? OR qualified_name LIKE ?
               ORDER BY name LIMIT ?""",
            (term, term, self.MAX_SYMBOL_RESULTS),
        ).fetchall()
        result = [
            {
                "kind": "method",
                "method_id": row["id"],
                "name": row["name"],
                "qualified_class": row["qualified_class"],
                "descriptor": row["descriptor"],
                "path": row["path"],
                "line": int(row["start_line"]),
            }
            for row in method_rows
        ]
        result.extend(
            {"kind": "class", "name": row["name"], "qualified_class": row["qualified_name"]}
            for row in class_rows
        )
        return result

    # ------------------------------------------------------------------
    # 有界子树
    # ------------------------------------------------------------------

    def build_bounded_tree(
        self,
        entry_method_id: str,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        """按入口 BFS 构建有界调用树（callees 方向）。

        gaps 透传（评审 R-1：查询带回的歧义 gap 按节点聚合——树不伪完整）；
        环安全（visited）；edges 端点恒 ⊆ nodes。
        """

        depth_limit = max_depth or self._settings.max_depth
        node_limit = max_nodes or self._settings.max_nodes
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        gaps: dict[str, list[dict[str, Any]]] = {}
        visited: set[str] = {entry_method_id}
        frontier = [entry_method_id]
        truncated: dict[str, Any] | None = None

        for depth in range(depth_limit):
            if not frontier:
                break
            if len(visited) >= node_limit:
                truncated = {"reason": "node_limit", "nodes": len(visited), "depth_reached": depth}
                break
            relations = self._reader.get_call_relations_for_methods(
                frontier, include_callers=False, include_callees=True
            )
            next_frontier: list[str] = []
            for current in frontier:
                for gap in relations["gaps"].get(current, []):
                    gaps.setdefault(current, []).append(gap)
                for callee in relations["callees"].get(current, []):
                    edges.append({"from": current, "to": callee})
                    if callee not in visited:
                        visited.add(callee)
                        next_frontier.append(callee)
                        if len(visited) >= node_limit:
                            truncated = {
                                "reason": "node_limit",
                                "nodes": len(visited),
                                "depth_reached": depth + 1,
                            }
            if truncated:
                break
            frontier = next_frontier
        else:
            if not truncated and frontier:
                truncated = {"reason": "depth_limit", "nodes": len(visited), "depth_reached": depth_limit}

        for method_id in visited:
            summary = self._method_summaries([method_id])
            nodes[method_id] = summary[0] if summary else {"method_id": method_id}
        return {
            "entry": entry_method_id,
            "nodes": nodes,
            "edges": edges,
            "gaps": gaps,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # 落盘（可选）
    # ------------------------------------------------------------------

    def save_tree(self, entry_id: str, tree: dict[str, Any]) -> Path:
        """落盘 run_dir/api-surface/call_tree/{entry_id}.json（原子写 + 0o600）。"""

        directory = self._run_dir / "api-surface" / "call_tree"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / f"{entry_id}.json"
        temporary = directory / f".{entry_id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(tree, ensure_ascii=False, indent=2), "utf-8"
            )
            import os

            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return target
