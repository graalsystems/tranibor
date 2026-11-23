"""The roles' former names leave the rows (ADR 0181 C1, windows closed on 2026-11-23).

`memberships.role` and the approver roles of a proposed action are rewritten from `developer` and
`release_captain` to `contributor` and `approver`; every other value is left alone; a second run changes
nothing. Played on SQLite here.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from tranibor_api.config import get_settings

API = pathlib.Path(__file__).resolve().parents[1]
BEFORE = "a9c1e3b5d7f0"
AFTER = "c1a3e5f7b9d2"
NOW = "2026-11-23 09:00:00"


def _config(url: str) -> Config:
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "src" / "tranibor_api" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _migration() -> Any:
    path = API / "src/tranibor_api/migrations/versions/20261123_0900_roles_neutral_names.py"
    spec = importlib.util.spec_from_file_location("roles_neutral_names", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _approval(*roles: str) -> str:
    return json.dumps({"approvers": [{"role": role, "min": 1} for role in roles], "step_up_minutes": 10})


def test_the_former_roles_are_rewritten_idempotently(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "roles.db"
    monkeypatch.setenv("TRANIBOR_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    try:
        command.upgrade(_config(url), BEFORE)
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO organizations (id, slug, name, settings) VALUES ('o1', 'o1', 'o1', '{}')")
            )
            connection.execute(
                text(
                    "INSERT INTO projects (id, org_id, slug, name, config, status, provision_state) "
                    "VALUES ('p1', 'o1', 'p1', 'p1', '{}', 'active', '{}')"
                )
            )
            for user, role in (
                ("u1", "developer"),
                ("u2", "release_captain"),
                ("u3", "org_admin"),
                ("u4", "viewer"),
            ):
                connection.execute(
                    text("INSERT INTO users (id, email, display_name, is_service) VALUES (:u, :e, :u, 0)"),
                    {"u": user, "e": f"{user}@x.dev"},
                )
                connection.execute(
                    text("INSERT INTO memberships (id, user_id, org_id, role) VALUES (:i, :u, 'o1', :r)"),
                    {"i": f"m-{user}", "u": user, "r": role},
                )
            for ident, approval in (
                ("a1", _approval("developer", "project_owner")),
                ("a2", _approval("release_captain")),
                ("a3", _approval("org_admin")),
                ("a4", "{}"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO actions (id, org_id, project_id, origin, kind, title, params, effects, "
                        "proposed_by, approval, decisions, status, created_at, updated_at) VALUES (:i, 'o1', "
                        "'p1', 'agent', 'k', 't', '{}', '[]', '{}', :a, '[]', 'pending_approval', :n, :n)"
                    ),
                    {"i": ident, "a": approval, "n": NOW},
                )
            connection.execute(
                text(
                    "INSERT INTO org_saml_idps (id, org_id, idp_entity_id, sso_url, certificate_pem, "
                    "group_map, domains, created_at, updated_at) "
                    "VALUES ('s1', 'o1', 'e', 'u', 'c', :g, '[]', :n, :n)"
                ),
                {
                    "g": json.dumps({"Devs": "developers", "Caps": "release-captains", "Ops": "org-admins"}),
                    "n": NOW,
                },
            )
        command.upgrade(_config(url), AFTER)
        with engine.connect() as connection:
            roles = dict(connection.execute(text("SELECT user_id, role FROM memberships")).all())
            approvals = {
                ident: [a["role"] for a in json.loads(value).get("approvers", [])]
                for ident, value in connection.execute(text("SELECT id, approval FROM actions")).all()
            }
            group_map = json.loads(
                connection.execute(text("SELECT group_map FROM org_saml_idps")).scalar_one()
            )
        assert group_map == {"Devs": "contributors", "Caps": "approvers", "Ops": "org-admins"}
        assert roles == {"u1": "contributor", "u2": "approver", "u3": "org_admin", "u4": "viewer"}
        assert approvals == {
            "a1": ["contributor", "project_owner"], "a2": ["approver"], "a3": ["org_admin"], "a4": [],
        }  # fmt: skip
        with engine.begin() as connection:
            assert _migration().upgrade_rows(connection) == 0, "a second run changes nothing"
    finally:
        engine.dispose()
        get_settings.cache_clear()
