# SPDX-License-Identifier: Apache-2.0
"""S87-09: "production" is spelled in one place, `tranibor_core.production` (ADR 0228).

Thirteen modules used to write their own `{"staging", "prod"}`. A personal installation
(`TRANIBOR_ENV=personal`) is production too: every refusal those sets carried must reach it, and a module
that kept its own copy would silently let the personal posture through (a development sign-in, a `direct`
gateway, a demonstration connector). This test reads the source of every package and refuses any set,
frozenset, list or tuple literal whose strings are exactly `staging` and `prod`, outside the one module
that names production. A module asks `is_production()` or reads `PRODUCTION_ENVIRONMENTS`.

Lists of deployment stages that also name `dev` (a release train's environments, a project's default
`envs`) are a different notion and are not matched: the rule is the exact pair.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [
    *sorted((ROOT / "apps").glob("*/src")),
    *sorted((ROOT / "packages").glob("*/src")),
    *sorted((ROOT / "plugins").glob("*/src")),
    ROOT / "tools",
]
THE_ONE_PLACE = ROOT / "packages" / "core" / "src" / "tranibor_core" / "production.py"
PAIR = frozenset({"staging", "prod"})


def _strings(node: ast.AST) -> frozenset[str] | None:
    """The string constants of a set, list or tuple literal, or of `frozenset({...})`/`set([...])`."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"frozenset", "set"}
    ):
        if len(node.args) != 1:
            return None
        node = node.args[0]
    if not isinstance(node, ast.Set | ast.List | ast.Tuple):
        return None
    values = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    if len(values) != len(node.elts):
        return None
    return frozenset(values)


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    return [f"{path.relative_to(ROOT)}:{node.lineno}" for node in ast.walk(tree) if _strings(node) == PAIR]


def test_production_is_spelled_only_in_tranibor_core_production() -> None:
    files = [p for source in SOURCES for p in sorted(source.rglob("*.py")) if p != THE_ONE_PLACE]
    assert len(files) > 500, "the sources were not found: the test checks nothing"
    offences = [offence for path in files for offence in _offences(path)]
    assert not offences, (
        'a module spells production as {"staging", "prod"}: call tranibor_core.production.is_production() '
        f"so that a personal installation (ADR 0228) is production there too: {offences}"
    )


def test_the_rule_matches_what_it_refuses() -> None:
    """The detector itself: the pair in any literal form is found, a three-stage list is not."""
    found = [
        _strings(ast.parse(source, mode="eval").body)
        for source in (
            '{"staging", "prod"}',
            'frozenset({"prod", "staging"})',
            '("staging", "prod")',
            '["prod", "staging"]',
        )
    ]
    assert all(strings == PAIR for strings in found)
    assert _strings(ast.parse('("dev", "staging", "prod")', mode="eval").body) != PAIR
