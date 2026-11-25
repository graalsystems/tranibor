"""Every workflow start of the shipped code names its organisation, or says why it cannot (ADR 0224).

ADR 0220 scoped the interpreter's starts (`test_interpreter_starts_are_organisation_scoped.py`); the other
workflows (actions, trials, provisioning, run cancellations, hosted builds, session pods, knowledge syncs
and distillations, the catch-up and memory loops, findings triage, the release train) were sealed under the
master key, which no organisation can shred. A start, a signal-with-start or a loop start outside
`with organisation_scope(org_id):` is refused here, unless `UNSCOPED` lists it with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    ROOT / "apps" / "api" / "src",
    ROOT / "apps" / "orchestrator" / "src",
    *sorted((ROOT / "plugins").glob("*/src")),
)
#: The gateway's and the clients' start gestures, and the helpers that start a workflow when it does not run.
STARTS = {
    "start_interpreter",
    "restart_interpreter",
    "start_action",
    "restart_action",
    "start_trial",
    "start_provisioning",
    "start_knowledge_sync",
    "start_reconciliation",
    "start_knowledge_distill",
    "start_memory_ingestion",
    "start_hosted_build",
    "start_session_pod",
    "start_run_cancellation",
    "start_workflow",
    "start_child_workflow",
    "start_workflow_once",
    "start_reconciliation_loop",
    "start_memory_loop",
    "start_tracker_reconciliation",
    "signal_findings",
    "signal_release_train",
}
#: The files that define or forward a start: they take the scope their caller opened (the API's gateway
#: and its fake, the orchestrator's client).
FORWARDERS = {
    "apps/api/src/tranibor_api/temporal.py",
    "apps/orchestrator/src/tranibor_orchestrator/train_client.py",
}
#: Starts that cannot name an organisation today, with the reason. A new entry needs one too.
UNSCOPED = {
    "apps/orchestrator/src/tranibor_orchestrator/workflows/findings.py": (
        "a child interpreter started from workflow code: the SDK encodes its input with the child's id as "
        "the only context, and workflow code cannot open a scope the codec sees (ADR 0224, 'Not covered')"
    ),
}


def _name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


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
        if isinstance(node, ast.Call) and _name(node) in STARTS and id(node) not in scoped
    ]


def _shipped() -> list[Path]:
    return [path for root in SOURCES for path in root.rglob("*.py")]


def test_every_workflow_start_is_inside_an_organisation_scope() -> None:
    offenders: dict[str, list[int]] = {}
    for path in _shipped():
        relative = str(path.relative_to(ROOT))
        if relative in FORWARDERS or relative in UNSCOPED:
            continue
        lines = _unscoped(path)
        if lines:
            offenders[relative] = lines
    assert not offenders, (
        "start the workflow inside `with organisation_scope(org_id):`, or list it in UNSCOPED with the "
        f"reason it cannot (ADR 0224): {offenders}"
    )


def test_the_guard_still_sees_the_starts_it_protects() -> None:
    callers = [
        p
        for p in _shipped()
        if str(p.relative_to(ROOT)) not in FORWARDERS and any(s + "(" in p.read_text("utf-8") for s in STARTS)
    ]
    assert len(callers) >= 20, f"only {len(callers)} files start a workflow: did the gestures get renamed?"


def test_each_unscoped_entry_is_still_true() -> None:
    for relative in UNSCOPED:
        path = ROOT / relative
        assert path.exists(), f"{relative} is gone: remove its UNSCOPED entry"
        assert _unscoped(path), f"{relative} is scoped now: remove its UNSCOPED entry"
