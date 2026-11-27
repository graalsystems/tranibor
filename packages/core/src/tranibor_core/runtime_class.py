# SPDX-License-Identifier: Apache-2.0
"""Which `RuntimeClass` an agent pod names, from the project policy's `sandbox.runtime` and the
deployment's class (finding #2349, ADR 0227).

The deployment's class (`TRANIBOR_RUNNER_RUNTIME_CLASS`, the chart's `global.runner.runtimeClass`) is
what the cluster offers. A policy may ask for a sandboxed runtime by family, `gvisor` or `kata`; the
family is matched against the deployment's class, never substituted for it:

- `default`: the deployment's class, else none (the cluster's runtime).
- `gvisor`: the deployment's class when it is a gVisor class (`GVISOR_CLASSES`); `gvisor` when the
  deployment names none (the behaviour before #2349, where a project asks for gVisor on its own).
- `kata`: the deployment's class when it is a Kata class (`is_kata_class`, `kata-vm-isolation` on AKS);
  `kata` when the deployment names none, as `gvisor` does.

A policy whose family the deployment's class is not of is refused at the run's start, and `/readiness`
says the same sentence before the first run (`policy_runtime_refusal`, one comparison for both): a pod
naming a class the cluster lacks is never admitted, and the run would wait for its timeout.
"""

from __future__ import annotations

#: The deployment's class, read by the API (`/readiness`) and by every worker.
RUNTIME_CLASS_ENV = "TRANIBOR_RUNNER_RUNTIME_CLASS"

#: The RuntimeClass names of gVisor: `gvisor` (GKE Sandbox, the operator's clusters, `infra/bootstrap`)
#: and `runsc`, the handler's own name, which a self-installed gVisor often keeps as the class's.
GVISOR_CLASSES: frozenset[str] = frozenset({"gvisor", "runsc"})
#: Kata: `kata` and every `kata-<flavour>` (`kata-vm-isolation` on AKS, `kata-qemu`, `kata-clh`, `kata-fc`,
#: the confidential `kata-qemu-snp`...), the names the Kata deployments and AKS pod sandboxing give it.
KATA_CLASS = "kata"


def is_gvisor_class(name: str) -> bool:
    return name in GVISOR_CLASSES


def is_kata_class(name: str) -> bool:
    return name == KATA_CLASS or name.startswith(f"{KATA_CLASS}-")


_FAMILIES = {"gvisor": ("gVisor", is_gvisor_class), "kata": ("Kata", is_kata_class)}
#: The class a family names when the deployment names none.
_UNSET = {"gvisor": "gvisor", "kata": KATA_CLASS}


def policy_runtime_refusal(asked: str, deployment: str | None) -> str | None:
    """Why a run whose policy asks for `sandbox.runtime: asked` cannot run on a deployment whose class is
    `deployment` (None or empty: none named), or None when it can."""
    family = _FAMILIES.get(asked)
    if family is None or not deployment:
        return None
    label, matches = family
    if matches(deployment):
        return None
    return (
        f"the project policy asks for `sandbox.runtime: {asked}` and the "
        f"deployment runs agent pods under RuntimeClass `{deployment}` ({RUNTIME_CLASS_ENV}, the chart's "
        f"`global.runner.runtimeClass`), which is not a {label} class: a pod naming another class is "
        "refused by a cluster that lacks it. Run the project on a deployment that offers "
        f"{label}, or change the policy's `sandbox.runtime`"
    )


def pod_runtime_class(asked: str, deployment: str | None) -> str | None:
    """The RuntimeClass an agent pod names (None: the cluster's runtime). Raises `ValueError` with the
    sentence of `policy_runtime_refusal` when the deployment cannot give what the policy asks."""
    refusal = policy_runtime_refusal(asked, deployment)
    if refusal is not None:
        raise ValueError(refusal)
    if deployment:
        return deployment
    return _UNSET.get(asked)


__all__ = [
    "GVISOR_CLASSES",
    "KATA_CLASS",
    "RUNTIME_CLASS_ENV",
    "is_gvisor_class",
    "is_kata_class",
    "pod_runtime_class",
    "policy_runtime_refusal",
]
