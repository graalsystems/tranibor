"""The optional `choregos-websearch` subchart (S80-17, ADR 0195): SearXNG, a search-only Firecrawl and
its Redis, and the egress relay when asked. Off by default; names adoptable; secrets never inline."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "choregos"
ON = ("choregos-websearch.enabled=true",)
RELAY = (
    "choregos-websearch.relay.enabled=true",
    "choregos-websearch.relay.secretName=relay-secrets",
    "choregos-websearch.relay.organisation=acme",
)

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    command = ["helm", "template", "c", str(CHART), "-f", str(CHART / "values" / "local.yaml")]
    for item in sets:
        command += ["--set", item]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _docs(*sets: str) -> list[dict[str, Any]]:
    rendered = _render(*sets)
    assert rendered.returncode == 0, rendered.stderr
    return [d for d in yaml.safe_load_all(rendered.stdout) if d]


def _names(docs: list[dict[str, Any]], kind: str) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d.get("kind") == kind}


def test_off_by_default() -> None:
    docs = _docs()
    assert not {n for n in _names(docs, "Deployment") if "searxng" in n or "firecrawl" in n}


def test_search_only_without_relay() -> None:
    docs = _docs(*ON)
    assert {"c-searxng", "c-firecrawl", "c-firecrawl-redis"} <= _names(docs, "Deployment")
    assert "c-relay" not in _names(docs, "Deployment")
    firecrawl = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "c-firecrawl")
    container = firecrawl["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["node", "dist/src/index.js"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["SEARXNG_ENDPOINT"] == "http://c-searxng:8080"
    assert env["REDIS_URL"] == "redis://c-firecrawl-redis:6379"


def test_relay_needs_secret_and_organisation() -> None:
    assert "relay.secretName" in _render(*ON, "choregos-websearch.relay.enabled=true").stderr
    bad = _render(*ON, "choregos-websearch.relay.enabled=true", "choregos-websearch.relay.secretName=s")
    assert bad.returncode != 0 and "relay.organisation" in bad.stderr


def test_relay_reads_its_tokens_from_the_secret() -> None:
    docs = _docs(*ON, *RELAY)
    relay = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "c-relay")
    container = relay["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "tranibor_api.relay", "serve"]
    refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"] if "valueFrom" in e}
    assert refs["CHOREGOS_RELAY_API_TOKEN"] == {"name": "relay-secrets", "key": "api-token"}
    assert refs["CHOREGOS_RELAY_CLIENT_TOKEN"] == {"name": "relay-secrets", "key": "client-token"}
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "c-searxng-config")
    assert "c-relay:3128" in config["data"]["settings.yml"]
    assert "relay-secrets" not in config["data"]["settings.yml"]


def test_names_are_adoptable() -> None:
    docs = _docs(
        *ON, "choregos-websearch.names.firecrawl=firecrawl", "choregos-websearch.names.searxng=searxng"
    )
    assert {"firecrawl", "searxng"} <= _names(docs, "Service")
    firecrawl = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "firecrawl")
    env = {e["name"]: e["value"] for e in firecrawl["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["SEARXNG_ENDPOINT"] == "http://searxng:8080"


def test_pods_are_non_root_and_unprivileged() -> None:
    for doc in _docs(*ON, *RELAY):
        if doc["kind"] != "Deployment" or "websearch" not in doc["spec"]["template"]["metadata"][
            "labels"
        ].get("app.kubernetes.io/component", ""):
            continue
        spec = doc["spec"]["template"]["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True
        for container in spec["containers"] + spec.get("initContainers", []):
            assert container["securityContext"]["allowPrivilegeEscalation"] is False


def _settings(*sets: str) -> dict[str, Any]:
    docs = _docs(*ON, *sets)
    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "c-searxng-config")
    settings: dict[str, Any] = yaml.safe_load(config["data"]["settings.yml"])
    return settings


def test_every_listed_engine_is_kept_loaded_and_enabled() -> None:
    """S80-40 (#2183): upstream ships most general-web engines `disabled` (a query naming no engine, as
    Firecrawl's, never asks them) or `inactive` (not loaded). `keep_only` alone kept them listed and
    silent: each listed engine is now also enabled and loaded, the default list as any other."""
    defaults = yaml.safe_load((CHART / "charts" / "websearch" / "values.yaml").read_text())
    names = defaults["searxng"]["engines"]
    assert len(names) >= 3, "one engine that fails leaves the search with others"
    for relay in ((), RELAY):
        settings = _settings(*relay)
        assert settings["use_default_settings"]["engines"]["keep_only"] == names
        assert settings["engines"] == [{"name": n, "disabled": False, "inactive": False} for n in names]


def test_an_engine_entry_may_carry_its_own_settings() -> None:
    settings = _settings(
        "choregos-websearch.searxng.engines[0]=bing",
        "choregos-websearch.searxng.engines[1].name=mojeek",
        "choregos-websearch.searxng.engines[1].timeout=8",
        "choregos-websearch.searxng.engines[1].disabled=true",
    )
    assert settings["use_default_settings"]["engines"]["keep_only"] == ["bing", "mojeek"]
    assert settings["engines"] == [
        {"name": "bing", "disabled": False, "inactive": False},
        {"name": "mojeek", "timeout": 8, "disabled": True, "inactive": False},
    ]


def test_no_engine_or_a_nameless_one_is_refused() -> None:
    empty = _render(*ON, "choregos-websearch.searxng.engines=null")
    assert empty.returncode != 0 and "searxng.engines is empty" in empty.stderr
    nameless = _render(*ON, "choregos-websearch.searxng.engines[0].timeout=8")
    assert nameless.returncode != 0 and "a map with its `name`" in nameless.stderr
