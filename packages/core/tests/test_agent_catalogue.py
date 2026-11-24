"""The agent catalogue shipped with the platform (ADR 0040, S21-16), and its former names (S45-05)."""

from __future__ import annotations

import importlib

import pytest
import yaml
from tranibor_core.agent_catalogue import catalogue_skills, entries, entry, skill_files
from tranibor_core.dsl import TEMPLATES_DIR
from tranibor_core.instructions import render_instructions

INTERNAL = [e for e in entries() if e.kind == "internal"]

#: Recorded on main before the rename (S45-05): the digest of an entry is what an organisation's
#: installed copy is compared with, so no rename may move one (ADR 0098 §4).
RECORDED_DIGESTS = {
    # Every document moved once, on purpose: the plugins' versions that declare the five components
    # (S60-04, ADR 0127) — software-delivery 1.1.1, netops 1.2.0, authoring 1.1.0, requests 1.1.0. Sixteen
    # delivery agents moved once more in software-delivery 1.2.0 (the skills and knowledge their
    # instructions use, offline from 1.3.0, S83-04o): an installed one is offered the update.
    # The agents of procurement 0.3.0 and procurement-operations 0.3.4 (S85, ADR 0202) were born there.
    "agent-drafter": "sha256:28d4c5df5d91ed095358fe7990760eaa4b30f47ca3ed04c0d384ef39ed129734",
    "architect": "sha256:88ecc0245b687fa1eeb2da7c39dd6e9d6818ada400a1370f275e6eb9030865dc",
    "assistant": "sha256:e99375ec41aa60fd5202bdace406ee249c24627295f73db81f194e4f696e4e16",
    "brief-writer": "sha256:8c000f3c88b249f96bff9c4522486f34c2fb83d6ef59b4dde817b993a77e7cfe",
    "chatgpt": "sha256:c8381ed40acbc2888c3c4bf12ebcd9e6a49bf3edab1f7d7c070c685b7ba90af3",
    "ci-fixer": "sha256:e03314debde495f2dcb1e36bbcdf6facbf084ff8b35ac89dc139e3506e2a23a3",
    "claude-ai": "sha256:56cbd162bc57f803e6e162baf922669d6a50a3d205a521a88b69efc2b3113496",
    "claude-code": "sha256:9b26f873b0cea50cd208f055512bea877f3743addce82438d7c0d590321f836f",
    "claude-desktop": "sha256:39de108874699603fad6781e387cff032c6502304e3df228f11baaca217bdd89",
    "compliance": "sha256:04140763523b84016ab91560a5f1562f7d9d95460e1eb46bd363bca2b1c6a2ea",
    "copilot-vscode": "sha256:5d43fcb4c391dba5250bc056ddb8df3ab03ad735ffb6c610ffac1bf64422172a",
    "cursor": "sha256:105c9636b27da7f6df85564bfe58719db587d4f0ac20281939b10e1c914eef94",
    "developer": "sha256:669917186a5ec5d48e86a86678a843a07eae1c3d99f9511bc603eeabc9e95779",
    "devops": "sha256:fb5deca5b39c41871a0c41ebce494a966091e6a1502014be6eb1f56c01d514d3",
    "docs-writer": "sha256:ba8d70145092a4b6a0a109311033c67e166488402f579e0e7c2cfe3ace9bfccf",
    "knowledge-distiller": "sha256:01059be74d34854fc824794dea067b72716c8af4c0aa67477ec956a2a7ce538d",
    "netops": "sha256:7f6a8262fee61e909fc5cbeb78d2aa90eff5441868db3a2c150902c846af8718",
    "offer-reader": "sha256:c84057009db0984cbc593ab375f6da586501745c0b67ca9e96b2157578723579",
    "planner": "sha256:5178fd0e2273c5539d86f40394ee6ba6c0edeed2b9cdfd85ddb129e3dfddfb1b",
    "prod-verifier": "sha256:5bcd2c16c62f251fe4159e0d7738f861e53f1477acf9d6ce8fadf680b4d05b65",
    "purchasing-desk": "sha256:3a28d8b76718967c17a6483461c050771fde4d5e1c8b08c010c35313537f7d19",
    "qa": "sha256:52a584fb1d04f938696194887f4e9424b103471303b595497432a77c7bed5b98",
    "release-notes-writer": "sha256:4bd0fafa0ac5ce85df8dc9143a4fbc6b08a0378052b35bc20a2a73df4f340fce",
    "renewal-analyst": "sha256:59c5d32fec49f9baa1e795dd485d0394b063139f411cb8c732246595baff81d5",
    "request-qualifier": "sha256:cdd88241d9a376e89cb1d00e3e2f4edae922bb601006d4c9f50ec2a621585cbd",
    "researcher": "sha256:1821c99802c97dd6ca6ae1c01aed8ff07bbea402fdd2dc16ecf18794fd87db76",
    "review-responder": "sha256:16676d671c8d7c5549d0e8100866e5d0a6f9ba8b97acca3317b2f7ec623e2a6e",
    "reviewer": "sha256:7d75c7b36b847f4d8d072073cb60a21d171aa7ffaa22aaf393042d08d91c4655",
    "secops": "sha256:3fddcf91e661b32595719bbc20a67141de31bf6ca291388bc0fd89f1bdc6446b",
    "security-reviewer": "sha256:4475c3e6e275efe340102e3f815b368e41cbe02e31e0e3f7c31b9f7823bcb929",
    "sourcing-analyst": "sha256:0be9ab3733366e174bef753c197e951118c746cd09966d1a48e2de5bbfe66d8f",
    "spec-writer": "sha256:d5216835100324d818e5f43f4d72dd134199c7a47359eac54b7b6e621e1aba94",
    "supplier-file-checker": "sha256:98c6ed547624bbf6e257e0d8de5012eacbd37f8293bce98a44622bc281edfd67",
    "tester": "sha256:9b81d8a155878bd795835c2f54bcb311609f29cf71ef4a8710f49f404afae15d",
    "triager": "sha256:21b84b4838e89c322a618baff828434f5fb62f57b379fadcf394e4d76da99572",
    "ux-designer": "sha256:ed90d08347d27b93a76bb3e48598638b8854b5b9aa68cc5e7ebef7a150aa32c3",
}


def _workflow_inputs() -> set[str]:
    """Every input the shipped workflows declare: an agent must render with each of them."""
    names: set[str] = set()
    for path in TEMPLATES_DIR.glob("*.yaml"):
        for transition in yaml.safe_load(path.read_text(encoding="utf-8"))["transitions"]:
            names.update(transition.get("inputs") or [])
    return names | {"human_feedback", "test_report", "review_markdown", "security_review_markdown",
                    "research_markdown", "adr_path", "spec_markdown"}  # fmt: skip


@pytest.mark.parametrize("agent", INTERNAL, ids=lambda e: e.slug)
def test_every_catalogue_agent_renders_without_error(agent) -> None:  # type: ignore[no-untyped-def]
    """With the sample variables, without inputs, then with all of them: `StrictUndefined` refuses a
    name the template would read without `.get` — the agent would die at its first run."""
    instructions = agent.document["spec"]["instructions"]
    empty = render_instructions(instructions)
    full = render_instructions(instructions, inputs=dict.fromkeys(_workflow_inputs(), "x"))
    assert "## Work item" in empty and "DEMO-1" in empty
    assert len(full) >= len(empty)


def test_the_ticket_context_precedes_an_internal_agents_instructions() -> None:
    rendered = render_instructions(
        entry("developer").document["spec"]["instructions"],  # type: ignore[union-attr]
        ticket={"key": "SBX-12", "title": "Fix rounding", "body": "Totals are off by one cent."},
        spec="Given an invoice",
        allowed_paths=["src/orders/**"],
        inputs={"test_report": "2 tests fail", "human_feedback": ""},
    )
    assert rendered.index("**SBX-12** — Fix rounding") < rendered.index("You implement the work item")
    assert "## Approved specification\nGiven an invoice" in rendered
    assert "- `src/orders/**`" in rendered
    assert "## Input: test_report\n2 tests fail" in rendered
    assert "Input: human_feedback" not in rendered, "an empty input is not shown"


def test_no_catalogue_agent_fixes_a_runtime() -> None:
    """The runtime comes from the deployment: an agent that required `claude-code` would not run on a
    deployment that only serves opencode (S20-11). The model is the version's to name since ADR 0127
    (a tier or a model; `tests/conformance/test_shipped_agents_are_complete.py` says which agents do)."""
    for agent in entries():
        spec = agent.document["spec"]
        assert "backend" not in spec, agent.slug
        # A prompt agent (ADR 0080) names the platform's own loop, which every deployment serves: it
        # fixes no sandbox. Any other runtime is the deployment's to choose.
        assert "runtime" not in spec or spec["runtime"] == {"kind": "prompt"}, agent.slug


def test_every_role_of_the_package_has_its_agent_in_the_catalogue() -> None:
    playbooks = pytest.importorskip("tranibor_playbooks")
    roles = {e.role for e in INTERNAL}
    expected = set(playbooks.KNOWN_ROLES)
    assert expected <= roles, expected - roles


def test_the_skills_an_agent_names_are_in_the_catalogue() -> None:
    known = set(catalogue_skills())
    for agent in entries():
        assert set(agent.skills) <= known, (agent.slug, agent.skills)
    assert skill_files("madr-4")["SKILL.md"].startswith("---\nname: madr-4\n")


def test_an_external_client_says_where_it_calls_from_and_has_no_instructions_nor_model() -> None:
    external = [e for e in entries() if e.kind == "external"]
    assert {e.client for e in external} == {
        "claude-code",
        "claude-desktop",
        "cursor",
        "vscode",
        "chatgpt",
        "claude-ai",
    }
    assert {e.slug for e in external if e.reach == "cloud"} == {"chatgpt", "claude-ai"}
    assert all(not e.document["spec"] for e in external)


def test_the_digest_changes_when_the_text_changes() -> None:
    from dataclasses import replace

    agent = entry("triager")
    assert agent is not None
    other = replace(agent, document={**agent.document, "description": "x"})
    assert other.digest != agent.digest


def test_the_architect_checks_its_number_before_accepting() -> None:
    """#287: sent back by `adr_number_free`, the architect restarts from acceptance; that is where it
    rereads the default branch's numbers and renumbers its own if it is taken."""
    architect = entry("architect")
    assert architect is not None
    instructions = architect.document["spec"]["instructions"]
    accept = render_instructions(instructions, inputs={"adr_path": "docs/adr/0007-queue.md"})
    assert "docs/adr/0007-queue.md" in accept
    assert "adr_number_free" in accept and "git ls-tree" in accept
    assert "highest number on the default branch plus one" in accept


def test_every_entry_has_one_to_ten_valid_tags() -> None:
    import re

    for proposal in entries():
        assert 1 <= len(proposal.tags) <= 10, proposal.slug
        assert len(set(proposal.tags)) == len(proposal.tags), proposal.slug
        assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", tag) for tag in proposal.tags), proposal.slug


def test_the_architect_is_tagged_madr_architecture_design() -> None:
    found = entry("architect")
    assert found is not None
    assert found.tags == ("madr", "architecture", "design")


# ───────────────────────── S45-05: the rename moved nothing, the former names work ─────────────────────────


def test_digests_did_not_move() -> None:
    assert {e.slug: e.digest for e in entries()} == RECORDED_DIGESTS


def test_the_context_file_has_its_new_name() -> None:
    from tranibor_core.agent_catalogue import FOLDER

    assert (FOLDER / "_context.md").is_file()
    assert not (FOLDER / "_contexte.md").exists()  # lang: fr-ok


def test_former_package_is_gone() -> None:
    """`tranibor_core.catalogue_d_agents` was removed in 0.24.0 (S45-49)."""
    with pytest.raises(ImportError):
        importlib.import_module("tranibor_core.catalogue_d_agents")  # lang: fr-ok - removed, asserted gone
    with pytest.raises(ImportError):
        importlib.import_module("tranibor_core.catalogue_d_agents.evals")  # lang: fr-ok - asserted gone


@pytest.mark.parametrize(
    "former",
    [
        "EntreeDuCatalogue",  # lang: fr-ok - removed, asserted gone
        "DOSSIER",  # lang: fr-ok - removed, asserted gone
        "entrees",  # lang: fr-ok - removed, asserted gone
        "entree",  # lang: fr-ok - removed, asserted gone
        "skills_du_catalogue",  # lang: fr-ok - removed, asserted gone
        "fichiers_de_la_skill",  # lang: fr-ok - removed, asserted gone
    ],
)
def test_former_names_are_gone(former: str) -> None:
    from tranibor_core import agent_catalogue

    assert not hasattr(agent_catalogue, former)
