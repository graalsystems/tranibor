---
id: connector.runtime.tekton
title: Run agent stages as Tekton PipelineRuns
applies_to: {connector: runtime/tekton}
audience: operator
version: 2
reviewed: 2026-11-25
vendor_docs:
  - {title: "Tekton Pipelines", url: "https://tekton.dev/docs/pipelines/"}
egress: []
---

## Before you start

- A cluster with Tekton Pipelines, and the catalogue's `git-clone` Task in the runner namespace when
  projects have a repository. Nothing else of Tekton's is installed for the platform: each
  `PipelineRun` carries its Pipeline and its agent stage's Task inline.
- The default executor when Tekton is present: `runner.executor: tekton`.

## At the cluster

Install Tekton. Set `runner.executor: tekton` and `runner.namespacePattern` in the orchestrator's
values. Each agent stage becomes one `PipelineRun`; its Secret belongs to the run and goes with it
however it ends. Under `global.runner.agentIsolation: container` the agent stage's Task has the agent's
own container as a sidecar: Tekton waits for it to be ready before the runner's step (its default,
`await-sidecar-readiness`) and stops it after the last step; with Tekton's `enable-kubernetes-sidecar`
flag (Kubernetes 1.29+) it is a native sidecar.

## Permissions

The orchestrator creates, reads and deletes `PipelineRun`s and `Secret`s in the runner namespaces. A
stage's pod runs as `choregos-runner`, the account the chart creates: give it nothing beyond what the
runner needs. That account is the deployment's and is not set on a connector.

## In Tranibor

Chart values only: the connector has no field of its own. A `pipeline` field stored by an earlier
version is ignored.

## Test it

Start a run and look at `kubectl get pipelineruns -n <runner namespace>`.

## When it fails

- A clone that never starts: `git-clone` is not in that namespace.
- `Forbidden`: the orchestrator's service account lacks `pipelineruns` rights.
- The run's pod is rejected at admission: see the namespace policies (security context, `LimitRange`).
