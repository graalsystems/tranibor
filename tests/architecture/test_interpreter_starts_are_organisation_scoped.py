"""A work item's interpreter is started inside its organisation's scope (ADR 0220).

The interpreter's input names a project, not an organisation, so the payload codec cannot learn the
organisation from it: the caller says it, `with organisation_scope(project.org_id):` (the API's five
starts, the catch-up's two). A start outside one would seal the ticket's payloads under the platform
key, which is what ADR 0220 closes.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (ROOT / "apps" / "api" / "src", ROOT / "apps" / "orchestrator" / "src")
STARTS = {"start_interpreter", "restart_interpreter"}
#: The two files that define or forward the start, and take the scope their caller opened.
EXEMPT = {"temporal.py", "train_client.py"}


def _starts_an_interpreter(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    if name in STARTS:
        return True
    first = node.args[0] if node.args else None
    return (
        name == "start_workflow_once"
        and isinstance(first, ast.Constant)
        and first.value == "WorkflowInterpreter"
    )


def _unscoped(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scoped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With | ast.AsyncWith) and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "organisation_scope"
            for item in node.items
        ):
            scoped |= {id(inner) for inner in ast.walk(node)}
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _starts_an_interpreter(node) and id(node) not in scoped
    ]


def test_every_interpreter_start_of_the_api_is_inside_an_organisation_scope() -> None:
    callers = [p for root in SOURCES for p in root.rglob("*.py") if p.name not in EXEMPT]
    offenders = {str(p.relative_to(ROOT)): _unscoped(p) for p in callers if _unscoped(p)}
    assert not offenders, (
        f"start the interpreter inside `with organisation_scope(project.org_id):` {offenders}"
    )
    starts = [p for p in callers if any(s in p.read_text(encoding="utf-8") for s in STARTS)]
    assert len(starts) >= 6, "the guard must still see the call sites it protects"
