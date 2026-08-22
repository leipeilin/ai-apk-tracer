"""红线回归：backend/app 源码不得 import rules 模块。

M2 评审 §4.3-5：backend 源码无 `import rules` / `from rules` 的零依赖红线。
使用 AST 仅检测真实 import 节点，避免把注释/字符串/docstring 中的说明文字
（如 sink_taxonomy.py 模块 docstring）误判为违规。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _rules_imports(path: Path) -> list[tuple[int, str]]:
    """返回 (lineno, 描述) 列表；未发现返回空列表。"""

    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "rules" or alias.name.startswith("rules."):
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "rules" or node.module.startswith("rules.")):
                violations.append((node.lineno, f"from {node.module} import ..."))
    return violations


def test_backend_app_has_no_rules_import() -> None:
    """backend/app 全部 Python 文件不得以 import 方式依赖 rules 包。"""

    assert APP_ROOT.is_dir(), f"backend app 目录不存在: {APP_ROOT}"
    offending: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        for lineno, description in _rules_imports(path):
            offending.append(f"{path.relative_to(APP_ROOT.parent.parent)}:{lineno}: {description}")
    assert not offending, "backend/app 出现 rules 包 import 依赖：\n" + "\n".join(offending)
