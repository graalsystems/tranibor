# SPDX-License-Identifier: Apache-2.0
"""The finding types a work item may carry (ADR 0217): the core's and the plugins', which the contracts'
open list holds (`FINDING_TYPES`, ADR 0181), and the ones the work item's workflow declares in
`metadata.finding_types`. The first are valid everywhere; a workflow's are valid for its own work items
alone, so two trades may each name a type `duplicate-claim` without meeting.

The contracts read a finding's `type` as a name (a pattern): the platform judges it here, where it knows the
workflow. A workflow's declaration comes first, so a type the workflow declares keeps its title even if a
plugin installed later declares the same name (the validator refuses that at publication, but a stored
version is never rewritten).
"""

from __future__ import annotations

from dataclasses import dataclass

from tranibor_contracts import FINDING_TYPES, Workflow


@dataclass(frozen=True, slots=True)
class FindingTypeView:
    """One allowed finding type, as the validator, the API and the console read it."""

    name: str
    #: The workflow's title for it; `None` for a type the core or a plugin declares (shown by its name).
    title: str | None = None
    #: The severities a finding of this type may carry; `None`: every severity.
    severities: tuple[str, ...] | None = None
    description: str | None = None
    #: `None` for the core's and the plugins' types, else the workflow's name.
    workflow: str | None = None


def declared_by(workflow: Workflow | None) -> dict[str, FindingTypeView]:
    """The types `workflow` declares, by name."""
    if workflow is None:
        return {}
    return {
        decl.name: FindingTypeView(
            name=decl.name,
            title=decl.title,
            severities=tuple(str(s) for s in decl.severities) if decl.severities else None,
            description=decl.description,
            workflow=workflow.metadata.name,
        )
        for decl in workflow.metadata.finding_types or []
    }


def allowed(workflow: Workflow | None) -> dict[str, FindingTypeView]:
    """Every type a work item of `workflow` may be filed under: the registry's, then the workflow's."""
    found = {name: FindingTypeView(name=name) for name in sorted(FINDING_TYPES.known())}
    found.update(declared_by(workflow))
    return found


def title_of(workflow: Workflow | None, finding_type: str) -> str | None:
    """The title `workflow` gives `finding_type`; `None` when it declares none."""
    view = declared_by(workflow).get(finding_type)
    return view.title if view else None


def refusal(workflow: Workflow | None, finding_type: str, severity: str | None = None) -> str | None:
    """Why a finding of this type and severity cannot be filed under `workflow`, in words for the person
    who sent it; `None` when it can."""
    types = allowed(workflow)
    where = f"workflow `{workflow.metadata.name}`" if workflow is not None else "the default workflow"
    view = types.get(finding_type)
    if view is None:
        return (
            f"unknown finding type `{finding_type}` for {where}: it is not one the core, a plugin or the "
            f"workflow declares (allowed: {', '.join(sorted(types))}); a workflow declares its own in "
            "`metadata.finding_types`"
        )
    if severity is not None and view.severities is not None and severity not in view.severities:
        return (
            f"severity `{severity}` is not allowed for the finding type `{finding_type}` of {where} "
            f"(allowed: {', '.join(view.severities)})"
        )
    return None
