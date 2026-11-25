"""The data migration that moves an organisation ceiling's diff caps into its `delivery` section (ADR 0181,
S83-13).

A ceiling stored with `scope.max_diff_files` / `scope.max_diff_lines` is rewritten to
`delivery.scope.*`, what `PolicyCeiling` reads now; a ceiling without them, and the rest of the settings,
are left as they are; a second pass changes nothing, and the downgrade puts the caps back where the
previous release reads them. Played on SQLite here, and on PostgreSQL under forced RLS when the suite
has one.
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
from tranibor_core.policy_ceiling import PolicyCeiling

API = pathlib.Path(__file__).resolve().parents[1]
BEFORE = "e8c0a2d4f6b1"
AFTER = "6eb670a63a1b"
MIGRATION = API / "src/tranibor_api/migrations/versions/20261125_1000_ceiling_delivery_section.py"

FORMER = {
    "budgets": {"ticket_usd": {"M": 20}},
    "scope": {"auto_grant_max_files": 2, "max_diff_files": 40, "max_diff_lines": 1500},
}
ONLY_DIFF = {"scope": {"max_diff_lines": 900}}
NEW = {"delivery": {"scope": {"max_diff_files": 12}}, "findings": {"max_per_run": 3}}
SETTINGS = {
    "o-former": {"policy_ceiling": {"ceiling": FORMER, "updated_by": "a@acme.test"}, "theme": "dark"},
    "o-only": {"policy_ceiling": {"ceiling": ONLY_DIFF}},
    "o-new": {"policy_ceiling": {"ceiling": NEW}},
    "o-none": {"theme": "light"},
}


def _config(url: str) -> Config:
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "src" / "tranibor_api" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("ceiling_delivery_section", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(connection: Any) -> None:
    # A JSON text goes into PostgreSQL's JSONB column through a cast; SQLite stores the text.
    value = "CAST(:settings AS JSONB)" if connection.dialect.name == "postgresql" else ":settings"
    for ident, settings in SETTINGS.items():
        connection.execute(
            text(f"INSERT INTO organizations (id, slug, name, settings) VALUES (:id, :id, :id, {value})"),
            {"id": ident, "settings": json.dumps(settings)},
        )


def _stored(connection: Any) -> dict[str, Any]:
    rows = connection.execute(text("SELECT id, settings FROM organizations ORDER BY id")).fetchall()
    return {row_id: json.loads(value) if isinstance(value, str) else value for row_id, value in rows}


def _check(stored: dict[str, Any]) -> None:
    former = stored["o-former"]
    assert former["theme"] == "dark" and former["policy_ceiling"]["updated_by"] == "a@acme.test"
    assert former["policy_ceiling"]["ceiling"] == {
        "budgets": {"ticket_usd": {"M": 20}},
        "scope": {"auto_grant_max_files": 2},
        "delivery": {"scope": {"max_diff_files": 40, "max_diff_lines": 1500}},
    }
    assert stored["o-only"]["policy_ceiling"]["ceiling"] == {"delivery": {"scope": {"max_diff_lines": 900}}}
    assert stored["o-new"] == SETTINGS["o-new"] and stored["o-none"] == SETTINGS["o-none"]
    for settings in stored.values():
        if "policy_ceiling" in settings:
            PolicyCeiling.model_validate(settings["policy_ceiling"]["ceiling"])  # what the API reads


def test_a_former_ceiling_is_refused_and_the_migrated_one_is_read() -> None:
    with pytest.raises(ValueError, match="moved to the `delivery` section of the policy ceiling"):
        PolicyCeiling.model_validate(FORMER)
    moved = _migration().ceiling_forward(FORMER)
    assert PolicyCeiling.model_validate(moved).delivery.scope.max_diff_files == 40
    assert _migration().ceiling_back(moved) == FORMER


def test_the_stored_ceilings_move_to_their_section_idempotently(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "ceiling.db"
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
            back = _stored(connection)
        assert back["o-former"] == SETTINGS["o-former"], "the downgrade puts the caps where 0.29 reads them"
        assert back["o-only"] == SETTINGS["o-only"]
        command.upgrade(_config(url), AFTER)
        command.upgrade(_config(url), "heads")
        with engine.connect() as connection:
            assert _stored(connection) == first, "idempotent: a second pass changes nothing"
    finally:
        engine.dispose()
        get_settings.cache_clear()


async def test_the_stored_ceilings_move_on_postgres_under_forced_rls(monkeypatch: pytest.MonkeyPatch) -> None:
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
            await connection.run_sync(_insert)
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
