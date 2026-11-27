# SPDX-License-Identifier: Apache-2.0
"""The plugins of an organisation (ADR 0093, S43-04, S43-05): list, install, upgrade, uninstall, and the
second approval a plugin that brings a server waits for.

Any member reads the list and the requests waiting; installing, upgrading, uninstalling and approving are
the organisation administrator's (ADR 0074, `CatalogueAction.INSTALL`). The plugin itself comes from the
deployment's `PluginSupply` (`services/plugin_supply.py`): its sources and their trust (S43-03).

A plugin that brings a server is not installed on one administrator's word (ADR 0050): the install or
upgrade is checked in full, recorded, and answered 202 `pending_approval`; it happens when ANOTHER
administrator approves (`POST /orgs/{org}/plugin-approvals/{id}/approve`), and only if the source still
serves the content that was checked.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response, status
from fastapi.responses import JSONResponse
from tranibor_core.plugins import Plugin, PluginError

from ..db.models import OrgConnector, OrgPlugin, OrgPluginApproval
from ..decision_session import PLUGIN_INSTALLS, require_person_session
from ..deps import Config, Db, Me
from ..errors import ApiError, conflict, forbidden, not_found, unprocessable
from ..schemas.plugins import (
    PLUGIN_NAME,
    InstalledPlugin,
    PluginApprovalDto,
    PluginApprovalList,
    PluginInstall,
    PluginInstallPending,
    PluginKept,
    PluginList,
    PluginProfile,
    PluginProfileImplementation,
    PluginProfileList,
    PluginProfileTool,
    PluginServerInstance,
    PluginServerInstanceCreate,
    PluginServerInstanceList,
    PluginUninstallResult,
    PluginUpgrade,
    PluginUpgradeResult,
)
from ..services import plugin_approvals as approvals
from ..services import plugin_installer as installer
from ..services import plugin_profile_view, plugin_servers
from ..services.catalogue_rights import CatalogueAction, decide
from ..services.plugin_sources import assess_plugin
from ..services.plugin_supply import (
    PluginNotFoundError,
    PluginVersionUnsupportedError,
    SourceError,
    plugin_supply,
)

router = APIRouter(tags=["agents"])

Name = Annotated[str, Path(pattern=PLUGIN_NAME)]
ServerName = Annotated[str, Path(pattern=PLUGIN_NAME)]
InstanceName = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{1,47}$")]
ApprovalId = Annotated[str, Path(min_length=1, max_length=64)]
SUPPLY_ERRORS = (PluginNotFoundError, PluginVersionUnsupportedError, PluginError, SourceError)
PENDING = {
    status.HTTP_202_ACCEPTED: {"model": PluginInstallPending, "description": "Waiting for a second approval"}
}


def _may_see(principal: Me, org: str) -> None:
    if not decide(principal, org, CatalogueAction.SEE).allowed:
        raise forbidden(f"the plugins of {org} are visible to its members")


def _may_install(principal: Me, org: str) -> None:
    if not decide(principal, org, CatalogueAction.INSTALL).allowed:
        raise forbidden(f"plugins are installed, upgraded and uninstalled by the administrator of {org}")


def _supply_error(error: Exception) -> ApiError:
    if isinstance(error, PluginNotFoundError):
        return not_found("Plugin", str(error))
    if isinstance(error, SourceError):
        if error.check in {"not_found", "version"}:
            return not_found("Plugin", f"{error.check}: {error.message}")
        # The contract: a refusal of the gate is a 422 naming the check.
        return unprocessable(
            f"the plugin is refused by the `{error.check}` check",
            [{"loc": ["plugin", error.check], "msg": error.message}],
        )
    if isinstance(error, PluginError):
        return unprocessable(
            "the plugin is not valid", [{"loc": ["plugin"], "msg": p} for p in error.problems]
        )
    return unprocessable(str(error))


def _pending(row: OrgPluginApproval) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=approvals.pending_body(row))


@router.get("/orgs/{org}/plugins", response_model=PluginList, operation_id="listPlugins")
async def list_plugins(org: str, session: Db, principal: Me, settings: Config) -> PluginList:
    _may_see(principal, org)
    org_id = await installer.org_id_of(session, org)
    supply = plugin_supply(settings)
    return PluginList(
        items=[
            await installer.to_dto(r, supply, wait=False) for r in await installer.installed(session, org_id)
        ]
    )


@router.post(
    "/orgs/{org}/plugins/install",
    response_model=InstalledPlugin,
    status_code=status.HTTP_201_CREATED,
    operation_id="installPlugin",
    responses=PENDING,  # type: ignore[arg-type]
)
async def install_plugin(
    org: str, body: PluginInstall, session: Db, principal: Me, settings: Config
) -> InstalledPlugin | JSONResponse:
    _may_install(principal, org)
    org_id = await installer.org_id_of(session, org)
    supply = plugin_supply(settings)
    try:
        async with supply.obtain(body.source, body.name, body.version) as plugin:
            installed = await installer.installed_row(session, org_id, plugin.name)
            if installed is not None:
                if installed.digest == plugin.digest:
                    raise conflict(f"`{plugin.name}` {installed.version} is already installed in {org}")
                raise conflict(
                    f"`{plugin.name}` {installed.version} is installed: upgrade it to {plugin.version}"
                )
            gate = assess_plugin(plugin)
            if gate.needs_second_approval:
                await installer.check(session, org_id, plugin, settings, replacing=None)
                row, _ = await approvals.request(
                    session, org_id, approvals.INSTALL, body.source, plugin, gate.reasons, principal
                )
                return _pending(row)
            installed, _ = await installer.install(session, org_id, plugin, body.source, settings, principal)
    except SUPPLY_ERRORS as error:
        raise _supply_error(error) from error
    return await installer.to_dto(installed, supply)


@router.post(
    "/orgs/{org}/plugins/{name}/upgrade",
    response_model=PluginUpgradeResult,
    operation_id="upgradePlugin",
    responses=PENDING,  # type: ignore[arg-type]
)
async def upgrade_plugin(
    org: str, name: Name, session: Db, principal: Me, settings: Config, body: PluginUpgrade | None = None
) -> PluginUpgradeResult | JSONResponse:
    _may_install(principal, org)
    org_id = await installer.org_id_of(session, org)
    row = await installer.installed_one(session, org_id, name)
    supply = plugin_supply(settings)
    try:
        async with supply.obtain(row.source, name, body.version if body else None) as plugin:
            gate = assess_plugin(plugin)
            if plugin.digest != row.digest and gate.needs_second_approval:
                if plugin.name != row.name:
                    raise unprocessable(f"the source served `{plugin.name}` for `{row.name}`")
                await installer.check(session, org_id, plugin, settings, replacing=row)
                request, _ = await approvals.request(
                    session, org_id, approvals.UPGRADE, row.source, plugin, gate.reasons, principal
                )
                return _pending(request)
            previous, offered = await installer.upgrade(session, org_id, row, plugin, settings, principal)
    except SUPPLY_ERRORS as error:
        raise _supply_error(error) from error
    return PluginUpgradeResult(
        plugin=await installer.to_dto(row, supply), previous_version=previous, offered=offered
    )


@router.delete(
    "/orgs/{org}/plugins/{name}",
    response_model=PluginUninstallResult,
    operation_id="uninstallPlugin",
)
async def uninstall_plugin(
    org: str, name: Name, session: Db, principal: Me, settings: Config
) -> PluginUninstallResult:
    _may_install(principal, org)
    org_id = await installer.org_id_of(session, org)
    row = await installer.installed_one(session, org_id, name)
    version = row.version
    outcome = await installer.uninstall(session, org_id, row, principal, settings)
    await approvals.forget(session, org_id, name)
    return PluginUninstallResult(
        name=name,
        version=version,
        removed=PluginKept.model_validate(outcome["removed"]),
        kept=PluginKept.model_validate(outcome["kept"]),
    )


@router.get(
    "/orgs/{org}/plugin-profiles", response_model=PluginProfileList, operation_id="listPluginProfiles"
)
async def list_plugin_profiles(org: str, session: Db, principal: Me) -> PluginProfileList:
    """The profiles the installed plugins declare, with their implementations and instances (ADR 0224)."""
    _may_see(principal, org)
    org_id = await installer.org_id_of(session, org)
    return PluginProfileList(
        items=[
            PluginProfile(
                **{k: v for k, v in p.items() if k not in ("tools", "implementations")},
                tools=[PluginProfileTool(**t) for t in p["tools"]],
                implementations=[
                    PluginProfileImplementation(
                        plugin=i["plugin"],
                        server=i["server"],
                        instances=[_instance_dto(c) for c in i["instances"]],
                    )
                    for i in p["implementations"]
                ],
            )
            for p in await plugin_profile_view.profiles_of(session, org_id)
        ]
    )


# ───────────────────────────── the instances of a server (ADR 0224 §3) ─────────────────────────────


def _instance_dto(row: OrgConnector) -> PluginServerInstance:
    return PluginServerInstance(
        name=row.name,
        server=str(plugin_servers.instance_of(row)),
        default=plugin_servers.is_default(row),
        state=plugin_servers.state_of(row),
        status=row.status,
        last_error=row.last_error,
    )


async def _server_doc(session: Db, org_id: str, plugin: str, server: str) -> tuple[OrgPlugin, dict[str, Any]]:
    row = await installer.installed_one(session, org_id, plugin)
    doc = (row.details.get("servers") or {}).get(server)
    if not isinstance(doc, dict):
        raise not_found("Server", f"{plugin}/{server}")
    return row, doc


@router.get(
    "/orgs/{org}/plugins/{name}/servers/{server}/instances",
    response_model=PluginServerInstanceList,
    operation_id="listPluginServerInstances",
)
async def list_server_instances(
    org: str, name: Name, server: ServerName, session: Db, principal: Me
) -> PluginServerInstanceList:
    _may_see(principal, org)
    org_id = await installer.org_id_of(session, org)
    await _server_doc(session, org_id, name, server)
    found = await plugin_servers.instances_of(session, org_id, name, server)
    return PluginServerInstanceList(items=[_instance_dto(r) for r in found])


@router.post(
    "/orgs/{org}/plugins/{name}/servers/{server}/instances",
    response_model=PluginServerInstance,
    status_code=status.HTTP_201_CREATED,
    operation_id="createPluginServerInstance",
)
async def create_server_instance(
    org: str,
    name: Name,
    server: ServerName,
    body: PluginServerInstanceCreate,
    session: Db,
    principal: Me,
    settings: Config,
) -> PluginServerInstance:
    _may_install(principal, org)
    org_id = await installer.org_id_of(session, org)
    row, doc = await _server_doc(session, org_id, name, server)
    created = await plugin_servers.add_instance(
        session, org_id, row.name, row.version, doc, body.name, principal, settings
    )
    return _instance_dto(created)


@router.delete(
    "/orgs/{org}/plugins/{name}/servers/{server}/instances/{instance}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deletePluginServerInstance",
)
async def delete_server_instance(
    org: str,
    name: Name,
    server: ServerName,
    instance: InstanceName,
    session: Db,
    principal: Me,
    settings: Config,
) -> Response:
    _may_install(principal, org)
    org_id = await installer.org_id_of(session, org)
    await _server_doc(session, org_id, name, server)
    found = [
        r for r in await plugin_servers.instances_of(session, org_id, name, server) if r.name == instance
    ]
    if not found:
        raise not_found("Instance", f"{name}/{server}/{instance}")
    await plugin_servers.remove_instance(session, found[0], principal, settings)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ───────────────────────────── the second approval (ADR 0050) ─────────────────────────────


@router.get(
    "/orgs/{org}/plugin-approvals", response_model=PluginApprovalList, operation_id="listPluginApprovals"
)
async def list_plugin_approvals(org: str, session: Db, principal: Me) -> PluginApprovalList:
    _may_see(principal, org)
    org_id = await installer.org_id_of(session, org)
    return PluginApprovalList(items=[approvals.to_dto(r) for r in await approvals.listed(session, org_id)])


async def _approved_plugin(
    session: Db, org_id: str, request: OrgPluginApproval, plugin: Plugin, settings: Config, principal: Me
) -> None:
    if plugin.digest != request.digest:
        raise conflict(
            f"the source now serves `{request.name}` {plugin.version} as {plugin.digest}, not the "
            f"{request.digest} that was checked: ask for the install again"
        )
    if request.action == approvals.INSTALL:
        if await installer.installed_row(session, org_id, request.name) is not None:
            raise conflict(f"`{request.name}` was installed meanwhile")
        await installer.install(
            session, org_id, plugin, request.source, settings, principal,
            default=request.is_default, installed_by=request.requested_by,
        )  # fmt: skip
        return
    row = await installer.installed_one(session, org_id, request.name)
    await installer.upgrade(session, org_id, row, plugin, settings, principal)


@router.post(
    "/orgs/{org}/plugin-approvals/{approval_id}/approve",
    response_model=PluginApprovalDto,
    operation_id="approvePluginInstall",
)
async def approve_plugin_install(
    org: str, approval_id: ApprovalId, session: Db, principal: Me, settings: Config
) -> PluginApprovalDto:
    """A second administrator approves: the plugin is obtained again at the exact version, and installed
    (or upgraded) only if its content is the one that was checked."""
    require_person_session(principal, "approving a plugin install", settings=settings, legacy=PLUGIN_INSTALLS)
    _may_install(principal, org)
    org_id = await installer.org_id_of(session, org)
    request = await approvals.pending_one(session, org_id, approval_id)
    sole_admin = await approvals.check_second_person(session, request, principal)
    supply = plugin_supply(settings)
    try:
        async with supply.obtain(request.source, request.name, request.version) as plugin:
            await _approved_plugin(session, org_id, request, plugin, settings, principal)
    except SUPPLY_ERRORS as error:
        raise _supply_error(error) from error
    await approvals.mark_approved(session, request, principal, sole_admin=sole_admin)
    return approvals.to_dto(request)
