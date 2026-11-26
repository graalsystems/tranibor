"""The application implements exactly the contract: the same paths, the same operations."""

from __future__ import annotations

from typing import Any

import tranibor_contracts as contracts

PREFIX = "/api/v1"

# Contract-first (ADR 0001): operations the contract declares before the API serves them. Each entry
# names the story that implements it; an entry the API does serve must be removed from here.
CONTRACT_ONLY: dict[str, str] = {
    # S87-08 (ADR 0228 §4): a personal installation's sign-in, by door.
    "getAuthMethods": "S87-12",
    "signInAsOwner": "S87-12",
    "createMyDevicePairing": "S87-13",
    "exchangeDevicePairing": "S87-13",
    "listMyPasskeys": "S87-14",
    "registerMyPasskey": "S87-14",
    "startMyPasskeyRegistration": "S87-14",
    "deleteMyPasskey": "S87-14",
    "startPasskeySignIn": "S87-14",
    "signInWithPasskey": "S87-14",
    # S87-08 (ADR 0226 §2): a person's own runner, enrolled and approved by its owner only.
    "listMyRunners": "S87-15",
    "createMyRunnerEnrolment": "S87-15",
    "approveMyRunnerInstance": "S87-15",
    "pauseMyRunner": "S87-15",
    "resumeMyRunner": "S87-15",
    "revokeMyRunner": "S87-15",
    # S87-08 (ADR 0226 §11): a runner-placed turn's answer, streamed from its deltas.
    "postAgentSessionTurnDeltas": "S87-37",
}
CONTRACT_ONLY_PATHS: set[str] = {
    "/auth/methods",
    "/auth/owner",
    "/me/device-pairings",
    "/auth/device/session",
    "/me/passkeys",
    "/me/passkeys/options",
    "/me/passkeys/{id}",
    "/auth/passkey/options",
    "/auth/passkey",
    "/me/runners",
    "/me/runners/{org}/enrolments",
    "/me/runners/instances/{instance}/approval",
    "/me/runners/{org}/pause",
    "/me/runners/{org}/resume",
    "/me/runners/{org}/revocation",
    "/internal/agent-sessions/{id}/turns/{turn_id}/deltas",
}
VERBS = {"get", "post", "put", "patch", "delete"}


def _absent_plugins() -> set[str]:
    """The plugins `openapi.yaml` marks operations for (`x-plugin`) whose extension this environment lacks.

    Their operations are served by the extension (ADR 0181, round D: the release train's routes are the
    `software-delivery` plugin's): where it is installed they are served like the core's, where it is not
    (the `core-without-plugins` job) none of them is.
    """
    from importlib import metadata

    installed = {point.name for point in metadata.entry_points(group="choregos.plugins")}
    marked = {
        str(op.get("x-plugin") or item.get("x-plugin"))
        for item in contracts.load_openapi()["paths"].values()
        for verb, op in item.items()
        if verb in VERBS and (op.get("x-plugin") or item.get("x-plugin"))
    }
    return marked - installed


def _plugin_absent_operations() -> tuple[set[str], set[str]]:
    """(operation ids, paths) of the absent plugins' operations; a path is theirs if all of it is."""
    absent = _absent_plugins()
    ids: set[str] = set()
    paths: set[str] = set()
    for path, operations in contracts.load_openapi()["paths"].items():
        verbs = [op for verb, op in operations.items() if verb in VERBS]
        theirs = [op for op in verbs if (op.get("x-plugin") or operations.get("x-plugin")) in absent]
        ids |= {op["operationId"] for op in theirs}
        if verbs and len(theirs) == len(verbs):
            paths.add(path)
    return ids, paths


def _app_paths(spec: dict[str, Any]) -> set[str]:
    out = set()
    for path in spec["paths"]:
        if path.startswith(PREFIX):
            out.add(path.removeprefix(PREFIX))
        else:
            out.add(path)
    return out


async def test_paths_match_contract(app: Any) -> None:
    spec = app.openapi()
    _, absent_paths = _plugin_absent_operations()
    declared = set(contracts.load_openapi()["paths"]) - absent_paths
    implemented = _app_paths(spec)
    missing = declared - implemented - CONTRACT_ONLY_PATHS
    extra = implemented - declared
    assert not missing, f"contract paths not implemented: {sorted(missing)}"
    assert not extra, f"implemented paths outside the contract: {sorted(extra)}"


async def test_operation_ids_match_contract(app: Any) -> None:
    spec = app.openapi()
    absent_ids, _ = _plugin_absent_operations()
    declared = {
        op["operationId"]
        for path in contracts.load_openapi()["paths"].values()
        for verb, op in path.items()
        if verb in VERBS
    } - absent_ids
    implemented = {
        op["operationId"]
        for path in spec["paths"].values()
        for verb, op in path.items()
        if verb in {"get", "post", "put", "patch", "delete"} and "operationId" in op
    }
    assert declared - set(CONTRACT_ONLY) == implemented, (
        f"manquants : {sorted(declared - implemented - set(CONTRACT_ONLY))} ; "
        f"en trop : {sorted(implemented - declared)}"
    )
    served = sorted(set(CONTRACT_ONLY) & implemented)
    assert not served, f"served now, remove from CONTRACT_ONLY: {served}"


async def test_health_endpoints(client: Any) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200
