# SPDX-License-Identifier: Apache-2.0
"""The Tekton executor: one `PipelineRun` per agent step (docs/plan/02 §2.3).

The run's secret is created for that run and deleted at the end; the PipelineRun carries the
labels that make it possible to find the run again from a Tekton event.

The run's Secret belongs to its PipelineRun (`ownerReferences`), and goes with it however the
run ends (#501).

The PipelineRun carries its Pipeline inline (`pipelineSpec`, S87-05): the clone (the catalogue's
`git-clone`, by reference, when the project has a repository), then the agent stage, whose Task is
inline too (`taskSpec`). Nothing of the agent stage is read from the project's namespace, so no
older copy of a Task can run the agent beside the runner. Tekton Chains signs a PipelineRun whose
spec is inline as it signs one by reference.

Under `container` (ADR 0146) the agent stage's Task gets the agent's own container as a sidecar
(`separate_agent.tekton_sidecar`, the container `k8s_job` starts, holding nothing of the run's), and
the runner's step is told where its launcher listens, on the pod's loopback.
"""

from __future__ import annotations

import base64
import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx
from tranibor_contracts import ExecutorKind, StageResult
from tranibor_contracts.delivery import repository_of
from tranibor_contracts.env import with_legacy_entries
from tranibor_core.agent_isolation import SEPARATE_AGENT
from tranibor_core.domain import (
    RUN_GATEWAY_KEY_ENV,
    RUN_REMOTE_TOKEN_ENV,
    ExecRef,
    ExecStatus,
    StageJobSpec,
    utcnow,
)

from ..errors import ConfigurationError, UpstreamError
from . import separate_agent
from .isolation import refuse_unless_separate

TEKTON_API = "/apis/tekton.dev/v1"
CORE_API = "/api/v1"


class KubernetesClient:
    """A minimal Kubernetes REST client: what the executor needs, nothing more."""

    def __init__(
        self,
        base_url: str = "https://kubernetes.default.svc",
        token: str | None = None,
        *,
        verify: bool | str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # The pod's service-account token is THIS cluster's credential (S65-03): it goes to this
        # cluster's API server and nowhere else. Any other API server is spoken to with the token
        # the caller gave, or with none — and then answers 401, which names the mistake.
        self.token = token or (_in_cluster_token() if is_own_cluster(self.base_url) else None)
        self._client = client or httpx.AsyncClient(timeout=30.0, verify=verify)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # The caller's headers are ADDED to ours. Without this merge, passing `headers=` raised
        # `got multiple values for keyword argument 'headers'`: invisible in review, and fatal at
        # the first PATCH, which must announce its `Content-Type: application/merge-patch+json`
        # or be refused by the API.
        headers.update(kwargs.pop("headers", None) or {})
        response = await self._client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise UpstreamError(
                "kubernetes",
                f"{method} {path} → {response.status_code} : {response.text[:300]}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        # A pod's logs arrive as `text/plain`: parsing them as JSON raised a decoding error on
        # the first line, and `logs()` never returned anything.
        if "json" not in response.headers.get("content-type", ""):
            return response.text
        return response.json()


#: The names a pod reaches its own cluster's API server under. A cluster whose domain is not
#: `cluster.local` is still reached at the short names, which is what the default `base_url` uses.
OWN_CLUSTER_HOSTS = frozenset(
    {"kubernetes", "kubernetes.default", "kubernetes.default.svc", "kubernetes.default.svc.cluster.local"}
)


def is_own_cluster(base_url: str) -> bool:
    """True when `base_url` is the API server of the cluster this process runs in.

    By its service name, or by the address the kubelet gives every pod (`KUBERNETES_SERVICE_HOST`).
    Exact names only: a host that merely starts or ends like one of them is another server."""
    host = (urlsplit(base_url).hostname or "").lower()
    own_address = os.environ.get("KUBERNETES_SERVICE_HOST", "").strip().strip("[]").lower()
    return bool(host) and (host in OWN_CLUSTER_HOSTS or host == own_address)


def _in_cluster_token() -> str | None:
    from pathlib import Path

    path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


class TektonExecutor:
    """Creates and follows the `PipelineRun`s of agent steps."""

    kind = ExecutorKind.TEKTON
    #: `separate_agent` (ADR 0146 §9, S87-05): under `container` the agent stage's Task carries the agent's
    #: own container as a sidecar. Tekton's own sidecar mechanism, so no Kubernetes version is required.
    capabilities: frozenset[str] = frozenset({SEPARATE_AGENT})

    def __init__(
        self,
        client: KubernetesClient | None = None,
        *,
        service_account: str = "choregos-runner",
        storage: str = "10Gi",
        cpu_limit: str = "2",
        memory_limit: str = "6Gi",
        runner_requests: dict[str, str] | None = None,
        runner_limits: dict[str, str] | None = None,
    ) -> None:
        self.client = client or KubernetesClient()
        self.service_account = service_account
        self.storage = storage
        #: The agent stage's ceiling: the runner's step under `process`, the agent's container under
        #: `container`, whose runner step then takes `separate_agent.RUNNER_REQUESTS`/`RUNNER_LIMITS`.
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        #: The runner step's own requests and limits beside the agent's container (ADR 0146 §12), the chart's
        #: `runner.ownResources`, as `k8s_job`'s runner container takes them.
        self.runner_requests = dict(runner_requests or separate_agent.RUNNER_REQUESTS)
        self.runner_limits = dict(runner_limits or separate_agent.RUNNER_LIMITS)

    def _name(self, run_id: str) -> str:
        return f"run-{run_id}"[:63].lower()

    async def start(self, spec: StageJobSpec) -> ExecRef:
        """Idempotent: if the PipelineRun already exists, it is reused."""
        refuse_unless_separate(str(self.kind), self.capabilities, spec)
        if spec.agent_container is not None:
            raise ConfigurationError(
                "a hosted agent's container runs beside the runner in one pod (ADR 0080), which "
                "a PipelineRun's task steps cannot carry; use `runner.executor: k8s_job` for hosted "
                "agents"
            )
        name = self._name(spec.run_id)
        existing = await self.client.request(
            "GET", f"{TEKTON_API}/namespaces/{spec.namespace}/pipelineruns/{name}"
        )
        if existing is not None:
            # A replayed start: what failed the first time may have been handing the Secret over
            # to the PipelineRun. It is handed over here, as `k8s_job` does (#499).
            await self._attach_secret(spec.namespace, name, existing)
            return ExecRef(kind=self.kind, name=name, namespace=spec.namespace, run_id=spec.run_id)

        await self._ensure_secret(spec, name)
        body = self._pipeline_run(spec, name)
        try:
            created = await self.client.request(
                "POST", f"{TEKTON_API}/namespaces/{spec.namespace}/pipelineruns", json=body
            )
        except Exception:
            await self._drop_secret_without_pipeline_run(spec.namespace, name)
            raise
        await self._attach_secret(spec.namespace, name, created)
        return ExecRef(
            kind=self.kind,
            name=name,
            namespace=spec.namespace,
            run_id=spec.run_id,
            url=f"/#/namespaces/{spec.namespace}/pipelineruns/{name}",
        )

    async def _attach_secret(self, namespace: str, name: str, pipeline_run: Any) -> None:
        """The run's Secret belongs to its PipelineRun: when the PipelineRun is deleted (Tekton's
        pruner, `cancel`, a person), the garbage collector takes the token and the gateway key with
        it (#501). Before this, every run left its Secret behind, whatever its end.

        A merge patch of `ownerReferences` alone. Without a `uid` (a client that does not return
        the object), nothing breaks: the Secret stays ownerless, as before.
        """
        uid = (
            ((pipeline_run or {}).get("metadata") or {}).get("uid")
            if isinstance(pipeline_run, dict)
            else None
        )
        if not uid:
            return
        await self.client.request(
            "PATCH",
            f"{CORE_API}/namespaces/{namespace}/secrets/{name}",
            json={
                "metadata": {
                    "ownerReferences": [
                        {"apiVersion": "tekton.dev/v1", "kind": "PipelineRun", "name": name, "uid": uid}
                    ]
                }
            },
            headers={"Content-Type": "application/merge-patch+json"},
        )

    async def _drop_secret_without_pipeline_run(self, namespace: str, name: str) -> None:
        """The PipelineRun could not be created: the Secret written just before it has no owner.

        Deleted only if the PipelineRun really does not exist: a lost answer after a successful
        create would otherwise leave the run without its token. In doubt (the API does not
        answer), it is left: the replayed start hands it over or rewrites it.
        """
        with contextlib.suppress(Exception):  # the original error is the one to raise
            path = f"{TEKTON_API}/namespaces/{namespace}/pipelineruns/{name}"
            if await self.client.request("GET", path) is None:
                await self.client.request("DELETE", f"{CORE_API}/namespaces/{namespace}/secrets/{name}")

    async def _ensure_secret(self, spec: StageJobSpec, name: str) -> None:
        """The run's token and gateway key (#523), written even if a Secret of this name exists:
        a run started again must not get the key of an earlier attempt, revoked or unaccounted."""
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": name,
                "namespace": spec.namespace,
                "labels": {"choregos/run-id": spec.run_id, "choregos/project": spec.project_slug},
            },
            "type": "Opaque",
            "data": run_secret_data(spec),
        }
        existing = await self.client.request("GET", f"{CORE_API}/namespaces/{spec.namespace}/secrets/{name}")
        if existing is None:
            await self.client.request("POST", f"{CORE_API}/namespaces/{spec.namespace}/secrets", json=secret)
        else:
            await self.client.request(
                "PUT", f"{CORE_API}/namespaces/{spec.namespace}/secrets/{name}", json=secret
            )

    def _pipeline_run(self, spec: StageJobSpec, name: str) -> dict[str, Any]:
        depot = repository_of(spec.stage_input) if spec.stage_input else None
        pod_template: dict[str, Any] = {
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "nodeSelector": {"role": "runners"},
            "tolerations": [{"key": "role", "operator": "Equal", "value": "runners", "effect": "NoSchedule"}],
        }
        if spec.runtime_class:
            pod_template["runtimeClassName"] = spec.runtime_class
        if spec.agent_isolation == "container":
            # The cluster's services are not announced to the agent's container as variables.
            pod_template["enableServiceLinks"] = False
        return {
            "apiVersion": "tekton.dev/v1",
            "kind": "PipelineRun",
            "metadata": {
                "name": name,
                "namespace": spec.namespace,
                "labels": {
                    "choregos/run-id": spec.run_id,
                    "choregos/project": spec.project_slug,
                    **spec.labels,
                },
            },
            "spec": {
                # Inline (S87-05): no Pipeline nor Task of the namespace is read to run the agent stage.
                "pipelineSpec": self._pipeline_spec(spec, name, cloned=depot is not None),
                "taskRunTemplate": {"serviceAccountName": self.service_account, "podTemplate": pod_template},
                "timeouts": {"pipeline": f"{spec.timeout_minutes}m"},
                "params": [
                    {"name": "run-id", "value": spec.run_id},
                    {"name": "api-url", "value": spec.api_url},
                    {"name": "runner-image", "value": spec.runner_image},
                    # Empty when the project has no repository: the pipeline has no clone task, and
                    # the runner prepares an empty directory itself.
                    {
                        "name": "repo-url",
                        "value": (depot.url if depot else ""),
                    },
                    {
                        "name": "revision",
                        "value": (depot.base_branch if depot else "main"),
                    },
                ],
                "workspaces": [
                    {
                        "name": "source",
                        "volumeClaimTemplate": {
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {"requests": {"storage": self.storage}},
                            }
                        },
                    }
                ],
            },
        }

    def _pipeline_spec(self, spec: StageJobSpec, name: str, *, cloned: bool) -> dict[str, Any]:
        """The run's Pipeline: the clone when there is a repository, then the agent stage."""
        tasks: list[dict[str, Any]] = []
        if cloned:
            tasks.append(
                {
                    "name": "clone",
                    "taskRef": {"name": "git-clone"},
                    "workspaces": [{"name": "output", "workspace": "source"}],
                    "params": [
                        {"name": "url", "value": "$(params.repo-url)"},
                        {"name": "revision", "value": "$(params.revision)"},
                        {"name": "depth", "value": "50"},
                    ],
                }
            )
        tasks.append(
            {
                "name": AGENT_STAGE,
                **({"runAfter": ["clone"]} if cloned else {}),
                "taskSpec": self._agent_stage(spec, name),
                "workspaces": [{"name": "source", "workspace": "source"}],
            }
        )
        return {
            "params": [
                {"name": "run-id", "type": "string"},
                {"name": "api-url", "type": "string"},
                {"name": "runner-image", "type": "string"},
                {"name": "repo-url", "type": "string"},
                {"name": "revision", "type": "string", "default": "main"},
            ],
            "workspaces": [{"name": "source"}],
            "results": [
                {"name": "result-url", "value": f"$(tasks.{AGENT_STAGE}.results.result-url)"},
                {"name": "status", "value": f"$(tasks.{AGENT_STAGE}.results.status)"},
            ],
            "tasks": tasks,
        }

    def _agent_stage(self, spec: StageJobSpec, name: str) -> dict[str, Any]:
        """The agent stage's Task, inline: the runner's step and, under `container`, the agent's sidecar.

        The runner's step gets what `k8s_job`'s runner container gets (`runner_env_entries`), and works in
        the Task's workspace, which Tekton mounts at `$(workspaces.source.path)` in every step and
        sidecar. `command` is named, so Tekton needs not read the image's entrypoint from its registry."""
        args = list(spec.runner_command)
        if args[:1] == ["run"]:
            args += [
                "--write-result-url",
                "$(results.result-url.path)",
                "--write-status",
                "$(results.status.path)",
            ]
        step: dict[str, Any] = {
            "name": "run",
            "image": spec.runner_image,
            "command": ["choregos-runner"],
            "args": args,
            "workingDir": WORKSPACE_PATH,
            "env": [*runner_env_entries(spec, name), {"name": WORKSPACE_ENV, "value": WORKSPACE_PATH}],
            "computeResources": {
                "requests": {"cpu": spec.cpu, "memory": spec.memory},
                "limits": {"cpu": self.cpu_limit, "memory": self.memory_limit},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
        }
        task: dict[str, Any] = {
            "workspaces": [{"name": "source"}],
            "results": [{"name": "result-url"}, {"name": "status"}],
            "steps": [step],
        }
        if spec.agent_isolation == "container":
            separate_agent.add_to_task(
                task,
                # A code agent's image (ADR 0146 §10), the platform's otherwise.
                spec.agent_image or spec.runner_image,
                agent_requests={"cpu": spec.cpu, "memory": spec.memory},
                agent_limits={"cpu": self.cpu_limit, "memory": self.memory_limit},
                runner_requests=self.runner_requests,
                runner_limits=self.runner_limits,
            )
        return task

    async def status(self, ref: ExecRef) -> ExecStatus:
        payload = await self.client.request(
            "GET", f"{TEKTON_API}/namespaces/{ref.namespace}/pipelineruns/{ref.name}"
        )
        if payload is None:
            return ExecStatus(state="unknown", message="PipelineRun introuvable")
        status = payload.get("status", {}) or {}
        conditions = status.get("conditions", []) or []
        condition = conditions[0] if conditions else {}
        succeeded = str(condition.get("status", "Unknown"))
        reason = str(condition.get("reason", ""))
        state = {
            ("True", ""): "succeeded",
            ("False", ""): "failed",
            ("Unknown", ""): "running",
        }.get((succeeded, ""), "running")
        if reason in {"Cancelled", "PipelineRunCancelled"}:
            state = "cancelled"
        results = {
            str(item.get("name")): str(item.get("value", "")) for item in status.get("results", []) or []
        }
        return ExecStatus(
            state=state,
            message=str(condition.get("message", ""))[:500],
            reason=reason or None,
            result_url=results.get("result-url"),
            started_at=_parse_time(status.get("startTime")),
            ended_at=_parse_time(status.get("completionTime")),
        )

    async def fetch_result(self, ref: ExecRef) -> StageResult | None:
        """Reads the `StageResult` published by the runner through the Tekton results, if needed."""
        status = await self.status(ref)
        if not status.result_url or not status.result_url.startswith("http"):
            return None
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(status.result_url)
            if response.status_code >= 400:
                return None
            return StageResult.model_validate(response.json())

    async def logs(self, ref: ExecRef) -> AsyncIterator[str]:
        pods = await self.client.request(
            "GET",
            f"{CORE_API}/namespaces/{ref.namespace}/pods",
            params={"labelSelector": f"tekton.dev/pipelineRun={ref.name}"},
        )
        for pod in (pods or {}).get("items", []):
            name = pod["metadata"]["name"]
            text = await self.client.request(
                "GET",
                f"{CORE_API}/namespaces/{ref.namespace}/pods/{name}/log",
                params={"container": "step-run"},
            )
            for line in str(text or "").splitlines():
                yield line

    async def cancel(self, ref: ExecRef) -> None:
        """A clean cancellation: Tekton stops the Tasks, then the secret is cleaned up."""
        await self.client.request(
            "PATCH",
            f"{TEKTON_API}/namespaces/{ref.namespace}/pipelineruns/{ref.name}",
            json={"spec": {"status": "Cancelled"}},
            headers={"Content-Type": "application/merge-patch+json"},
        )
        await self.cleanup(ref)

    async def cleanup(self, ref: ExecRef) -> None:
        await self.client.request("DELETE", f"{CORE_API}/namespaces/{ref.namespace}/secrets/{ref.name}")

    async def test(self) -> dict[str, Any]:
        payload = await self.client.request("GET", f"{TEKTON_API}")
        return {"ok": payload is not None}


def _parse_time(value: Any) -> Any:
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return utcnow()


#: The key of a run's Secret that holds its gateway key (#523); the runner gets it as
#: `TRANIBOR_RUN_GATEWAY_KEY`, next to `TRANIBOR_RUN_TOKEN` from the key `token`.
GATEWAY_KEY_FIELD = "gateway-key"
#: The key that holds the access token of a remote agent (ADR 0062): `TRANIBOR_RUN_REMOTE_TOKEN`.
REMOTE_TOKEN_FIELD = "remote-token"  # noqa: S105 - a Secret key name, not a secret


#: The agent stage's task in the inline Pipeline.
AGENT_STAGE = "agent-stage"
#: Where Tekton mounts the Task's workspace, in the runner's step and the agent's sidecar alike, and the
#: variable that tells the runner its working tree there.
WORKSPACE_PATH = "$(workspaces.source.path)"
WORKSPACE_ENV = "TRANIBOR_WORKSPACE"


def runner_env_entries(spec: StageJobSpec, secret: str) -> list[dict[str, Any]]:
    """The runner's environment, the same for a Job's container and a Tekton step: the run's id and API,
    its token, gateway key and remote agent's token by reference to the run's Secret, the spec's own
    variables, each with its former name too."""
    return with_legacy_entries(
        [
            {"name": "TRANIBOR_RUN_ID", "value": spec.run_id},
            {"name": "TRANIBOR_API_URL", "value": spec.api_url},
            {
                "name": "TRANIBOR_RUN_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": secret, "key": "token"}},
            },
            # The run's gateway key, from the same Secret (#523). Optional: a run without a key (none
            # minted) still starts.
            {
                "name": RUN_GATEWAY_KEY_ENV,
                "valueFrom": {"secretKeyRef": {"name": secret, "key": GATEWAY_KEY_FIELD, "optional": True}},
            },
            # The access token of a remote agent (ADR 0062), same Secret, same rule.
            {
                "name": RUN_REMOTE_TOKEN_ENV,
                "valueFrom": {"secretKeyRef": {"name": secret, "key": REMOTE_TOKEN_FIELD, "optional": True}},
            },
            *[{"name": key, "value": value} for key, value in spec.env.items()],
        ]
    )


def run_secret_data(spec: StageJobSpec) -> dict[str, str]:
    """The data of a run's Secret: its token and, when one was minted, its gateway key (#523)."""
    data = {"token": base64.b64encode(spec.run_token.encode()).decode()}
    if spec.gateway_key:
        data[GATEWAY_KEY_FIELD] = base64.b64encode(spec.gateway_key.encode()).decode()
    if spec.remote_token:
        data[REMOTE_TOKEN_FIELD] = base64.b64encode(spec.remote_token.encode()).decode()
    return data
