"""ADR 0226 (R9b): a runner's pod runs as `tranibor-runner` where the namespace has it, else as the former
`choregos-runner`.

Provisioning and the chart create both accounts for the window (0.30.x and 0.31.x). A project namespace
provisioned before 0.30.0 has only the former one, and its Role may not even let the executor read service
accounts: a pod named after an account that does not exist is never created, and nothing tells the run. So
the executor looks before it names one, in `k8s_job` and in `tekton` alike.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tranibor_adapters.executor.k8s_job import KubernetesJobExecutor
from tranibor_adapters.executor.tekton import (
    CORE_API,
    FORMER_RUNNER_SERVICE_ACCOUNT,
    RUNNER_SERVICE_ACCOUNT,
    TEKTON_API,
    KubernetesClient,
    RunnerServiceAccount,
    TektonExecutor,
)
from tranibor_core.domain import StageJobSpec

from ._transport import Wire

NS = "proj-billing-runners"
ACCOUNT = f"{CORE_API}/namespaces/{NS}/serviceaccounts/{RUNNER_SERVICE_ACCOUNT}"


def _spec() -> StageJobSpec:
    return StageJobSpec(
        run_id="r-1",
        project_slug="billing",
        namespace=NS,
        runner_image="registry.example/tranibor-runner:0.30.0",
        api_url="http://tranibor-api:8000",
        run_token="token",
    )


def _client(wire: Wire) -> KubernetesClient:
    return KubernetesClient("https://k8s.test", token="sa", client=wire.client())


@pytest.mark.parametrize(
    ("reply", "account"),
    [
        ((200, {"metadata": {"name": RUNNER_SERVICE_ACCOUNT}}), RUNNER_SERVICE_ACCOUNT),
        ((404, {"message": "not found"}), FORMER_RUNNER_SERVICE_ACCOUNT),
        # A Role written before 0.30.0 grants no `get` on service accounts: the former one is there.
        ((403, {"message": "forbidden"}), FORMER_RUNNER_SERVICE_ACCOUNT),
    ],
    ids=["provisioned-again", "provisioned-before", "role-before"],
)
async def test_the_account_is_the_new_one_where_the_namespace_has_it(reply: Any, account: str) -> None:
    wire = Wire({("GET", ACCOUNT): reply})
    assert await RunnerServiceAccount().name(_client(wire), NS) == account


async def test_a_configured_account_is_used_as_it_is_without_asking() -> None:
    wire = Wire()
    assert await RunnerServiceAccount("house-runner").name(_client(wire), NS) == "house-runner"
    assert wire.requests == []


async def test_the_new_account_is_kept_and_the_former_asked_again_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace provisioned again moves to the new account without a restart of the workers."""
    import tranibor_adapters.executor.tekton as tekton

    now = [1000.0]
    monkeypatch.setattr(tekton.time, "monotonic", lambda: now[0])
    wire = Wire({("GET", ACCOUNT): [(404, {}), (200, {})]})
    accounts = RunnerServiceAccount(recheck_s=300)
    client = _client(wire)
    assert await accounts.name(client, NS) == FORMER_RUNNER_SERVICE_ACCOUNT
    assert await accounts.name(client, NS) == FORMER_RUNNER_SERVICE_ACCOUNT
    assert len(wire.requests) == 1, "the former is remembered for a while"
    now[0] += 301
    assert await accounts.name(client, NS) == RUNNER_SERVICE_ACCOUNT
    assert await accounts.name(client, NS) == RUNNER_SERVICE_ACCOUNT
    assert len(wire.requests) == 2, "the new one, once found, is not asked again"


@pytest.mark.parametrize("found", [True, False], ids=["new", "former"])
async def test_a_job_runs_as_the_account_the_namespace_has(found: bool) -> None:
    jobs = f"/apis/batch/v1/namespaces/{NS}/jobs"
    routes: dict[tuple[str, str], Any] = {
        ("POST", f"{CORE_API}/namespaces/{NS}/secrets"): (201, {}),
        ("POST", jobs): (201, {"metadata": {"uid": "u-1"}}),
    }
    if found:
        routes[("GET", ACCOUNT)] = (200, {})
    wire = Wire(routes)
    await KubernetesJobExecutor(client=_client(wire)).start(_spec())
    job = next(r for r in wire.requests if r.method == "POST" and r.url.path == jobs)
    pod = json.loads(job.content)["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == (RUNNER_SERVICE_ACCOUNT if found else FORMER_RUNNER_SERVICE_ACCOUNT)
    assert pod["automountServiceAccountToken"] is False


@pytest.mark.parametrize("found", [True, False], ids=["new", "former"])
async def test_a_pipeline_run_runs_as_the_account_the_namespace_has(found: bool) -> None:
    routes: dict[tuple[str, str], Any] = {
        ("POST", f"{CORE_API}/namespaces/{NS}/secrets"): (201, {}),
        ("POST", f"{TEKTON_API}/namespaces/{NS}/pipelineruns"): (201, {}),
    }
    if found:
        routes[("GET", ACCOUNT)] = (200, {})
    wire = Wire(routes)
    await TektonExecutor(_client(wire)).start(_spec())
    run = wire.body(-1)
    expected = RUNNER_SERVICE_ACCOUNT if found else FORMER_RUNNER_SERVICE_ACCOUNT
    assert run["spec"]["taskRunTemplate"]["serviceAccountName"] == expected
