"""The data migration that writes the stored documents' software delivery in their `delivery` sections
(ADR 0181 C2, the window closed on 2026-11-23; S83-04r).

Projects, policies (their JSON and their YAML text) and runs (their stage input and stage result) stored
under the former fields are rewritten, each to what the contracts read and write now; a document without
a former field is left as it is, a second pass changes nothing, and the downgrade does nothing (the
previous release reads the sections). Played on SQLite here, and on PostgreSQL when the suite has one.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any

import pytest
import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from tranibor_api.config import get_settings
from tranibor_contracts import Policy, ProjectConfig, StageInput, StageResult
from tranibor_core.policy import parse_policy

API = pathlib.Path(__file__).resolve().parents[1]
BEFORE = "c1a3e5f7b9d2"
AFTER = "13e34c50cb88"
MIGRATION = API / "src/tranibor_api/migrations/versions/20261123_1200_delivery_sections.py"

REPO = {"url": "https://github.com/acme/api.git", "default_branch": "main", "test_command": "make test"}
FORMER_PROJECT = {
    "slug": "api",
    "org": "acme",
    "repo": REPO,
    "repositories": [{"name": "docs", "url": "https://github.com/acme/docs.git"}],
    "gitops": {"repo_url": "https://github.com/acme/gitops.git", "apps": ["api"]},
    "notify": {"slack_channel": "#api"},
}
NEW_PROJECT = {"slug": "letters", "org": "acme", "delivery": {"repo": REPO}}
PLAIN_PROJECT = {"slug": "hr", "org": "acme"}

#: A policy as people write it: block style, comments, a train, the diff bounds and a merge rule.
BLOCK_POLICY = """# The team's policy.
apiVersion: choregos/v1
kind: Policy
metadata:
  name: team
  version: 3
approvals:
  # Never asked: humans are declared in the workflow.
  merge: { required: never }
scope:
  auto_grant_max_files: 2
  max_diff_files: 40   # a reviewer reads forty files at most
  max_diff_lines: 1500
review:
  cross_backend: true
# The trains: dev syncs, prod waits for a captain.
release_train:
  dev: { mode: auto_sync }
  prod:
    mode: train
    approval: { group: release-captains, required: true }
"""
FLOW_POLICY = (
    "{apiVersion: choregos/v1, kind: Policy, metadata: {name: solo, version: 1}, "
    "scope: {max_diff_files: 9}}\n"
)


def _config(url: str) -> Config:
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "src" / "tranibor_api" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("delivery_sections", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _former_result() -> dict[str, Any]:
    return {
        "schema": "choregos/StageResult/v1",
        "status": "done",
        "summary": "fixed",
        "artifacts": {
            "branch": "choregos/1",
            "commits": ["abc"],
            "pr_url": "https://github.com/acme/api/pull/1",
        },
        "evidence": {
            "tests_passed": True,
            "tests_run": 12,
            "diff_files": 2,
            "diff_lines": 30,
            "measured": ["diff_files", "diff_lines", "tests_passed", "tests_run"],
        },
    }


def _former_input() -> dict[str, Any]:
    return {
        "schema": "choregos/StageInput/v1",
        "run_id": "run-1",
        "attempt": 1,
        "project": {"slug": "api", "org": "acme", "test_command": "make test", "lint_command": None},
        "work_item": {
            "key": "acme/api#1",
            "title": "Fix",
            "links": {"pr": "https://github.com/acme/api/pull/1"},
        },
        "transition": {"id": "t-implement", "role": "implement", "from": "ready", "to": "done"},
        "repo": {"url": REPO["url"], "base_branch": "main", "work_branch": "choregos/1"},
        "agent": {"backend": "claude-code"},
        "model": {"litellm_model": "m", "base_url": "http://gateway"},
        "budget": {"usd": 1.0, "max_turns": 10, "max_minutes": 30},
        "playbook": {"ref": "implement"},
        "callbacks": {"api_url": "http://api", "run_token": "[REDACTED]"},
    }


def _insert(connection: Any) -> None:
    connection.execute(
        text("INSERT INTO organizations (id, slug, name, settings) VALUES ('o', 'acme', 'Acme', '{}')")
    )
    for ident, config in (("p-former", FORMER_PROJECT), ("p-new", NEW_PROJECT), ("p-plain", PLAIN_PROJECT)):
        connection.execute(
            text(
                "INSERT INTO projects (id, org_id, slug, name, config, status, provision_state, "
                "workflow_routing, routing_strategy, routing_hints, agent_channel, kind) VALUES "
                "(:id, 'o', :slug, :slug, :config, 'active', '{}', '[]', 'labels', '{}', 'stable', "
                "'standard')"
            ),
            {"id": ident, "slug": config["slug"], "config": json.dumps(config)},
        )
    for ident, source in (("pol-block", BLOCK_POLICY), ("pol-flow", FLOW_POLICY)):
        document = yaml.safe_load(source)
        connection.execute(
            text(
                "INSERT INTO policies (id, project_id, name, version, yaml, json, is_active) "
                "VALUES (:id, 'p-former', :name, 1, :yaml, :json, 1)"
            ),
            {"id": ident, "name": document["metadata"]["name"], "yaml": source, "json": json.dumps(document)},
        )
    connection.execute(
        text(
            "INSERT INTO work_items (id, project_id, tracker_key, title, state, paused, allowed_paths, "
            "fields, totals, documents) VALUES ('wi', 'p-former', 'acme/api#1', 'Fix', 'done', 0, '[]', "
            "'{}', '{}', '{}')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO runs (id, work_item_id, project_id, stage_role, attempt, status, tokens, cost_usd, "
            "allowed_paths, spend_collected, stage_input, result) VALUES ('run-1', 'wi', 'p-former', "
            "'implement', 1, 'succeeded', '{}', 0, '[]', 1, :input, :result)"
        ),
        {"input": json.dumps(_former_input()), "result": json.dumps(_former_result())},
    )


def _stored(connection: Any) -> dict[str, Any]:
    def load(value: Any) -> Any:
        return json.loads(value) if isinstance(value, str) else value

    return {
        "projects": {
            r.id: load(r.config) for r in connection.execute(text("SELECT id, config FROM projects"))
        },
        "policies": {
            r.id: (r.yaml, load(r.json))
            for r in connection.execute(text("SELECT id, yaml, json FROM policies"))
        },
        "runs": {
            r.id: (load(r.stage_input), load(r.result))
            for r in connection.execute(text("SELECT id, stage_input, result FROM runs"))
        },
    }


def _check(stored: dict[str, Any]) -> None:
    projects = stored["projects"]
    assert projects["p-new"] == NEW_PROJECT and projects["p-plain"] == PLAIN_PROJECT, "untouched"
    former = projects["p-former"]
    assert not {"repo", "repositories", "gitops"} & set(former)
    assert former["delivery"] == {k: FORMER_PROJECT[k] for k in ("repo", "repositories", "gitops")}
    config = ProjectConfig.model_validate(former)
    assert config.delivery_section.repository_names() == ["api", "docs"]

    block_yaml, block_json = stored["policies"]["pol-block"]
    assert "# a reviewer reads forty files at most" in block_yaml, "block style keeps its comments"
    assert "# The trains: dev syncs, prod waits for a captain." in block_yaml
    policy = parse_policy(block_yaml)
    assert (policy.delivery.scope.max_diff_files, policy.delivery.scope.max_diff_lines) == (40, 1500)
    assert (
        policy.delivery.approvals.merge is not None and policy.delivery.release_train["prod"].mode == "train"
    )
    assert policy.scope.auto_grant_max_files == 2 and policy.review.cross_backend
    assert Policy.model_validate(block_json) == policy, "the JSON and the text say the same"
    flow_yaml, flow_json = stored["policies"]["pol-flow"]
    assert parse_policy(flow_yaml).delivery.scope.max_diff_files == 9, "flow style is rendered again"
    assert Policy.model_validate(flow_json).delivery.scope.max_diff_files == 9

    stage_input, result = stored["runs"]["run-1"]
    assert "repo" not in stage_input and not {"test_command", "lint_command"} & set(stage_input["project"])
    assert stage_input["delivery"]["repo"]["work_branch"] == "choregos/1"
    assert stage_input["delivery"]["commands"] == {"test": "make test"}
    assert stage_input["work_item"]["links"] == {"change_request": "https://github.com/acme/api/pull/1"}
    StageInput.model_validate(stage_input)
    assert result["artifacts"] == {
        "delivery": {"branch": "choregos/1", "commits": ["abc"]},
        "links": [{"rel": "change_request", "url": "https://github.com/acme/api/pull/1"}],
    }
    assert result["evidence"] == {
        "facts": {"tests_passed": True, "tests_run": 12, "diff_files": 2, "diff_lines": 30},
        "measured": ["facts.diff_files", "facts.diff_lines", "facts.tests_passed", "facts.tests_run"],
    }
    parsed = StageResult.model_validate(result)
    assert parsed == StageResult.model_validate(_former_result()), "what the contract's reader folds"


def test_the_stored_documents_move_to_their_sections_idempotently(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "delivery.db"
    monkeypatch.setenv("TRANIBOR_DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    get_settings.cache_clear()
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    try:
        command.upgrade(_config(url), BEFORE)
        with engine.begin() as connection:
            _insert(connection)
        command.upgrade(_config(url), AFTER)
        with engine.connect() as connection:
            first = _stored(connection)
        _check(first)
        command.downgrade(_config(url), BEFORE)
        with engine.connect() as connection:
            assert _stored(connection) == first, (
                "the downgrade leaves the sections the previous release reads"
            )
        command.upgrade(_config(url), AFTER)
        with engine.connect() as connection:
            assert _stored(connection) == first, "idempotent: a second pass changes nothing"
    finally:
        engine.dispose()
        get_settings.cache_clear()


def test_the_shipped_presets_and_templates_are_what_the_migration_writes() -> None:
    """The policies the repository ships were moved with the same transform the rows get: running it
    again changes nothing, and none writes a former field."""
    migration = _migration()
    root = API.parents[1]
    shipped = [
        *sorted((root / "packages/core/src/tranibor_core/presets").glob("*.yaml")),
        *sorted(root.glob("templates/*/policy.yaml")),
    ]
    assert len(shipped) >= 5
    for path in shipped:
        source = path.read_text(encoding="utf-8")
        assert migration.policy_text(source) == source, path
        parse_policy(source)


def test_the_policy_text_of_a_preset_written_the_former_way_keeps_its_comments() -> None:
    migration = _migration()
    moved = migration.policy_text(BLOCK_POLICY)
    assert moved != BLOCK_POLICY
    assert yaml.safe_load(moved) == migration.policy_document(yaml.safe_load(BLOCK_POLICY))
    assert "  # Never asked: humans are declared in the workflow." in moved
    assert migration.policy_text(moved) == moved


def test_the_downgrade_is_a_no_op() -> None:
    assert _migration().downgrade() is None


async def test_the_stored_documents_move_on_postgres_under_forced_rls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same rows on PostgreSQL, the migration run as the application role: under forced RLS it sees
    the rows only because it opens every organisation (S40-22e)."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.db import session as db_session
    from tranibor_api.migrate import configuration

    from .conftest import PG_URL, _administer, _app_url, _prepare_postgres

    if not PG_URL.startswith("postgresql"):
        pytest.skip("TRANIBOR_TEST_DATABASE_URL absent: no PostgreSQL")
    await _prepare_postgres()
    monkeypatch.setenv("TRANIBOR_DATABASE_URL", _app_url(PG_URL))
    reset_settings_cache()
    await db_session.dispose_engine()
    config = configuration()
    owner = create_async_engine(PG_URL)
    try:
        await asyncio.to_thread(command.upgrade, config, BEFORE)
        async with owner.begin() as connection:  # the superuser: what the rows were before, RLS aside
            await connection.run_sync(lambda sync: _insert(_Postgres(sync)))
        await asyncio.to_thread(command.upgrade, config, AFTER)
        async with owner.connect() as connection:
            first = await connection.run_sync(_stored)
        _check(first)
        await asyncio.to_thread(command.upgrade, config, "heads")
        async with owner.connect() as connection:
            assert await connection.run_sync(_stored) == first
    finally:
        await owner.dispose()
        await db_session.dispose_engine()
        await _administer(PG_URL, "DROP SCHEMA public CASCADE", "CREATE SCHEMA public")
        reset_settings_cache()


class _Postgres:
    """The inserts of `_insert` on PostgreSQL: a JSON text goes into a JSONB column, a flag is a boolean."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        for name in ("config", "json", "input", "result"):
            sql = sql.replace(f":{name})", f"CAST(:{name} AS JSONB))").replace(
                f":{name},", f"CAST(:{name} AS JSONB),"
            )
        sql = sql.replace("'{}'", "'{}'::jsonb").replace("'[]'", "'[]'::jsonb")
        sql = sql.replace(", 1)", ", true)").replace("'[]'::jsonb, 1,", "'[]'::jsonb, true,")
        sql = sql.replace("'done', 0,", "'done', false,")
        return self.connection.execute(text(sql), parameters or {})
