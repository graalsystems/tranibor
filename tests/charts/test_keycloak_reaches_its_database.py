# SPDX-License-Identifier: Apache-2.0
"""S87-13, S87-09: the embedded Keycloak is admitted to the embedded PostgreSQL that holds its realm.

The all-in-one profile walls the PostgreSQL to a list of clients; Keycloak was not on it. Where
NetworkPolicies are enforced (AKS with Cilium, the first real install of the profile) Keycloak never
obtained a JDBC connection and the platform had no identity provider. The render test of the profile
did not see it: it checks the objects, not who may reach whom.

The first fix put a NAME on the list, `tranibor-keycloak`, and its test checked that name; the pods
carry another label (ADR 0226 R7 keeps the pods' labels), and on kind with Calico Keycloak again never
got a connection (S87-09). The policy is now read against the label the Keycloak pods really carry.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "tranibor"
NAME = "app.kubernetes.io/name"


def _render(*extra: str) -> list[dict[str, Any]]:
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "tranibor",
            str(CHART),
            "-n",
            "tranibor",
            "-f",
            str(CHART / "values" / "all-in-one.yaml"),
            "--set",
            "global.bootstrap.org=proof",
            "--set",
            "global.bootstrap.admins=admin@proof.example",
            *extra,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _postgresql_clients(documents: list[dict[str, Any]]) -> set[str]:
    for doc in documents:
        if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "tranibor-postgresql-ingress":
            (expression,) = doc["spec"]["ingress"][0]["from"][0]["podSelector"]["matchExpressions"]
            assert expression["key"] == NAME and expression["operator"] == "In"
            return set(expression["values"])
    raise AssertionError("no tranibor-postgresql-ingress policy rendered")


def _keycloak_pod_label(documents: list[dict[str, Any]]) -> str:
    (deployment,) = [
        doc
        for doc in documents
        if doc["kind"] == "Deployment"
        and any(c["name"] == "keycloak" for c in doc["spec"]["template"]["spec"]["containers"])
    ]
    return str(deployment["spec"]["template"]["metadata"]["labels"][NAME])


def test_the_embedded_keycloak_pods_may_reach_the_embedded_postgresql() -> None:
    documents = _render()
    assert _keycloak_pod_label(documents) in _postgresql_clients(documents)


def test_a_keycloak_on_its_own_database_is_not_admitted() -> None:
    documents = _render("--set", "tranibor-keycloak.database.host=db.example")
    assert _keycloak_pod_label(documents) not in _postgresql_clients(documents)
