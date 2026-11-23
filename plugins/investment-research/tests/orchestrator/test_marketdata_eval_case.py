# SPDX-License-Identifier: Apache-2.0
"""The eval case E6, `marketdata-volatility` (S57-09, ADR 0122 §8-9), on the plugin's `marketdata` scene.

The case lives with the core's agent cases (`tranibor_playbooks`), but the scene it plays on, its fake and
its connector kind are this plugin's (ADR 0181 O4-4). Its three checks moved here from the core's suites
(S83-05n), unchanged, where they ran parametrized beside the cases of the core's scenes:

- `packages/playbooks/tests/test_agent_case_assertions.py`: the volatilities the case's answer must hold are
  what the scene's bars give;
- `apps/orchestrator/tests/test_eval_live_agent_cases.py`: the case seeds in the live matrix and its stage
  is prepared (its connector on its scene, its agent resolved, no repository);
- `apps/orchestrator/tests/test_eval_doors_parity.py`: through both doors of a run's tools, the model reads
  what the script gets, whole.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import statistics
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tranibor_adapters.fakes.scenes import EVAL_FAKE_SCENES_ENV
from tranibor_orchestrator.activities import eval_doors, evals
from tranibor_orchestrator.activities import stage as stage_activities
from tranibor_orchestrator.config import reset_settings_cache
from tranibor_playbooks.evals.agents import AGENT_CASES_DIR, load_case
from tranibor_testkit.orchestrator import Fixture

CASE = "marketdata-volatility"


# ───────────────────────── the answer, from the scene's bars ─────────────────────────


def test_e6_s_volatilities_are_what_the_bars_give() -> None:
    from tranibor_adapters.fakes.scenes import scene

    case = load_case(AGENT_CASES_DIR / f"{CASE}.yaml")
    # The scene's fake, declared by the `investment-research` extension (ADR 0181 O4-4).
    market = scene("marketdata").fake

    async def volatility(symbol: str) -> float:
        bars = (
            await market.execute("get_bars", {"symbol": symbol, "start": "2026-07-01", "end": "2026-10-30"})
        )["bars"]
        closes = [bar["adj_close"] for bar in bars][-31:]
        returns = [math.log(b / a) for a, b in itertools.pairwise(closes)]
        return statistics.stdev(returns) * math.sqrt(252) * 100

    symbols = [call["arguments"]["symbol"] for call in case["evidence"]]
    found = {symbol: asyncio.run(volatility(symbol)) for symbol in symbols}
    ranked = sorted(found.items(), key=lambda item: -item[1])
    assert [[s, f"{v:.1f}"] for s, v in ranked[:5]] == [
        fact["tokens"] for fact in case["answer_must_contain"]
    ]
    assert f"{ranked[4][1]:.1f}" != f"{ranked[5][1]:.1f}", "no tie at the fifth place"


# ───────────────────────── the live matrix ─────────────────────────


@pytest.fixture
def scripts_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TRANIBOR_TOOL_SCRIPTS", "on")
    reset_settings_cache()
    yield
    monkeypatch.delenv("TRANIBOR_TOOL_SCRIPTS", raising=False)
    reset_settings_cache()


@pytest.fixture
async def eval_project(setup: Fixture, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    """The matrix's eval project: memory off, no tools section (the cell decides)."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import active_policy, policy_model

    monkeypatch.setenv(EVAL_FAKE_SCENES_ENV, "1")
    async with session_scope() as session:
        row = await active_policy(session, setup.project_id)
        assert row is not None
        model = policy_model(row).model_dump(mode="json", exclude_none=True)
        model["memory"] = {**model.get("memory", {}), "enabled": False}
        model.pop("tools", None)
        row.json_doc = model
    return setup


async def _rows(model: Any, **where: Any) -> list[Any]:
    from sqlalchemy import select
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        query = select(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return list((await session.execute(query)).scalars().all())


async def test_the_case_of_the_live_matrix_seeds_and_its_stage_is_prepared(
    eval_project: Fixture, scripts_on: None
) -> None:
    """Its connector on its scene, its agent resolved by `prepare_stage` as for any run, the cell's door in
    the stage input, no repository."""
    cell = {
        "project_slug": "billing-api",
        "matrix_id": "evals-nightly",
        "backend": "claude-code",
        "model": "platform/standard",
        "with_memory": False,
        "context": "none",
        "tools": "scripts",
        "fixture": CASE,
        "agent_case": CASE,
    }
    seeded = await evals.seed_eval_ticket(cell)
    assert not seeded["skipped"], seeded["reason"]
    prepared = await stage_activities.prepare_stage(seeded["plan"])
    stage_input = prepared["stage_input"]
    assert stage_input["tools"]["scripts"] == {"max_concurrent_calls": 4}
    assert stage_input.get("repo") is None and stage_input["project"]["fact_commands"] == {}
    from tranibor_api.db.models import OrgConnector, Run

    [row] = await _rows(OrgConnector, name="marketdata")
    assert row.config == {"fake_scene": "default"}
    [run] = await _rows(Run, id=prepared["run_id"])
    assert run.agent_slug == "analyst"


# ───────────────────────── both doors read the same answer ─────────────────────────

#: What the tool server showed the model of an answer before #1374.
OLD_MAX_TEXT = 20_000

#: A script that makes the case's calls through the client and prints what each one gave it.
SCRIPT = """\
import json
import choregos_tools

calls = json.loads({calls!r})
print(json.dumps([choregos_tools.call(c["tool"], **c["arguments"]) for c in calls], ensure_ascii=False))
"""


def _calls(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"tool": str(call["tool"]), "arguments": dict(call.get("arguments") or {})}
        for call in case.get("evidence") or []
        if "tool" in call
    ]


def _body(answer: dict[str, Any]) -> str:
    """What the model reads of one `tools/call` answer, past the notices: one line of compact JSON."""
    text = answer["result"]["content"][0]["text"]
    [body] = [part for part in text.split("\n\n") if part.startswith("{")]
    return str(body)


def _data(read: dict[str, Any]) -> Any:
    """The data in what the model read: the structure, and a text the structure does not hold."""
    texts = [block["text"] for block in read.get("content", []) if block.get("type") == "text"]
    if not texts:
        return read["structuredContent"]
    return {"text": "\n".join(texts), "structured": read["structuredContent"]}


async def _cut_before(name: str) -> int:
    """How many of the case's answers the MCP door cut before #1374: their result as indented JSON,
    the data twice (its text block and its `structuredContent`), past 20,000 characters."""
    case = evals.load_agent_case(name)
    assert case is not None
    api = eval_doors.EvalApi(case)
    cut = 0
    for call in _calls(case):
        result = (await api.call(call["tool"], call["arguments"])).json()["result"]
        assert "truncated" not in result, "every answer within the platform's bound"
        cut += len(json.dumps(result, ensure_ascii=False, indent=2)) > OLD_MAX_TEXT
    return cut


async def test_the_model_reads_what_the_script_gets_on_the_scene(tmp_path: Path) -> None:
    """E6: all twenty `marketdata__get_bars`, which the MCP door cut before #1374."""
    case = evals.load_agent_case(CASE)
    assert case is not None
    calls = _calls(case)
    model = eval_doors.EvalApi(case)
    answers = await eval_doors.through_mcp(model, calls)
    script = eval_doors.EvalApi(case)
    ran = await eval_doors.through_the_bridge(
        script, {"name": "parity.py", "text": SCRIPT.format(calls=json.dumps(calls))}, tmp_path
    )
    assert ran["exit_code"] == 0, ran["stderr"]
    assert [c["origin"] for c in model.calls] == ["model"] * len(calls)
    assert [c["origin"] for c in script.calls] == ["script"] * len(calls)
    bodies, results = [_body(answer) for answer in answers], json.loads(ran["stdout"])

    assert await _cut_before(CASE) == 20, "the answers the MCP door cut before #1374"
    for body, result in zip(bodies, results, strict=True):
        assert "[cut by the tool server" not in body, "whole, through the MCP door"
        read = json.loads(body)
        assert set(read) == {"structuredContent"}, "the data once: no text that serializes it again"
        assert _data(read) == result, "the model reads the data the script gets"
