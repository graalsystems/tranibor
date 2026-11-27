"""The RuntimeClass of an agent pod: the deployment's when the policy's runtime is of its family, the
family's own name when the deployment names none, and a refusal when the deployment cannot give it
(finding #2349)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tranibor_adapters.errors import ConfigurationError, RuntimeClassRefusedError
from tranibor_contracts import Policy
from tranibor_core import load_preset
from tranibor_core.runtime_class import is_gvisor_class, is_kata_class, policy_runtime_refusal
from tranibor_orchestrator.activities.stage import runtime_class


def _policy(runtime: str) -> Policy:
    return Policy.model_validate({"metadata": {"name": "p", "version": "1"}, "sandbox": {"runtime": runtime}})


def _deployment(name: str) -> SimpleNamespace:
    return SimpleNamespace(runner_runtime_class=name)


def test_with_no_requirement_the_deployment_chooses() -> None:
    free = load_preset("solo")
    assert free.sandbox.runtime == "default"
    assert runtime_class(_deployment("kata"), free) == "kata"
    assert runtime_class(_deployment("gvisor"), free) == "gvisor"
    assert runtime_class(_deployment(""), free) is None


def test_a_gvisor_policy_runs_on_a_gvisor_deployment_under_its_class() -> None:
    gvisor = load_preset("regulated")
    assert gvisor.sandbox.runtime == "gvisor"
    assert runtime_class(_deployment("gvisor"), gvisor) == "gvisor"
    assert runtime_class(_deployment("runsc"), gvisor) == "runsc", "gVisor under the handler's name"


def test_a_gvisor_policy_on_a_deployment_that_names_no_class_still_names_gvisor() -> None:
    """Before #2349 a project could ask for gVisor on its own: that keeps working where it worked."""
    assert runtime_class(_deployment(""), _policy("gvisor")) == "gvisor"


@pytest.mark.parametrize("deployment", ["kata-vm-isolation", "runc", "kata"])
def test_a_gvisor_policy_on_a_deployment_without_gvisor_is_refused(deployment: str) -> None:
    """AKS pod sandboxing (Kata) and EKS (runc): a pod naming `gvisor` would never be admitted, and the
    run would wait for its timeout. It is refused at its start instead, as a configuration refusal."""
    with pytest.raises(RuntimeClassRefusedError) as refused:
        runtime_class(_deployment(deployment), _policy("gvisor"))
    assert isinstance(refused.value, ConfigurationError), "the workflow escalates it (S38-13e)"
    message = str(refused.value)
    for wanted in ("sandbox.runtime: gvisor", f"`{deployment}`", "global.runner.runtimeClass", "gVisor"):
        assert wanted in message, message


@pytest.mark.parametrize("deployment", ["kata-vm-isolation", "kata", "kata-qemu", "kata-clh"])
def test_a_kata_policy_runs_under_the_deployments_kata_class(deployment: str) -> None:
    assert runtime_class(_deployment(deployment), _policy("kata")) == deployment


def test_a_kata_policy_on_a_deployment_that_names_no_class_names_kata() -> None:
    """The same choice as gVisor's: the family's own name, never the cluster's runtime in silence."""
    assert runtime_class(_deployment(""), _policy("kata")) == "kata"


@pytest.mark.parametrize("deployment", ["gvisor", "runc", "runsc"])
def test_a_kata_policy_on_a_deployment_without_kata_is_refused(deployment: str) -> None:
    with pytest.raises(RuntimeClassRefusedError, match=r"sandbox\.runtime: kata"):
        runtime_class(_deployment(deployment), _policy("kata"))


def test_the_families_are_named_in_one_place() -> None:
    assert is_gvisor_class("gvisor") and is_gvisor_class("runsc") and not is_gvisor_class("runc")
    assert is_kata_class("kata") and is_kata_class("kata-vm-isolation") and not is_kata_class("katana")
    assert policy_runtime_refusal("default", "runc") is None
    assert policy_runtime_refusal("gvisor", None) is None and policy_runtime_refusal("gvisor", "") is None
