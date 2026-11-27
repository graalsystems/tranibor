# SPDX-License-Identifier: Apache-2.0
"""S87-13: the embedded Keycloak is admitted to the embedded PostgreSQL that holds its realm.

The all-in-one profile walls the PostgreSQL to a list of clients; Keycloak was not on it. Where
NetworkPolicies are enforced (AKS with Cilium, the first real install of the profile) Keycloak never
obtained a JDBC connection and the platform had no identity provider. The render test of the profile
did not see it: it checks the objects, not who may reach whom.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "tranibor"


def _postgresql_clients(*extra: str) -> set[str]:
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
    for doc in yaml.safe_load_all(rendered):
        if (
            doc
            and doc.get("kind") == "NetworkPolicy"
            and doc["metadata"]["name"] == "tranibor-postgresql-ingress"
        ):
            (expression,) = doc["spec"]["ingress"][0]["from"][0]["podSelector"]["matchExpressions"]
            return set(expression["values"])
    raise AssertionError("no tranibor-postgresql-ingress policy rendered")


def test_the_embedded_keycloak_may_reach_the_embedded_postgresql() -> None:
    assert "tranibor-keycloak" in _postgresql_clients()


def test_a_keycloak_on_its_own_database_is_not_admitted() -> None:
    assert "tranibor-keycloak" not in _postgresql_clients(
        "--set", "tranibor-keycloak.database.host=db.example"
    )
