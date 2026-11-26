"""#2331: a worker's activities queue for a pooled connection instead of failing after the API's 10 s.

One worker process runs `worker_max_concurrent_activities` activities on each of its queues (the local
install's `worker-local` serves four: 80) against one pool of `size + overflow` connections (15). In
the single-node load test the database slowed down, activities waited for a connection, and 59 failed
on `QueuePool limit of size 10 overflow 5 reached, connection timed out, timeout 10.00`, each retried
from its start. The worker now waits `worker_db_pool_timeout_s` (120 s) for one, and says at its start
when its activities can outnumber its connections.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import pytest
from structlog.testing import capture_logs

PG_URL = os.environ.get("TRANIBOR_TEST_DATABASE_URL", "")
on_postgres = pytest.mark.skipif(
    not PG_URL.startswith("postgresql"),
    reason="a pool that waits: PostgreSQL only (TRANIBOR_TEST_DATABASE_URL)",
)


class _Stop(Exception):  # noqa: N818 - the test's stop signal
    pass


@pytest.fixture(autouse=True)
def _clean_pool_wait() -> Iterator[None]:
    from tranibor_api.db import session
    from tranibor_orchestrator.config import reset_settings_cache

    # Another test's `run_worker` in this process set it: start from the API's wait.
    session._pool_timeout_s = None
    reset_settings_cache()
    yield
    session._pool_timeout_s = None
    reset_settings_cache()


def _settings(url: str, **overrides: Any) -> Any:
    from tranibor_api.config import Settings

    return Settings(database_url=url, **overrides)


def test_the_engine_takes_the_process_s_wait_over_the_api_s() -> None:
    from tranibor_api.db import session

    url = "postgresql+asyncpg://app:app@127.0.0.1:1/nothing"  # never connected to
    assert session._create_engine(_settings(url)).pool.timeout() == 10, "the API's wait, unchanged"
    session._pool_timeout_s = 120
    assert session._create_engine(_settings(url)).pool.timeout() == 120


async def _start_the_worker(monkeypatch: pytest.MonkeyPatch, queues: list[str]) -> None:
    """Run `run_worker` up to its first `Worker`, as `test_worker_concurrency` does."""
    import temporalio.worker
    import tranibor_adapters
    from temporalio.client import Client
    from tranibor_orchestrator import worker

    async def connect(*_: object, **__: object) -> object:
        return object()

    class _Worker:
        def __init__(self, client: object, **keywords: Any) -> None:
            pass

        async def __aenter__(self) -> None:
            raise _Stop

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(tranibor_adapters, "load_extensions", list)
    monkeypatch.setattr(Client, "connect", connect)
    monkeypatch.setattr(temporalio.worker, "Worker", _Worker)
    with pytest.raises(_Stop):
        await worker.run_worker(queues)


async def test_the_worker_sets_its_wait_before_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_api.db import session

    monkeypatch.delenv("TRANIBOR_WORKER_DB_POOL_TIMEOUT_S", raising=False)
    await _start_the_worker(monkeypatch, ["orchestrator"])
    assert session._pool_timeout_s == 120

    monkeypatch.setenv("TRANIBOR_WORKER_DB_POOL_TIMEOUT_S", "45")
    from tranibor_orchestrator.config import reset_settings_cache

    reset_settings_cache()
    await _start_the_worker(monkeypatch, ["orchestrator"])
    assert session._pool_timeout_s == 45


async def test_the_start_says_when_the_activities_outnumber_the_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tranibor_api.db import session
    from tranibor_orchestrator.config import get_settings
    from tranibor_orchestrator.worker import fit_the_pool

    pg = _settings("postgresql+asyncpg://app:app@127.0.0.1:1/nothing")
    monkeypatch.setattr(session, "get_settings", lambda: pg)
    monkeypatch.setenv("TRANIBOR_WORKER_MAX_CONCURRENT_ACTIVITIES", "20")
    settings = get_settings()

    with capture_logs() as logs:
        fit = await fit_the_pool(["orchestrator", "executor", "tracker", "memory"], settings)
    assert fit == {"activities": 80, "connections": 15, "wait_s": 120}
    (said,) = [entry for entry in logs if entry["event"] == "worker.activities_outnumber_connections"]
    assert said["log_level"] == "warning"
    assert "TRANIBOR_DB_MAX_OVERFLOW" in said["settings"]

    monkeypatch.setenv("TRANIBOR_WORKER_MAX_CONCURRENT_ACTIVITIES", "5")
    from tranibor_orchestrator.config import reset_settings_cache

    reset_settings_cache()
    with capture_logs() as logs:
        fit = await fit_the_pool(["orchestrator", "executor"], get_settings())
    assert fit["activities"] == 10
    assert not [entry for entry in logs if entry["event"] == "worker.activities_outnumber_connections"]


@on_postgres
async def test_on_postgresql_an_activity_waits_for_the_connection_it_used_to_fail_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pool of one connection, held 2 s: with the API's 1 s wait the second checkout fails, as the
    load test's activities did; with the worker's wait it gets the connection once released."""
    from sqlalchemy import text
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError
    from tranibor_api.db import session

    settings = _settings(PG_URL, db_pool_size=1, db_max_overflow=0, db_pool_timeout_s=1)

    async def held_then_another(engine: Any) -> int:
        holding = asyncio.Event()

        async def hold() -> None:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                holding.set()
                await asyncio.sleep(2)

        holder = asyncio.create_task(hold())
        await holding.wait()
        try:
            async with engine.connect() as connection:
                return int((await connection.execute(text("SELECT 2"))).scalar_one())
        finally:
            await holder
            await engine.dispose()

    with pytest.raises(PoolTimeoutError):
        await held_then_another(session._create_engine(settings))

    session._pool_timeout_s = 10
    assert await held_then_another(session._create_engine(settings)) == 2
