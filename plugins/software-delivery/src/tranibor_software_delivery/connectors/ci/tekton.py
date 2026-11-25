# SPDX-License-Identifier: Apache-2.0
"""The Tekton CiAdapter: PipelineRun state, logs, triggering, incoming CloudEvents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tranibor_adapters.executor.tekton import CORE_API, TEKTON_API, KubernetesClient
from tranibor_contracts import InboundEvent
from tranibor_contracts.wire import label_keys, read_label
from tranibor_core.domain import CheckRun, CiStatus

from ...names import DeliveryEvent

CLOUD_EVENT_MAP = {
    "dev.tekton.event.pipelinerun.started.v1": DeliveryEvent.CI_STARTED,
    "dev.tekton.event.pipelinerun.successful.v1": DeliveryEvent.CI_SUCCEEDED,
    "dev.tekton.event.pipelinerun.failed.v1": DeliveryEvent.CI_FAILED,
    "dev.tekton.event.pipelinerun.unknown.v1": DeliveryEvent.CI_STARTED,
}


def _identity(run: dict[str, Any]) -> str:
    """A PipelineRun's identity, for a run both selectors return (it carries both label families)."""
    metadata = run.get("metadata") or {}
    return str(metadata.get("uid") or metadata.get("name") or "")


class TektonCi:
    def __init__(
        self,
        client: KubernetesClient | None = None,
        *,
        namespace: str = "default",
        pipeline: str = "choregos-ci",
    ) -> None:
        self.client = client or KubernetesClient()
        self.namespace = namespace
        self.pipeline = pipeline

    async def status_for(self, repo: str, sha: str) -> CiStatus:
        # A pipeline labels its runs `tranibor/sha=<commit>`, or, written before ADR 0224, `choregos/sha`:
        # a selector names one key, so both are asked.
        runs: list[dict[str, Any]] = []
        for key in label_keys("sha", domain=False):
            payload = await self.client.request(
                "GET",
                f"{TEKTON_API}/namespaces/{self.namespace}/pipelineruns",
                params={"labelSelector": f"{key}={sha}"},
            )
            seen = {_identity(r) for r in runs}
            runs += [r for r in (payload or {}).get("items", []) if _identity(r) not in seen]
        if not runs:
            return CiStatus(state="unknown", sha=sha)
        checks: list[CheckRun] = []
        state = "success"
        for run in runs:
            conditions = ((run.get("status") or {}).get("conditions") or [{}])[0]
            status = str(conditions.get("status", "Unknown"))
            conclusion = {"True": "success", "False": "failure"}.get(status)
            checks.append(
                CheckRun(
                    name=run["metadata"]["name"],
                    status="completed" if conclusion else "in_progress",
                    conclusion=conclusion,
                )
            )
            if conclusion is None:
                state = "running"
            elif conclusion == "failure":
                state = "failure"
                break
        return CiStatus(state=state, runs=checks, sha=sha)

    async def logs(self, run_ref: str, tail: int = 500) -> str:
        pods = await self.client.request(
            "GET",
            f"{CORE_API}/namespaces/{self.namespace}/pods",
            params={"labelSelector": f"tekton.dev/pipelineRun={run_ref}"},
        )
        chunks: list[str] = []
        for pod in (pods or {}).get("items", []):
            text = await self.client.request(
                "GET", f"{CORE_API}/namespaces/{self.namespace}/pods/{pod['metadata']['name']}/log"
            )
            chunks.append(str(text or ""))
        return "\n".join("\n".join(chunk.splitlines()[-tail:]) for chunk in chunks)

    async def trigger(self, repo: str, ref: str, pipeline: str) -> str:
        body = {
            "apiVersion": "tekton.dev/v1",
            "kind": "PipelineRun",
            "metadata": {"generateName": f"{pipeline or self.pipeline}-", "namespace": self.namespace},
            "spec": {
                "pipelineRef": {"name": pipeline or self.pipeline},
                "params": [
                    {"name": "repo-url", "value": f"https://github.com/{repo}.git"},
                    {"name": "revision", "value": ref},
                ],
            },
        }
        payload = await self.client.request(
            "POST", f"{TEKTON_API}/namespaces/{self.namespace}/pipelineruns", json=body
        )
        return str((payload or {}).get("metadata", {}).get("name", ""))

    def parse_event(self, headers: Mapping[str, str], body: bytes) -> list[InboundEvent]:
        import json

        payload: dict[str, Any] = json.loads(body or b"{}")
        run = payload.get("pipelineRun", payload)
        labels = (run.get("metadata") or {}).get("labels") or {}
        ce_type = headers.get("ce-type", headers.get("Ce-Type", ""))
        return [
            InboundEvent(
                type=CLOUD_EVENT_MAP.get(ce_type, DeliveryEvent.CI_STARTED),
                source="tekton",
                delivery_id=headers.get("ce-id", headers.get("Ce-Id", "")),
                project_slug=read_label(labels, "project", domain=False),
                work_item_key=read_label(labels, "work-item", domain=False),
                payload={
                    "pipeline_run": (run.get("metadata") or {}).get("name"),
                    "sha": read_label(labels, "sha", domain=False),
                    "run_id": read_label(labels, "run-id", domain=False),
                },
            )
        ]

    async def test(self) -> dict[str, Any]:
        payload = await self.client.request("GET", TEKTON_API)
        return {"ok": payload is not None, "namespace": self.namespace}
