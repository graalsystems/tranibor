"""M8 — procurement end to end (S85-10, S85-13), on fakes, with the real runner.

The plugins are the archives the core ships offline, installed through the API like an organisation installs
them (`plugins/procurement/tests` read the same bytes): `procurement` 0.3.0 (the comparison of offers, no
server) for S85-10, `procurement-operations` 0.3.4 (onboarding, requests, renewals and the `purchasing`
server) for S85-13. The trade was one plugin until 0.2.1 (split in pull request 27 of the plugin
repository, #2134).

**The comparison of offers** (`procurement-sourcing`, workflow `offer-comparison`). A person files a
consultation with `start: false`, hands the S85-07 fixture offers to `POST /work-items/{id}/files`, and
starts it. The three job steps (extract, compare, tier) run the **real** runner in job mode, which runs the
real `tranibor-procurement` command in a subprocess on the files the API serves; the two agent steps are
scripted: the reader copies what the extracted text says, the analyst writes a memo from the numbers the
job computed. No model, no network.

Remove a brick — the `numbers_measured` gate, the `output_sections` gate, the evidence the extract job
measures, the tier job's threshold — and a scenario turns red.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from tranibor_adapters.fakes.executor import FakeExecutor
from tranibor_contracts import StageInput, StageResult, StageStatus
from tranibor_core.domain import ExecRef, ExecStatus, StageJobSpec, utcnow

from .conftest import Platform, login

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

ORG = "/api/v1/orgs/northwind"
OFFLINE = Path(__file__).resolve().parents[2] / "plugins" / "offline"
HISTORIES = Path(__file__).resolve().parents[1] / "replay" / "histories"
FIXTURES = Path(__file__).resolve().parents[2] / "plugins" / "procurement" / "tests" / "fixtures"
JOB_IMAGE = "registry.example/tranibor-procurement@sha256:" + "c" * 64
OFFERS = ("alderbrook-offer.pdf", "brightfield-offer.docx", "corvane-prices.xlsx")
AWARD_ATTESTATION = "I award this consultation on the basis of the files and the memo I read."
SECOND = "second-admin@northwind.dev"
BUYER = "buyer@northwind.dev"
MEMBER = "committee@northwind.dev"


async def _replayed(handle: Any, archive: str) -> None:
    """The work item's history replays against the current code, and is archived for the next ones
    (`TRANIBOR_RECORD_HISTORY=1`): a change to the interpreter that would decide differently on a step
    fails `tests/replay`."""
    from temporalio.worker import Replayer
    from tranibor_orchestrator.workflows import ALL_WORKFLOWS

    history = await handle.fetch_history()
    await Replayer(workflows=ALL_WORKFLOWS).replay_workflow(history)
    if os.environ.get("TRANIBOR_RECORD_HISTORY"):
        target = HISTORIES / archive
        target.write_text(json.dumps(json.loads(history.to_json()), indent=1) + "\n", encoding="utf-8")


Agent = Callable[[StageInput], Awaitable[StageResult]]


class Bench(FakeExecutor):
    """The executor of the bench: a job step runs the real runner, an agent step is scripted.

    The runner runs in this event loop and talks to the API over real HTTP, as in a pod; its command
    runs in a subprocess. The run's result is the one the runner posted to the API."""

    def __init__(self, api_url: str, root: Path, agents: dict[str, Agent]) -> None:
        super().__init__()
        self.api_url, self.root, self.agents = api_url, root, agents
        self.jobs: dict[str, asyncio.Task[Any]] = {}
        self.outcomes: dict[str, Any] = {}

    async def start(self, spec: StageJobSpec) -> ExecRef:
        ref = ExecRef(
            kind=self.kind, name=f"bench-{spec.run_id}", namespace=spec.namespace, run_id=spec.run_id
        )
        if spec.run_id in self.statuses:
            return ref
        self.started.append(spec)
        stage_input = spec.stage_input
        assert stage_input is not None
        if stage_input.job is not None:
            self.statuses[spec.run_id] = ExecStatus(state="running", started_at=utcnow())
            self.jobs[spec.run_id] = asyncio.create_task(self._job(spec))
            return ref
        self.results[spec.run_id] = await self.agents[stage_input.transition.role](stage_input)
        self.statuses[spec.run_id] = ExecStatus(
            state="succeeded", exit_code=0, started_at=utcnow(), ended_at=utcnow()
        )
        return ref

    async def _job(self, spec: StageJobSpec) -> None:
        from tranibor_runner.config import RunnerSettings
        from tranibor_runner.runner import Runner

        settings = RunnerSettings(
            run_id=spec.run_id,
            api_url=spec.api_url,
            run_token=spec.run_token,
            workspace=self.root / spec.run_id,
        )
        try:
            outcome = await Runner(settings=settings).run()
            self.outcomes[spec.run_id] = outcome
            state = "succeeded" if int(outcome.exit_code) == 0 else "failed"
            message = outcome.detail
        except Exception as exc:  # the run fails on its own message, never silently
            self.outcomes[spec.run_id] = exc
            state, message = "failed", f"runner raised: {exc!r}"
        self.statuses[spec.run_id] = ExecStatus(
            state=state, exit_code=0 if state == "succeeded" else 1, message=message, ended_at=utcnow()
        )

    def played(self, role: str) -> list[StageJobSpec]:
        return [s for s in self.started if s.stage_input and s.stage_input.transition.role == role]

    def roles(self) -> list[str]:
        return [s.stage_input.transition.role for s in self.started if s.stage_input]


def _settings_changed() -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_orchestrator.config import get_settings

    reset_settings_cache()
    get_settings.cache_clear()


@pytest.fixture
def procurement(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The plugin from the archive the core ships, its job image mapped, its server run in memory."""
    from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
    from tranibor_adapters.plugin_servers import PluginServerRuntime
    from tranibor_api.services.plugin_servers import use_plugin_runtime, use_plugin_transport
    from tranibor_core.publishing import PUBLISHING

    monkeypatch.setenv("TRANIBOR_PLUGINS_OFFLINE", str(OFFLINE))
    monkeypatch.setenv("TRANIBOR_JOB_IMAGES", json.dumps({"procurement": JOB_IMAGE}))
    monkeypatch.setenv(
        "TRANIBOR_PLUGIN_SERVERS_REGISTRIES", json.dumps([PUBLISHING.image("tranibor-agents")])
    )
    _settings_changed()
    kube = FakeKubernetesApi()
    use_plugin_runtime(PluginServerRuntime(kube, "choregos-plugins"))
    yield kube
    use_plugin_runtime(None)
    use_plugin_transport(None)
    _settings_changed()


@pytest.fixture
async def api_url(platform: Platform, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """The API on a real port: the runner, and the job's subprocess, reach it over HTTP."""
    import uvicorn
    from tranibor_api.main import create_app
    from tranibor_orchestrator.config import reset_settings_cache

    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    serving = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("TRANIBOR_INTERNAL_API_URL", url)
    monkeypatch.setenv("TRANIBOR_HEARTBEAT_SECONDS", "1")
    reset_settings_cache()
    yield url
    server.should_exit = True
    await serving
    reset_settings_cache()


# -- the organisation ---------------------------------------------------------------------------------------


#: The versions `plugins/offline/plugins.lock` holds; operations moved to 0.3.4 after three nights.
VERSIONS = {"procurement": "0.3.0", "procurement-operations": "0.3.4"}


async def _install(client: AsyncClient, plugin: str = "procurement") -> None:
    """Two administrators install the plugin (ADR 0050); `procurement-operations` brings a server."""
    asked = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": plugin, "version": VERSIONS[plugin]}
    )
    assert asked.status_code == 202, asked.text
    added = await client.post(f"{ORG}/members", json={"email": SECOND, "role": "org_admin"})
    assert added.status_code in {200, 201}, added.text
    await login(client, SECOND)
    approved = await client.post(f"{ORG}/plugin-approvals/{asked.json()['approval_id']}/approve")
    assert approved.status_code == 200, approved.text
    await login(client)


async def _project(
    client: AsyncClient, template: str, slug: str, plugin: str = "procurement"
) -> dict[str, Any]:
    created = await client.post(
        f"{ORG}/projects",
        json={"slug": slug, "name": slug, "template_ref": f"plugin:{plugin}/{template}",
              "config": {"slug": slug, "org": "northwind"}},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    project = dict(created.json())
    tracker = await client.put(
        f"/api/v1/projects/{project['id']}/connectors/tracker", json={"type": "internal", "config": {}}
    )
    assert tracker.status_code == 200, tracker.text
    return project


async def _group(client: AsyncClient, slug: str, *members: str, role: str = "contributor") -> None:
    """An organisation group and its members, each a contributor of the organisation (it may decide)."""
    created = await client.post(f"{ORG}/groups", json={"slug": slug, "name": slug})
    assert created.status_code in {200, 201}, created.text
    for email in members:
        added = await client.post(f"{ORG}/members", json={"email": email, "role": role})
        assert added.status_code in {200, 201, 409}, added.text
        joined = await client.post(f"{ORG}/groups/{slug}/members", json={"email": email})
        assert joined.status_code in {200, 201}, joined.text


# -- the steps ----------------------------------------------------------------------------------------------


async def _inputs(stage_input: StageInput) -> dict[str, Any]:
    """What a step's agent is given: the inputs its transition declares, as the platform stores them (the
    API's own readers, the digest checked). A JSON input is parsed, a text one kept; `human_feedback`, the
    reason a person sent the work back, is the work item's document."""
    from sqlalchemy import select
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.object_store import object_store
    from tranibor_api.services.artifacts import latest as latest_artifact
    from tranibor_api.services.artifacts import read_content

    found: dict[str, Any] = {}
    async with session_scope() as session:
        item = (
            await session.execute(select(WorkItem).where(WorkItem.tracker_key == stage_input.work_item.key))
        ).scalar_one()
        for name in stage_input.transition.inputs:
            row = await latest_artifact(session, item.id, name)
            if row is None:
                if name in (item.documents or {}):
                    found[name] = item.documents[name]
                continue
            text = (await read_content(object_store(), row)).decode("utf-8")
            found[name] = json.loads(text) if row.type == "json" else text
    return found


def _line(label: str, quantity: float, unit_price: float, total: float) -> dict[str, Any]:
    return {"label": label, "quantity": quantity, "unit": "pc", "unit_price": unit_price, "line_total": total}


def _supplier(name: str, file: str, lines: list[dict[str, Any]], stated: float | None, **terms: Any) -> Any:
    return {"name": name, "currency": "EUR", "files": [file], "lines": lines, "stated_total": stated,
            "payment_terms_days": terms.get("payment"), "delivery_days": None,
            "validity_date": terms.get("validity"), "exclusions": []}  # fmt: skip


#: What the three fixture offers say, copied as an honest reader copies them: nothing computed, nothing
#: filled (a delivery in weeks is not a number of days, a total nobody writes is null).
def _offer(rows: list[tuple[str, float, float, float]]) -> list[dict[str, Any]]:
    return [_line(label, quantity, price, total) for label, quantity, price, total in rows]


READ = {
    "suppliers": [
        _supplier(
            "Alderbrook Metalworks Ltd",
            "alderbrook-offer.pdf",
            _offer([("Steel bracket type 4", 400, 12.5, 5000), ("Steel bracket type 7", 250, 22, 5500),
                    ("Delivery to the Lyon site", 1, 750, 750)]),
            11250,
            payment=45,
            validity="2026-12-31",
        ),
        _supplier(
            "Brightfield Supply Co",
            "brightfield-offer.docx",
            _offer([("Steel bracket type 4", 400, 12.75, 5100), ("Steel bracket type 7", 250, 22.4, 5600),
                    ("Delivery", 1, 800, 800)]),
            11500,
        ),
        _supplier(
            "Corvane Industrial SA",
            "corvane-prices.xlsx",
            _offer([("Steel bracket type 4", 400, 11.9, 4760), ("Steel bracket type 7", 250, 21.5, 5400),
                    ("Delivery", 1, 700, 700)]),
            None,
        ),
    ],
    "unassigned_files": [],
}  # fmt: skip


async def honest_reader(stage_input: StageInput) -> StageResult:
    names = {f["name"] for f in (await _inputs(stage_input))["offers_text"]["files"]}
    assert names == set(OFFERS), names
    return StageResult(
        status=StageStatus.DONE, summary="read", outputs={"offers_structured": json.dumps(READ)}
    )


def _memo(
    comparison: dict[str, Any],
    basis: dict[str, Any],
    *,
    leave_out_mismatches: bool = False,
    extra: str = "",
) -> str:
    """The memo an honest analyst writes: every number is one the job computed, every mismatch of the
    award basis is written down (a decimal point, no thousands separator)."""
    ranking = "\n".join(
        f"- Rank {r['rank']}: {r['supplier']}, total {r['total']:.2f}" for r in comparison["ranking"]
    )
    mismatches = "\n".join(
        f"- {m['supplier']}: {m['kind']} written {m['written']:.2f}, expected {m['expected']:.2f}"
        for m in basis["mismatches"]
    )
    named = "none" if leave_out_mismatches else mismatches
    return (
        "# Comparison memo\n\n"
        "## Summary\n"
        f"Lowest total {basis['lowest_total']:.2f}, highest total {basis['highest_total']:.2f}, "
        f"spread {basis['spread']:.2f}. This memo recommends nobody.\n\n"
        f"## Files read\n{', '.join(f for r in comparison['suppliers'] for f in r['files'])}\n\n"
        f"## Price comparison\n{ranking}\n\n"
        "## Differences and exclusions\nTransport and delivery are written differently by each supplier.\n\n"
        f"## Checks that failed\n{named}\n\n"
        f"## Questions for the suppliers\nPlease confirm the delivery terms.{extra}\n\n"
        "## What this memo does not cover\nQuality, references and the suppliers' finances.\n"
    )


async def honest_analyst(stage_input: StageInput) -> StageResult:
    given = await _inputs(stage_input)
    memo = _memo(given["comparison"], given["award_basis"])
    return StageResult(status=StageStatus.DONE, summary="memo", outputs={"comparison_memo_markdown": memo})


async def inventive_analyst(stage_input: StageInput) -> StageResult:
    """Writes a total nobody computed (a negotiated price it made up): every other number is measured."""
    given = await _inputs(stage_input)
    memo = _memo(given["comparison"], given["award_basis"], extra=" A total of 9876.54 is reachable.")
    return StageResult(status=StageStatus.DONE, summary="memo", outputs={"comparison_memo_markdown": memo})


async def forgetful_analyst(stage_input: StageInput) -> StageResult:
    """Every number it writes is measured, but the mismatch of the award basis is left unwritten."""
    given = await _inputs(stage_input)
    memo = _memo(given["comparison"], given["award_basis"], leave_out_mismatches=True)
    return StageResult(status=StageStatus.DONE, summary="memo", outputs={"comparison_memo_markdown": memo})


async def inventive_reader(stage_input: StageInput) -> StageResult:
    """Copies the offers but writes a unit price (99.99) that no offer states."""
    invented = json.loads(json.dumps(READ))
    invented["suppliers"][1]["lines"][0]["unit_price"] = 99.99
    return StageResult(
        status=StageStatus.DONE, summary="read", outputs={"offers_structured": json.dumps(invented)}
    )


#: What each analyst round was given: the reason of the person who sent the memo back, when there is one.
FEEDBACK: list[str | None] = []


async def listening_analyst(stage_input: StageInput) -> StageResult:
    given = await _inputs(stage_input)
    FEEDBACK.append(given.get("human_feedback"))
    return await honest_analyst(stage_input)


def _csv(rows: list[tuple[str, int, str, str]]) -> bytes:
    # Semicolon-separated: with commas, `100,400.00` is read by `numbers_measured` as the one number 100400
    # (a finding on CSV offers, origin S85-10): the reader's honest `100` and `400` are "measured nowhere".
    body = "\n".join(f"{item};{qty};{price};{total}" for item, qty, price, total in rows)
    return f"item;quantity;unit_price;total\n{body}\n".encode()


#: Two offers whose highest total (58000.00) is above the committee threshold the workflow writes (50000).
BIG = {
    "north.csv": _csv(
        [("Steel frame", 100, "400.00", "40000.00"), ("Installation", 1, "15000.00", "15000.00")]
    ),
    "south.csv": _csv(
        [("Steel frame", 100, "420.00", "42000.00"), ("Installation", 1, "16000.00", "16000.00")]
    ),
}


async def big_reader(stage_input: StageInput) -> StageResult:
    def supplier(name: str, file: str, price: float, line: float, install: float) -> Any:
        lines = [_line("Steel frame", 100, price, line), _line("Installation", 1, install, install)]
        return _supplier(name, file, lines, None)

    read = {
        "suppliers": [
            supplier("North Metal", "north.csv", 400, 40000, 15000),
            supplier("South Metal", "south.csv", 420, 42000, 16000),
        ],
        "unassigned_files": [],
    }
    return StageResult(
        status=StageStatus.DONE, summary="read", outputs={"offers_structured": json.dumps(read)}
    )


# -- the consultation ----------------------------------------------------------------------------------------


class _Items(dict[str, Any]):
    """The fake tracker's items: a work item born in the platform mirrors its state to a tracker that has
    never heard of it, and the fake learns it on first sight."""

    def __missing__(self, key: str) -> Any:
        from tranibor_core.domain import WorkItemData

        self[key] = WorkItemData(key=key, title=key, state="Todo")
        return self[key]


async def _file(
    platform: Platform, project: dict[str, Any], title: str, fields: dict[str, Any], workflow: str
) -> dict[str, str]:
    """Files the work item with `start: false`, as the console does when the workflow takes files."""
    if not isinstance(platform.adapters.tracker.items, _Items):
        platform.adapters.tracker.items = _Items(platform.adapters.tracker.items)
    created = await platform.client.post(
        f"/api/v1/projects/{project['id']}/work-items",
        json={"title": title, "workflow": workflow, "start": False, "fields": fields},
    )
    assert created.status_code == 201 and created.json()["workflow_name"] == workflow, created.text
    return {"id": created.json()["id"], "key": created.json()["tracker_key"]}


async def _hand(client: AsyncClient, item: dict[str, str], *names: str, **made: bytes) -> None:
    """Hands fixture files (by name) and files made by the scenario to `POST /work-items/{id}/files`."""
    files = [("files", (n, (FIXTURES / n).read_bytes(), "application/octet-stream")) for n in names]
    files += [("files", (n.replace("_", "."), data, "application/octet-stream")) for n, data in made.items()]
    stored = await client.post(f"/api/v1/work-items/{item['id']}/files", files=files)
    assert stored.status_code == 201, stored.text


async def _start(platform: Platform, project: dict[str, Any], item: dict[str, str]) -> Any:
    """The scenario starts the interpreter on the workers' queue, under the API's identifier, once the API
    has said the item may start (`mark_agent_ready`: 202 only when the files the workflow requires are in)."""
    from tranibor_api.temporal import interpreter_id

    ready = await platform.client.post(
        f"/api/v1/work-items/{item['id']}/actions", json={"action": "mark_agent_ready"}
    )
    assert ready.status_code == 202, ready.text
    return await platform.env.client.start_workflow(
        "WorkflowInterpreter",
        {"project_id": project["id"], "project_slug": project["slug"], "work_item_id": item["id"],
         "tracker_key": item["key"]},
        id=interpreter_id(project["slug"], item["key"]),
        task_queue="e2e",
    )  # fmt: skip


async def _state(
    client: AsyncClient, item_id: str, expected: str, what: str, tries: int = 1200
) -> dict[str, Any]:
    for _ in range(tries):
        read = (await client.get(f"/api/v1/work-items/{item_id}")).json()
        if read["state"] == expected:
            return dict(read)
        await asyncio.sleep(0.1)
    timeline = (await client.get(f"/api/v1/work-items/{item_id}/timeline")).text
    raise AssertionError(
        f"{what}: the work item is in `{read['state']}`, not `{expected}`\n{timeline[-3000:]}"
    )


async def _request(client: AsyncClient, item_id: str) -> dict[str, Any]:
    for _ in range(600):
        read = (await client.get(f"/api/v1/work-items/{item_id}")).json()
        if read.get("pending_request"):
            return dict(read["pending_request"])
        await asyncio.sleep(0.1)
    raise AssertionError("no human request opened")


async def _timeline(client: AsyncClient, item_id: str) -> str:
    return (await client.get(f"/api/v1/work-items/{item_id}/timeline")).text


async def _comparison_project(platform: Platform, worker_items: Any = None) -> dict[str, Any]:
    client = platform.client
    await login(client)
    await _install(client)
    await _group(client, "buyers", BUYER)
    await _group(client, "purchasing-committee", MEMBER)
    return await _project(client, "procurement-sourcing", "sourcing")


async def _award(client: AsyncClient, item_id: str, who: str, supplier: str) -> Any:
    await login(client, who)
    request = await _request(client, item_id)
    assert (request["kind"], request["payload"]["attest"]) == ("task", AWARD_ATTESTATION)
    return await client.post(
        f"/api/v1/work-items/{item_id}/decisions",
        json={"kind": "complete", "attested": True,
              "values": {"awarded_supplier": supplier, "rationale": "lowest measured total"}},
    )  # fmt: skip


async def test_an_honest_reader_and_analyst_reach_the_buyer_who_awards_with_the_attestation(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project = await _comparison_project(platform)
    bench = Bench(api_url, tmp_path / "runs", {"structure": honest_reader, "compare": honest_analyst})
    platform.adapters.executor = bench

    async with worker():
        item = await _file(platform, project, "Steel brackets", {"currency": "EUR"}, "offer-comparison")
        await _hand(client, item, *OFFERS)
        interpreter = await _start(platform, project, item)
        await _state(client, item["id"], "awaiting_buyer", "the memo reaches the buyer")
        assert bench.roles() == ["extract", "structure", "compare", "compare", "tier"], bench.roles()
        decided = await _award(client, item["id"], BUYER, "Corvane Industrial SA")
        assert decided.status_code == 202, decided.text
        result = await interpreter.result()
        await _replayed(interpreter, "wi-procurement-awarded-S85-10.json")
    assert result["state"] == "awarded"
    assert AWARD_ATTESTATION in await _timeline(client, item["id"])


async def _settled_in_review(
    platform: Platform, project: dict[str, Any], bench: Bench, offers: tuple[str, ...] = OFFERS, **made: bytes
) -> tuple[dict[str, str], Any]:
    client = platform.client
    item = await _file(platform, project, "Steel brackets", {"currency": "EUR"}, "offer-comparison")
    await _hand(client, item, *offers, **made)
    return item, await _start(platform, project, item)


async def test_totals_over_the_threshold_reach_the_committee_and_a_buyer_alone_cannot_award(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project = await _comparison_project(platform)
    bench = Bench(api_url, tmp_path / "runs", {"structure": big_reader, "compare": honest_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _settled_in_review(
            platform, project, bench, (), north_csv=BIG["north.csv"], south_csv=BIG["south.csv"]
        )
        await _state(client, item["id"], "awaiting_committee", "the highest total reaches the committee")
        tier = await _artifact_value(client, item["id"], "approval_tier")
        assert tier == "committee", tier

        await login(client, BUYER)
        refused = await client.post(
            f"/api/v1/work-items/{item['id']}/decisions",
            json={"kind": "complete", "attested": True,
                  "values": {"awarded_supplier": "South Metal", "rationale": "I am a buyer"}},
        )  # fmt: skip
        assert refused.status_code == 403, refused.text
        still = (await client.get(f"/api/v1/work-items/{item['id']}")).json()
        assert still["state"] == "awaiting_committee", "the refused decision changed nothing"

        decided = await _award(client, item["id"], MEMBER, "North Metal")
        assert decided.status_code == 202, decided.text
        result = await interpreter.result()
        await _replayed(interpreter, "wi-procurement-awarded-by-committee-S85-10.json")
    assert result["state"] == "awarded"


async def _artifact_text(client: AsyncClient, item_id: str, name: str) -> str:
    content = await client.get(f"/api/v1/work-items/{item_id}/artifacts/{name}/content")
    assert content.status_code == 200, content.text
    return content.text


async def _artifact_value(client: AsyncClient, item_id: str, name: str) -> Any:
    return (await _artifact_text(client, item_id, name)).strip().strip('"')


async def test_an_analyst_that_writes_a_total_the_job_did_not_compute_never_reaches_a_person(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    """(c) The memo is every number measured but one: a total of 9876.54 that no job computed."""
    client = platform.client
    project = await _comparison_project(platform)
    bench = Bench(api_url, tmp_path / "runs", {"structure": honest_reader, "compare": inventive_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _settled_in_review(platform, project, bench)
        await _state(client, item["id"], "needs_human", "the invented total sends the work item to a person")
        timeline = await _timeline(client, item["id"])
        await interpreter.cancel()
    assert len(bench.played("compare")) == 3, "the job, then the analyst had its two attempts"
    assert "9876.54" in timeline, "the refusal names the number"
    assert not bench.played("tier"), "nothing after the memo ran"
    states = [e["title"] for e in json.loads(timeline)["items"] if e["kind"] == "state_change"]
    assert not any("awaiting" in t for t in states), states


async def test_a_memo_that_omits_a_mismatch_of_the_award_basis_never_reaches_a_person(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    """(d) Corvane writes 5400.00 for 250 pieces at 21.50 (5375.00): the job counts it, the memo says it."""
    client = platform.client
    project = await _comparison_project(platform)
    bench = Bench(api_url, tmp_path / "runs", {"structure": honest_reader, "compare": forgetful_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _settled_in_review(platform, project, bench)
        await _state(client, item["id"], "needs_human", "a memo that hides a mismatch goes to a person")
        timeline = await _timeline(client, item["id"])
        await interpreter.cancel()
    assert len(bench.played("compare")) == 3
    assert "to quote written nowhere: award_basis: 5375, award_basis: 5400" in timeline
    assert not bench.played("tier")


async def test_a_reader_that_invents_a_unit_price_is_sent_back_then_escalates_to_a_person(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project = await _comparison_project(platform)
    bench = Bench(api_url, tmp_path / "runs", {"structure": inventive_reader, "compare": honest_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _settled_in_review(platform, project, bench)
        await _state(client, item["id"], "needs_human", "an invented price ends in a person's hands")
        timeline = await _timeline(client, item["id"])
        await interpreter.cancel()
    assert bench.roles() == ["extract", "structure", "structure"], "sent back once, then escalated"
    assert "99.99" in timeline
    assert not bench.played("compare")


async def test_an_image_only_pdf_among_the_files_stops_at_a_person_on_files_unreadable(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project = await _comparison_project(platform)
    bench = Bench(api_url, tmp_path / "runs", {"structure": honest_reader, "compare": honest_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _settled_in_review(platform, project, bench, (*OFFERS, "scan-only.pdf"))
        await _state(client, item["id"], "needs_human", "a scan stops the work item")
        timeline = await _timeline(client, item["id"])
        await interpreter.cancel()
    assert bench.roles() == ["extract"], "no agent read a file set with a scan in it"
    assert "files_unreadable" in timeline


async def test_a_send_back_returns_to_the_analyst_with_the_reason_and_the_rewritten_memo_is_awarded(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project = await _comparison_project(platform)
    FEEDBACK.clear()
    bench = Bench(api_url, tmp_path / "runs", {"structure": honest_reader, "compare": listening_analyst})
    platform.adapters.executor = bench
    reason = "Ask Corvane to confirm line two before I decide."

    async with worker():
        item, interpreter = await _settled_in_review(platform, project, bench)
        await _state(client, item["id"], "awaiting_buyer", "the first memo reaches the buyer")
        await login(client, BUYER)
        await _request(client, item["id"])
        sent = await client.post(
            f"/api/v1/work-items/{item['id']}/decisions", json={"kind": "reject", "reason": reason}
        )
        assert sent.status_code == 202, sent.text
        for _ in range(600):  # the analyst writes again
            if len(FEEDBACK) == 2:
                break
            await asyncio.sleep(0.1)
        assert reason == FEEDBACK[1] and FEEDBACK[0] is None, FEEDBACK
        for _ in range(600):  # the rewritten memo is a new request, not the one that was sent back
            pending = (await client.get(f"/api/v1/work-items/{item['id']}")).json()
            if pending["state"] == "awaiting_buyer" and pending.get("pending_request"):
                break
            await asyncio.sleep(0.1)
        decided = await _award(client, item["id"], BUYER, "Corvane Industrial SA")
        assert decided.status_code == 202, decided.text
        result = await interpreter.result()
        await _replayed(interpreter, "wi-procurement-sent-back-S85-10.json")
    assert result["state"] == "awarded"
    assert len(bench.played("compare")) == 3, "the job, the analyst, the analyst again"
    assert bench.roles().count("tier") == 2, "the tier is derived again from the new award basis"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The template's `t-no-award-buyer` and `t-no-award-committee` are unreachable: the interpreter opens "
        "ONE human request per wait state, for the first human transition (`t-award-*`), and a task whose "
        "form is another transition's is refused (422 `not in the task's form`). Finding for the agents "
        "repository (one task with an `award` or `no award` choice), origin S85-10."
    ),
)
async def test_the_buyer_may_close_the_consultation_without_an_award(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project = await _comparison_project(platform)
    bench = Bench(api_url, tmp_path / "runs", {"structure": honest_reader, "compare": honest_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _settled_in_review(platform, project, bench)
        await _state(client, item["id"], "awaiting_buyer", "the memo reaches the buyer")
        await login(client, BUYER)
        await _request(client, item["id"])
        closed = await client.post(
            f"/api/v1/work-items/{item['id']}/decisions",
            json={"kind": "complete", "values": {"no_award_reason": "Nobody fits."}},
        )
        assert closed.status_code == 202, closed.text
        result = await interpreter.result()
    assert result["state"] == "not_awarded"


async def test_starting_a_consultation_that_needs_files_is_refused(
    platform: Platform, procurement: Any
) -> None:
    project = await _comparison_project(platform)
    refused = await platform.client.post(
        f"/api/v1/projects/{project['id']}/work-items",
        json={"title": "Steel brackets", "workflow": "offer-comparison", "start": True,
              "fields": {"currency": "EUR"}},
    )  # fmt: skip
    assert refused.status_code == 422, refused.text
    assert "start: false" in refused.json()["detail"]
    one = await _file(platform, project, "Steel brackets", {"currency": "EUR"}, "offer-comparison")
    await _hand(platform.client, one, OFFERS[0])
    early = await platform.client.post(
        f"/api/v1/work-items/{one['id']}/actions", json={"action": "mark_agent_ready"}
    )
    assert early.status_code == 409, "a single offer is under the workflow's minimum of two"


# ======================================================================================================
# S85-13 — lot 2: a governed write and a renewal campaign, against the purchasing server's fake
# ======================================================================================================

REVIEWER, APPROVER, OUTSIDER = "reviewer@northwind.dev", "approver@northwind.dev", "outsider@northwind.dev"
BUYER_TWO = "buyer-two@northwind.dev"
SUPPLIER = {
    "legal_name": "Vexford Components GmbH",
    "registration_number": "HRB 770011",
    "country": "DE",
    "category": "components",
}
REVIEW_ATTESTATION = (
    "I reviewed the supplier's documents and the measured check, and the record may be created."
)
DECISION_ATTESTATION = (
    "I read the memo and the contract's notes; this decision, and the reason I give, are mine."
)
FAR = "2099-12-31"
MAKE_FIXTURES = Path(__file__).resolve().parents[2] / "plugins" / "procurement" / "tests" / "make_fixtures.py"


def _operations_tools() -> list[dict[str, Any]]:
    names = ("search_suppliers", "get_supplier", "list_framework_agreements", "list_contracts",
             "get_contract",
             "create_supplier", "update_supplier", "archive_supplier", "add_contract_note",
             "create_purchase_request", "cancel_purchase_request")  # fmt: skip
    return [{"name": n, "description": n, "inputSchema": {"type": "object"}} for n in names]


def _squash(text: str) -> str:
    return "".join(c for c in text.casefold() if c.isalnum())


class RefusalError(Exception):
    """An error the profile answers as `<code>: <message>`."""


class PurchasingFake:
    """The purchasing server on its fake, as the platform reaches it: MCP over HTTP behind the platform's
    bearer token (`FakePluginServer`), answering the profile's operations from a seeded in-memory system.

    Written from the server's profile (`PROFILE.md`) and its fake, which live in the agents repository: the
    shapes `{suppliers|contracts, total, next_cursor}`, a write that requires `action_key`, the same key with
    the same call returning the first result and writing nothing, `conflict: duplicate_supplier` on an active
    supplier with the same registration number and country. Every record is invented; contract dates are
    relative to today, since the job under test reads the clock itself."""

    def __init__(self, token: str, contracts: list[tuple[str, int]] | None = None) -> None:
        import datetime as dt

        from tranibor_adapters.fakes.plugin_servers import FakePluginServer

        self._server = FakePluginServer(_operations_tools(), [], name="purchasing", token=token)
        self._server._call = self._call  # type: ignore[method-assign]
        today = dt.datetime.now(dt.UTC).date()
        self.suppliers: dict[str, dict[str, Any]] = {
            "SUP-0001": {"id": "SUP-0001", "name": "Brightwater Office Supplies Ltd", "country": "GB",
                         "registration_number": "09990101", "status": "active"},
            "SUP-0002": {"id": "SUP-0002", "name": "Kestrel Logistik GmbH", "country": "DE",
                         "registration_number": "HRB 990202", "status": "active"},
            "SUP-0005": {"id": "SUP-0005", "name": "Marrowdale Catering Co", "country": "IE",
                         "registration_number": "999505", "status": "archived"},
        }  # fmt: skip
        self.contracts: dict[str, dict[str, Any]] = {}
        for identifier, days in contracts or [("CT-0001", 10), ("CT-0002", 40), ("CT-0003", 80)]:
            self.contracts[identifier] = {
                "id": identifier, "supplier_id": "SUP-0001", "title": f"Contract {identifier}",
                "category": "office-supplies", "start_date": "2024-01-01",
                "end_date": (today + dt.timedelta(days=days)).isoformat(), "currency": "EUR",
                "value": "48000.00", "notes": [],
            }  # fmt: skip
        # A contract that never ends and one that ends far away: neither is listed by the 90 days window.
        base = next(iter(self.contracts.values()))
        self.contracts["CT-0098"] = {**base, "id": "CT-0098", "end_date": None}
        self.contracts["CT-0099"] = {
            **base, "id": "CT-0099",
            "end_date": (today + dt.timedelta(days=400)).isoformat(),
        }  # fmt: skip
        self.writes: list[str] = []
        self._keys: dict[str, tuple[str, dict[str, Any]]] = {}
        self._seq = 100

    def transport(self) -> Any:
        return self._server.transport()

    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        return self._server.calls

    def called(self, tool: str) -> list[dict[str, Any]]:
        return self._server.called(tool)

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._server.calls.append((tool, arguments))
        try:
            result = getattr(self, f"op_{tool}")(arguments)
        except RefusalError as refused:
            return {"isError": True, "content": [{"type": "text", "text": str(refused)}]}
        return {"structuredContent": result, "content": [{"type": "text", "text": json.dumps(result)}]}

    def _page(self, rows: list[dict[str, Any]], key: str, arguments: dict[str, Any]) -> dict[str, Any]:
        limit, start = int(arguments.get("limit") or 100), int(arguments.get("cursor") or 0)
        window = rows[start : start + limit]
        more = start + limit < len(rows)
        return {key: window, "total": len(rows), "next_cursor": str(start + limit) if more else None}

    def op_search_suppliers(self, a: dict[str, Any]) -> dict[str, Any]:
        found = [
            s for s in self.suppliers.values()
            if (a.get("include_archived") or s["status"] == "active")
            and (not a.get("name") or a["name"].casefold() in s["name"].casefold())
            and (not a.get("registration_number")
                 or _squash(a["registration_number"]) == _squash(s["registration_number"]))
            and (not a.get("country") or a["country"] == s["country"])
        ]  # fmt: skip
        return self._page(sorted(found, key=lambda s: (s["name"].casefold(), s["id"])), "suppliers", a)

    def op_list_contracts(self, a: dict[str, Any]) -> dict[str, Any]:
        found = [
            {**{k: v for k, v in c.items() if k != "notes"}, "note_count": len(c["notes"])}
            for c in self.contracts.values()
            if a.get("ending_before") is None or (c["end_date"] and c["end_date"] < a["ending_before"])
        ]
        return self._page(sorted(found, key=lambda c: (c["end_date"] or "9999", c["id"])), "contracts", a)

    def op_get_contract(self, a: dict[str, Any]) -> dict[str, Any]:
        if a["contract_id"] not in self.contracts:
            raise RefusalError(f"not_found: no contract {a['contract_id']}")
        return {"contract": self.contracts[a["contract_id"]]}

    def _once(self, tool: str, a: dict[str, Any]) -> dict[str, Any] | None:
        """The idempotency of a write: it needs its key, and the same key with the same call answers the
        first result and writes nothing; the same key with another call is a conflict."""
        key = a.get("action_key")
        if not key:
            raise RefusalError("refused: a write needs its action_key")
        fingerprint = json.dumps({k: v for k, v in a.items() if k != "action_key"}, sort_keys=True)
        known = self._keys.get(str(key))
        if known is None:
            return None
        if known[0] != f"{tool}:{fingerprint}":
            raise RefusalError("conflict: this action_key was used for another call")
        return known[1]

    def _remember(self, tool: str, a: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        fingerprint = json.dumps({k: v for k, v in a.items() if k != "action_key"}, sort_keys=True)
        self._keys[str(a["action_key"])] = (f"{tool}:{fingerprint}", result)
        return result

    def op_create_supplier(self, a: dict[str, Any]) -> dict[str, Any]:
        if (first := self._once("create_supplier", a)) is not None:
            return first
        for s in self.suppliers.values():
            if s["status"] == "active" and s["country"] == a["country"].upper() and (
                _squash(a.get("registration_number") or "x") == _squash(s["registration_number"])
            ):  # fmt: skip
                raise RefusalError("conflict: duplicate_supplier")
        self._seq += 1
        supplier = {"id": f"SUP-{self._seq:04d}", "name": a["name"], "country": a["country"].upper(),
                    "registration_number": a.get("registration_number"), "status": "active",
                    "categories": a.get("categories", [])}  # fmt: skip
        self.suppliers[supplier["id"]] = supplier
        self.writes.append(f"create_supplier {supplier['id']}")
        return self._remember("create_supplier", a, {"supplier_id": supplier["id"], "supplier": supplier,
                                                     "created": True})  # fmt: skip

    def op_archive_supplier(self, a: dict[str, Any]) -> dict[str, Any]:
        if (first := self._once("archive_supplier", a)) is not None:
            return first
        self.suppliers[a["supplier_id"]]["status"] = "archived"
        self.writes.append(f"archive_supplier {a['supplier_id']}")
        return self._remember("archive_supplier", a, {"supplier_id": a["supplier_id"], "archived": True})

    def op_add_contract_note(self, a: dict[str, Any]) -> dict[str, Any]:
        import datetime as dt

        if (first := self._once("add_contract_note", a)) is not None:
            return first
        if a["contract_id"] not in self.contracts:
            raise RefusalError(f"not_found: no contract {a['contract_id']}")
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._seq += 1
        self.contracts[a["contract_id"]]["notes"].append(
            {"id": f"NOTE-{self._seq}", "text": a["text"], "created_at": stamp}
        )
        self.writes.append(f"add_contract_note {a['contract_id']}")
        return self._remember("add_contract_note", a, {"contract_id": a["contract_id"], "note_id": self._seq,
                                                       "created_at": stamp})  # fmt: skip


def _pdf(*lines: str) -> bytes:
    import importlib.util

    spec = importlib.util.spec_from_file_location("procurement_make_fixtures", MAKE_FIXTURES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data: bytes = module.pdf([list(lines)])
    return data


def supplier_file(
    name: str = SUPPLIER["legal_name"], registration: str = SUPPLIER["registration_number"]
) -> dict[str, bytes]:
    """The three documents a complete onboarding file holds, each dated far ahead of any clock."""
    kinds = (("registration", "Extract of company registration"), ("tax", "Certificate of tax compliance"),
             ("insurance", "Certificate of liability insurance"))  # fmt: skip
    return {
        f"{kind}_pdf": _pdf(title, f"Issued to {name}", f"Registration {registration}", f"Valid until {FAR}")
        for kind, title in kinds
    }


async def checker(stage_input: StageInput) -> StageResult:
    """The agent that copies the supplier's file: its identity and its three dated documents, judged none."""
    given = (await _inputs(stage_input))["offers_text"]["files"]
    text = " ".join(f["text"] for f in given)
    name = SUPPLIER["legal_name"] if SUPPLIER["legal_name"] in text else "Kestrel Logistik GmbH"
    number = SUPPLIER["registration_number"] if name == SUPPLIER["legal_name"] else "HRB 990202"
    country = "DE"
    kinds = {
        "registration": "company_registration",
        "tax": "tax_clearance",
        "insurance": "liability_insurance",
    }
    documents = [
        {"kind": kinds[f["name"].split(".")[0]], "file": f["name"], "issuer": None, "issued_on": None,
         "expires_on": FAR, "identifier": None}
        for f in given
    ]  # fmt: skip
    file = {"identity": {"legal_name": name, "registration_number": number, "country": country},
            "documents": documents}  # fmt: skip
    return StageResult(
        status=StageStatus.DONE, summary="file read", outputs={"supplier_file": json.dumps(file)}
    )


def _server_token(kube: Any) -> str:
    import base64

    from tranibor_adapters.plugin_servers import TOKEN_ENV

    (secret,) = kube.store("secrets").values()
    return base64.b64decode(secret["data"][TOKEN_ENV]).decode()


async def _operations_project(
    platform: Platform, kube: Any, contracts: list[tuple[str, int]] | None = None
) -> tuple[dict[str, Any], PurchasingFake]:
    """The organisation of lot 2: the plugin installed, its server configured and reached on its fake, the
    groups the templates name, and a project of `procurement-operations`."""
    from tranibor_api.services.plugin_servers import use_plugin_transport

    client = platform.client
    await login(client)
    await _install(client, "procurement-operations")
    configured = await client.patch(
        f"{ORG}/connectors/purchasing",
        json={"config": {"backend": "odoo", "base_url": "https://purchasing.example.com",
                         "username": "integration"},
              "secret_values": {"api_key": "an-api-key"}},
    )  # fmt: skip
    assert configured.status_code == 200, configured.text
    fake = PurchasingFake(_server_token(kube), contracts)
    use_plugin_transport(fake.transport())
    await _group(client, "supplier-data", REVIEWER, APPROVER, role="approver")
    await _group(client, "buyers", BUYER, BUYER_TWO, role="approver")
    await _group(client, "outsiders", OUTSIDER, role="approver")
    return await _project(client, "procurement-operations", "operations", "procurement-operations"), fake


# -- helpers of the governed actions ---------------------------------------------------------------------


async def _pending_action(
    client: AsyncClient, project: dict[str, Any], kind: str, count: int = 1
) -> list[Any]:
    for _ in range(600):
        listed = (
            await client.get(
                f"/api/v1/projects/{project['id']}/actions", params={"status": "pending_approval"}
            )
        ).json()["items"]
        found = [a for a in listed if a["kind"] == kind]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.1)
    raise AssertionError(f"fewer than {count} `{kind}` action(s) waiting for approval")


async def _action(client: AsyncClient, project: dict[str, Any], action_id: str) -> dict[str, Any]:
    return dict((await client.get(f"/api/v1/projects/{project['id']}/actions/{action_id}")).json())


async def _decide_action(
    client: AsyncClient, project: dict[str, Any], action_id: str, who: str, decision: str = "approve"
) -> Any:
    await login(client, who)  # a fresh session: the approval asks for a recent authentication
    body = {"decision": decision} | (
        {"reason": "not the registration number on the extract"} if decision == "reject" else {}
    )
    return await client.post(f"/api/v1/projects/{project['id']}/actions/{action_id}/decision", json=body)


async def _until(what: str, condition: Callable[[], Awaitable[bool]], tries: int = 600) -> None:
    for _ in range(tries):
        if await condition():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out: {what}")


async def _onboard(
    platform: Platform, project: dict[str, Any], bench: Bench, fields: dict[str, Any], files: dict[str, bytes]
) -> tuple[dict[str, str], Any]:
    item = await _file(platform, project, f"Onboard {fields['legal_name']}", fields, "supplier-onboarding")
    await _hand(platform.client, item, **files)
    return item, await _start(platform, project, item)


async def _review(client: AsyncClient, item_id: str, who: str = REVIEWER) -> None:
    await login(client, who)
    request = await _request(client, item_id)
    assert (request["kind"], request["payload"]["attest"]) == ("task", REVIEW_ATTESTATION)
    reviewed = await client.post(
        f"/api/v1/work-items/{item_id}/decisions",
        json={
            "kind": "complete",
            "attested": True,
            "values": {"review_note": "Documents match the request."},
        },
    )
    assert reviewed.status_code == 202, reviewed.text


async def test_a_complete_file_creates_one_supplier_after_a_second_approval_and_a_replay_no_second(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    from tranibor_orchestrator.activities.actions import execute_effect

    client = platform.client
    project, fake = await _operations_project(platform, procurement)
    bench = Bench(api_url, tmp_path / "runs", {"structure": checker})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _onboard(platform, project, bench, SUPPLIER, supplier_file())
        await _state(client, item["id"], "awaiting_review", "the measured file reaches the reviewer")
        assert bench.roles() == ["extract", "structure", "check"]
        assert [name for name, _ in fake.calls] == ["search_suppliers"], "the check only searched"
        await _review(client, item["id"])

        (action,) = await _pending_action(client, project, "procurement.create_supplier")
        assert fake.writes == [], "nothing is written before the approvals"
        assert action["title"] == "Create the supplier Vexford Components GmbH (DE)"
        first = await _decide_action(client, project, action["id"], REVIEWER)
        assert first.status_code == 200 and first.json()["status"] == "pending_approval", first.text
        assert fake.writes == [], "the reviewer alone does not create it"
        second = await _decide_action(client, project, action["id"], APPROVER)
        assert second.status_code == 200 and second.json()["status"] == "approved", second.text
        await _state(client, item["id"], "created", "the supplier is created once approved")
        result = await interpreter.result()
        await _replayed(interpreter, "wi-procurement-supplier-onboarded-S85-13.json")
    assert result["state"] == "created"

    done = await _action(client, project, action["id"])
    assert done["status"] == "succeeded"
    assert sorted(d["by"] for d in done["decisions"]) == sorted([REVIEWER, APPROVER])
    assert fake.writes == ["create_supplier SUP-0101"], "one supplier"
    (created,) = fake.called("create_supplier")
    assert created["name"] == "Vexford Components GmbH" and created["categories"] == ["components"]
    assert created["action_key"], "the platform filled the idempotency key"
    assert not any("iban" in json.dumps(a).lower() for _, a in fake.calls), "no bank detail travels"

    # The activity replayed: the journal answers, nothing is sent again.
    again = await execute_effect({"action_id": action["id"], "position": 0})
    assert again["replayed"] is True and again["result"]["supplier_id"] == "SUP-0101"
    assert len(fake.called("create_supplier")) == 1
    # A worker killed between the call and its confirmation: the effect is sent again with the SAME key
    # and the server, not the platform, answers the first result.
    from sqlalchemy import select
    from tranibor_api.db.models import Action, ActionEffect
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        row = (
            await session.execute(select(ActionEffect).where(ActionEffect.action_id == action["id"]))
        ).scalar_one()
        row.status = "started"
        (running,) = (await session.execute(select(Action).where(Action.id == action["id"]))).scalars()
        running.status = "running"
    resent = await execute_effect({"action_id": action["id"], "position": 0})
    assert resent["replayed"] is False and resent["result"]["supplier_id"] == "SUP-0101"
    assert [c["action_key"] for c in fake.called("create_supplier")] == [created["action_key"]] * 2
    assert fake.writes == ["create_supplier SUP-0101"], "the same key writes once"
    assert len([s for s in fake.suppliers.values() if s["name"] == "Vexford Components GmbH"]) == 1


async def test_the_reviewer_alone_cannot_approve_and_nobody_outside_the_group_can(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project, fake = await _operations_project(platform, procurement)
    bench = Bench(api_url, tmp_path / "runs", {"structure": checker})
    platform.adapters.executor = bench

    async with worker():
        item, _ = await _onboard(platform, project, bench, SUPPLIER, supplier_file())
        await _state(client, item["id"], "awaiting_review", "the file reaches the reviewer")
        await _review(client, item["id"])
        (action,) = await _pending_action(client, project, "procurement.create_supplier")

        await _decide_action(client, project, action["id"], REVIEWER)
        twice = await _decide_action(client, project, action["id"], REVIEWER)
        assert (await _action(client, project, action["id"]))["status"] == "pending_approval", twice.text
        outsider = await _decide_action(client, project, action["id"], OUTSIDER)
        assert outsider.status_code == 403, outsider.text
        await asyncio.sleep(1.0)
        assert (
            fake.writes == []
            and (await _action(client, project, action["id"]))["status"] == "pending_approval"
        )
        assert (await client.get(f"/api/v1/work-items/{item['id']}")).json()["state"] == "approved"

        approved = await _decide_action(client, project, action["id"], APPROVER)
        assert approved.status_code == 200, approved.text
        await _state(client, item["id"], "created", "a second person of the group approves")
    assert fake.writes == ["create_supplier SUP-0101"]
    decisions = (await _action(client, project, action["id"]))["decisions"]
    assert sorted({d["by"] for d in decisions}) == sorted([REVIEWER, APPROVER]), "two distinct people"


async def test_a_duplicate_stops_before_any_approval_is_asked(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project, fake = await _operations_project(platform, procurement)
    bench = Bench(api_url, tmp_path / "runs", {"structure": checker})
    platform.adapters.executor = bench
    held = {"legal_name": "Kestrel Logistik GmbH", "registration_number": "HRB 990202", "country": "DE",
            "category": "logistics"}  # fmt: skip

    async with worker():
        item, interpreter = await _onboard(
            platform, project, bench, held, supplier_file("Kestrel Logistik GmbH", "HRB 990202")
        )
        await _state(client, item["id"], "needs_human", "a supplier already held stops the file")
        timeline = await _timeline(client, item["id"])
        actions = (await client.get(f"/api/v1/projects/{project['id']}/actions")).json()["items"]
        await interpreter.cancel()
    assert "duplicates_found" in timeline
    assert actions == [], "no action, so no approval, was ever asked"
    assert fake.writes == [] and fake.called("create_supplier") == []
    assert bench.roles() == ["extract", "structure", "check"]


async def test_a_rejected_approval_writes_nothing(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project, fake = await _operations_project(platform, procurement)
    bench = Bench(api_url, tmp_path / "runs", {"structure": checker})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _onboard(platform, project, bench, SUPPLIER, supplier_file())
        await _state(client, item["id"], "awaiting_review", "the file reaches the reviewer")
        await _review(client, item["id"])
        (action,) = await _pending_action(client, project, "procurement.create_supplier")
        rejected = await _decide_action(client, project, action["id"], APPROVER, "reject")
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected", rejected.text
        await asyncio.sleep(2.0)
        state = (await client.get(f"/api/v1/work-items/{item['id']}")).json()["state"]
        await interpreter.cancel()
    assert state != "created", state
    assert fake.writes == [] and fake.called("create_supplier") == [], "nothing reached the purchasing system"
    assert (await _action(client, project, action["id"]))["status"] == "rejected"


# -- the renewal campaign -----------------------------------------------------------------------------------


async def renewal_analyst(stage_input: StageInput) -> StageResult:
    """The memo of one contract, from what the campaign's job measured for it and nothing else."""
    from sqlalchemy import select
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        item = (
            await session.execute(select(WorkItem).where(WorkItem.tracker_key == stage_input.work_item.key))
        ).scalar_one()
        summary = dict(item.fields["summary"])
    memo = (
        f"# Renewal memo {summary['id']}\n\n"
        f"## Summary\n{summary['title']} ends on {summary['end_date']}.\n\n"
        f"## Contract\nValue {summary['value']} {summary['currency']}, category {summary['category']}.\n\n"
        f"## Notes on file\nThe contract holds {summary['note_count']} note.\n\n"
        "## Options\nRenew, renegotiate or terminate: the decision is the buyer's.\n\n"
        "## What this memo does not cover\nPerformance of the supplier and market prices.\n"
    )
    return StageResult(status=StageStatus.DONE, summary="memo", outputs={"renewal_memo_markdown": memo})


async def _campaign(platform: Platform, project: dict[str, Any]) -> tuple[dict[str, str], Any]:
    item = await _file(platform, project, "Contract renewals", {}, "contract-renewals")
    return item, await _start(platform, project, item)


async def _children(parent_key: str) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        rows = (
            (await session.execute(select(WorkItem).where(WorkItem.tracker_key.like(f"{parent_key}.%"))))
            .scalars()
            .all()
        )
        return [
            {"id": r.id, "key": r.tracker_key, "state": r.state, "fields": dict(r.fields)}
            for r in sorted(rows, key=lambda r: int(r.tracker_key.rsplit(".", 1)[1]))
        ]


async def _decide_contract(client: AsyncClient, project: dict[str, Any], child: dict[str, Any]) -> None:
    """A buyer decides on one contract (attested), and a second buyer approves the note it becomes."""
    await _state(client, child["id"], "memo_written", f"{child['key']} reaches its memo")
    await login(client, BUYER)
    request = await _request(client, child["id"])
    assert request["payload"]["attest"] == DECISION_ATTESTATION
    decided = await client.post(
        f"/api/v1/work-items/{child['id']}/decisions",
        json={"kind": "complete", "attested": True,
              "values": {"decision": "renew", "rationale": f"keep {child['fields']['contract_id']} a year"}},
    )  # fmt: skip
    assert decided.status_code == 202, decided.text
    title = f"Record the decision on contract {child['fields']['contract_id']}: renew"
    for _ in range(600):
        listed = (
            await client.get(
                f"/api/v1/projects/{project['id']}/actions", params={"status": "pending_approval"}
            )
        ).json()["items"]
        found = [a for a in listed if a["title"] == title]
        if found:
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(f"no action `{title}` waiting for approval")
    approved = await _decide_action(client, project, found[0]["id"], BUYER_TWO)
    assert approved.status_code == 200, approved.text
    await _state(client, child["id"], "recorded", f"{child['key']} is recorded on the contract")


async def test_a_renewal_campaign_opens_one_child_per_contract_and_the_parent_waits_for_all(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    client = platform.client
    project, fake = await _operations_project(platform, procurement)
    bench = Bench(api_url, tmp_path / "runs", {"renewal_memo": renewal_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _campaign(platform, project)

        async def three() -> bool:
            return len(await _children(item["key"])) == 3

        await _until("three children", three)
        children = await _children(item["key"])
        assert [c["fields"]["contract_id"] for c in children] == ["CT-0001", "CT-0002", "CT-0003"]
        assert {c["fields"]["summary"]["id"] for c in children} == {"CT-0001", "CT-0002", "CT-0003"}
        assert len(fake.called("list_contracts")) == 1 and fake.called("list_contracts")[0]["ending_before"]
        assert fake.writes == [], "no contract was written by listing"

        await _decide_contract(client, project, children[0])
        await _decide_contract(client, project, children[1])
        await asyncio.sleep(1.0)
        assert (await client.get(f"/api/v1/work-items/{item['id']}")).json()["state"] == "due", (
            "two of three are settled: the parent waits for the last"
        )
        await _decide_contract(client, project, children[2])
        await _state(client, item["id"], "settled", "all three settled: the campaign resumes")
        assert fake.writes == [f"add_contract_note CT-000{i}" for i in (1, 2, 3)]
        notes = [n["text"] for c in fake.contracts.values() for n in c["notes"]]
        assert all(t.startswith("Renewal decision: renew. Rationale: keep CT-000") for t in notes), notes

        await login(client, BUYER)
        closing = await _request(client, item["id"])
        assert closing["kind"] == "task"
        closed = await client.post(
            f"/api/v1/work-items/{item['id']}/decisions",
            json={"kind": "complete", "attested": True, "values": {"closing_note": "Nothing was left out."}},
        )
        assert closed.status_code == 202, closed.text
        result = await interpreter.result()
        await _replayed(interpreter, "wi-procurement-renewals-S85-13.json")
    assert result["state"] == "closed"
    assert len(fake.called("list_contracts")) == 1
    assert len(fake.called("get_contract")) == 0, "no contract held a note when the campaign listed them"


async def test_sixty_contracts_open_fifty_children_and_the_summary_quotes_the_ten_left_out(
    platform: Platform, worker: Any, procurement: Any, api_url: str, tmp_path: Path
) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    client = platform.client
    sixty = [(f"CT-{1000 + day}", day) for day in range(1, 61)]
    project, fake = await _operations_project(platform, procurement, sixty)
    bench = Bench(api_url, tmp_path / "runs", {"renewal_memo": renewal_analyst})
    platform.adapters.executor = bench

    async with worker():
        item, interpreter = await _campaign(platform, project)

        async def fifty() -> bool:
            return len(await _children(item["key"])) >= 50

        await _until("fifty children", fifty, tries=1800)
        await asyncio.sleep(2.0)
        children = await _children(item["key"])
        assert len(children) == 50, "the cap of ADR 0057: the sixty-first never opens, nor the fifty-first"
        assert [c["fields"]["contract_id"] for c in children] == [f"CT-{1000 + d}" for d in range(1, 51)]
        await interpreter.cancel()

    # Read once the worker is stopped: fifty children running hold the test database's connections,
    # and a read beside them waits for a free one until the pool gives up.
    timeline = await _timeline(client, item["id"])
    async with session_scope() as session:
        listing = (await session.execute(select(Run).where(Run.stage_role == "list"))).scalar_one()
    facts = ((listing.result or {}).get("evidence") or {}).get("facts") or {}
    assert (facts["contracts_expiring"], facts["contracts_left_out"]) == (50, 10), facts
    assert "10 left out past --max 50" in timeline, "the summary quotes the ten"
    left_out = json.loads(await _artifact_text(client, item["id"], "contracts"))["left_out"]
    assert left_out == [f"CT-{1000 + d}" for d in range(51, 61)], "the ten are named, never dropped"
    assert len(fake.called("list_contracts")) == 1
