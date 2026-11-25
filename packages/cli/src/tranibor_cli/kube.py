# SPDX-License-Identifier: Apache-2.0
"""The Kubernetes API, as the operator's own `kubectl` reaches it: the kubeconfig's current context.

The CLI ships as one native file and does not carry a Kubernetes SDK; the platform's executors do not
either (`tranibor_adapters.executor.tekton.KubernetesClient`: httpx and the REST paths, nothing
more). This module is the same approach, plus what a pod's service account never needed: reading a
kubeconfig. It understands what the clusters the platform meets put there: a bearer token (or a
token file), a client certificate (kind, k3s), basic credentials, and an `exec` credential plugin
(EKS's `aws eks get-token`, GKE's `gke-gcloud-auth-plugin`, AKS's `kubelogin`). A legacy
`auth-provider` is refused with what to do instead: `kubectl proxy`, and `--server` at its address.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

#: The `ExecCredential` versions a credential plugin may answer with.
EXEC_API_VERSIONS = ("client.authentication.k8s.io/v1", "client.authentication.k8s.io/v1beta1")


class KubeConfigError(Exception):
    """The kubeconfig cannot give a connection: what is missing, said for the operator."""


class KubeApiError(Exception):
    """The API server refused a request (any status from 400 on, but 404, which reads as `None`)."""

    def __init__(self, method: str, path: str, status: int, text: str) -> None:
        super().__init__(f"{method} {path} → {status}: {text[:300]}")
        self.status = status


@dataclass(frozen=True)
class Connection:
    """Where the API server is and how to authenticate to it."""

    server: str
    context: str
    verify: ssl.SSLContext | bool
    headers: Mapping[str, str]
    proxy: str | None = None


def kubeconfig_paths(explicit: str | None = None) -> list[Path]:
    """The files `kubectl` reads, in its order: `--kubeconfig`, else `$KUBECONFIG`, else `~/.kube/config`."""
    if explicit:
        return [Path(explicit).expanduser()]
    listed = [Path(part).expanduser() for part in os.environ.get("KUBECONFIG", "").split(os.pathsep) if part]
    return listed or [Path.home() / ".kube" / "config"]


def _merge(paths: list[Path]) -> tuple[dict[str, Any], dict[str, Path]]:
    """`kubectl`'s merge: the first file that names a context, cluster or user wins, and the first
    `current-context` too. Returns the merged view and the file each named entry came from (a
    relative certificate path is relative to its own file)."""
    merged: dict[str, Any] = {"clusters": {}, "users": {}, "contexts": {}, "current-context": ""}
    origin: dict[str, Path] = {}
    found = False
    for path in paths:
        if not path.is_file():
            continue
        found = True
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not merged["current-context"]:
            merged["current-context"] = str(document.get("current-context") or "")
        for section, singular in (("clusters", "cluster"), ("users", "user"), ("contexts", "context")):
            for entry in document.get(section) or []:
                name = str(entry.get("name") or "")
                if name and name not in merged[section]:
                    merged[section][name] = entry.get(singular) or {}
                    origin[f"{section}/{name}"] = path
    if not found:
        raise KubeConfigError(
            f"no kubeconfig found ({', '.join(str(path) for path in paths)}): point --kubeconfig or "
            "KUBECONFIG at the cluster's, or give --server"
        )
    return merged, origin


def _relative(value: str, base: Path | None) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() or base is None else base.parent / path)


def _decode(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


async def _exec_credential(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Run an `exec` credential plugin and return its `status` (`token`, or a client certificate)."""
    command = str(spec.get("command") or "")
    if not command:
        raise KubeConfigError("the kubeconfig's exec credential plugin names no command")
    api_version = str(spec.get("apiVersion") or EXEC_API_VERSIONS[0])
    env = dict(os.environ)
    for item in spec.get("env") or []:
        env[str(item.get("name"))] = str(item.get("value", ""))
    env["KUBERNETES_EXEC_INFO"] = json.dumps(
        {"apiVersion": api_version, "kind": "ExecCredential", "spec": {"interactive": False}}
    )
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *[str(arg) for arg in spec.get("args") or []],
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        hint = f" ({spec['installHint']})" if spec.get("installHint") else ""
        raise KubeConfigError(f"the credential plugin `{command}` cannot run: {exc}{hint}") from exc
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise KubeConfigError(
            f"the credential plugin `{command}` exited {process.returncode}: "
            f"{stderr.decode(errors='replace').strip()[:300]}"
        )
    try:
        status = json.loads(stdout.decode()).get("status") or {}
    except (ValueError, AttributeError) as exc:
        raise KubeConfigError(f"the credential plugin `{command}` did not print an ExecCredential") from exc
    if not isinstance(status, dict):
        raise KubeConfigError(f"the credential plugin `{command}` printed no `status`")
    return status


def _load_client_certificate(context: ssl.SSLContext, certificate: bytes, key: bytes) -> None:
    """`ssl` reads a client certificate from files only: written to a private directory, loaded, gone."""
    with tempfile.TemporaryDirectory(prefix="tranibor-kube-") as directory:
        cert_path = Path(directory) / "client.crt"
        key_path = Path(directory) / "client.key"
        for path, content in ((cert_path, certificate), (key_path, key)):
            path.touch(mode=0o600)
            path.write_bytes(content)
        context.load_cert_chain(cert_path, key_path)


def _tls(cluster: Mapping[str, Any], origin: Path | None) -> ssl.SSLContext:
    """The cluster's trust: its CA (inline or a file), or none when the kubeconfig says to skip it."""
    if cluster.get("insecure-skip-tls-verify"):
        tls = ssl.create_default_context()
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        return tls
    if cluster.get("certificate-authority-data"):
        return ssl.create_default_context(cadata=_decode(cluster["certificate-authority-data"]).decode())
    if cluster.get("certificate-authority"):
        return ssl.create_default_context(cafile=_relative(cluster["certificate-authority"], origin))
    return ssl.create_default_context()


def _auth_headers(user: Mapping[str, Any], origin: Path | None, status: Mapping[str, Any]) -> dict[str, str]:
    """A bearer token (the plugin's, the kubeconfig's, a token file's), else basic credentials."""
    token = status.get("token") or user.get("token")
    if not token and user.get("tokenFile"):
        token = Path(_relative(user["tokenFile"], origin)).read_text(encoding="utf-8").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    if user.get("username"):
        pair = f"{user['username']}:{user.get('password', '')}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode()}
    return {}


def _client_certificate(
    user: Mapping[str, Any], origin: Path | None, status: Mapping[str, Any]
) -> tuple[bytes, bytes] | None:
    """The client certificate and key: the plugin's, inline, or files; `None` without one."""
    if status.get("clientCertificateData") and status.get("clientKeyData"):
        return str(status["clientCertificateData"]).encode(), str(status["clientKeyData"]).encode()
    if user.get("client-certificate-data") and user.get("client-key-data"):
        return _decode(user["client-certificate-data"]), _decode(user["client-key-data"])
    if user.get("client-certificate") and user.get("client-key"):
        return (
            Path(_relative(user["client-certificate"], origin)).read_bytes(),
            Path(_relative(user["client-key"], origin)).read_bytes(),
        )
    return None


async def connection(
    kubeconfig: str | None = None, context: str | None = None, server: str | None = None
) -> Connection:
    """The connection to the API server.

    `server` alone (a `kubectl proxy` address, `http://127.0.0.1:8001`) bypasses the kubeconfig: the
    proxy authenticates. Otherwise the kubeconfig's `context`, or its current one."""
    if server:
        return Connection(server=server.rstrip("/"), context=server, verify=True, headers={})
    merged, origin = _merge(kubeconfig_paths(kubeconfig))
    name = context or merged["current-context"]
    if not name:
        raise KubeConfigError("the kubeconfig has no current context: name one with --context")
    ctx = merged["contexts"].get(name)
    if ctx is None:
        known = ", ".join(sorted(merged["contexts"])) or "none"
        raise KubeConfigError(f"no context `{name}` in the kubeconfig (it has: {known})")
    cluster_name = str(ctx.get("cluster") or "")
    cluster = merged["clusters"].get(cluster_name)
    if cluster is None or not cluster.get("server"):
        raise KubeConfigError(f"the context `{name}` names the cluster `{cluster_name}`, which has no server")
    user_name = str(ctx.get("user") or "")
    user = merged["users"].get(user_name) or {}
    user_file = origin.get(f"users/{user_name}")
    if user.get("auth-provider"):
        raise KubeConfigError(
            f"the user `{user_name}` authenticates through a legacy auth-provider, which this command "
            "does not run: start `kubectl proxy` and pass --server http://127.0.0.1:8001"
        )
    status = await _exec_credential(user["exec"]) if user.get("exec") else {}
    tls = _tls(cluster, origin.get(f"clusters/{cluster_name}"))
    pair = _client_certificate(user, user_file, status)
    if pair is not None:
        _load_client_certificate(tls, *pair)
    return Connection(
        server=str(cluster["server"]).rstrip("/"),
        context=name,
        verify=tls,
        headers=_auth_headers(user, user_file, status),
        proxy=str(cluster["proxy-url"]) if cluster.get("proxy-url") else None,
    )


class KubeApi:
    """A minimal REST client of the API server: JSON in, JSON out, a 404 is `None`."""

    def __init__(self, conn: Connection, *, client: httpx.AsyncClient | None = None) -> None:
        self.connection = conn
        self._client = client or httpx.AsyncClient(timeout=30.0, verify=conn.verify, proxy=conn.proxy)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Accept": "application/json", **self.connection.headers}
        response = await self._client.request(
            method, f"{self.connection.server}{path}", headers=headers, **kwargs
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise KubeApiError(method, path, response.status_code, response.text)
        if not response.content:
            return {}
        if "json" not in response.headers.get("content-type", ""):
            return response.text  # a pod's logs
        return response.json()

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def create(self, path: str, body: Mapping[str, Any]) -> Any:
        return await self.request("POST", path, json=dict(body))

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)
