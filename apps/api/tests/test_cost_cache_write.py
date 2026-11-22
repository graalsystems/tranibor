"""The cost ledger and its report keep cache writes apart from cache reads (S4-07, ADR 0188)."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def _book(project_id: str, model: str, cached: int, written: int | None, long_writes: int = 0) -> Any:
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.costs import record_cost

    extra: dict[str, Any] = (
        {} if written is None else {"tokens_cache_write": written, "tokens_cache_write_1h": long_writes}
    )
    async with session_scope() as session:
        return await record_cost(
            session,
            project_id=project_id,
            work_item_id=None,
            run_id=None,
            provider="p",
            model=model,
            backend=None,
            stage_role=None,
            size=None,
            tokens_in=1_000,
            tokens_out=10,
            tokens_cached=cached,
            cost_usd=1.0,
            fx_rate=0.9,
            **extra,
        )


async def test_the_report_sums_cache_writes_apart_from_cache_reads(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await _book(project["id"], "a", 700, 100)
    await _book(project["id"], "a", 50, 25)
    await _book(project["id"], "b", 0, 0)
    response = await client.get(f"/api/v1/projects/{project['id']}/costs", params={"group_by": "model"})
    assert response.status_code == 200, response.text
    report = response.json()
    rows = {r["key"]: (r["tokens_cached"], r["tokens_cache_write"]) for r in report["rows"]}
    assert rows == {"a": (750, 125), "b": (0, 0)}
    assert (report["total"]["tokens_cached"], report["total"]["tokens_cache_write"]) == (750, 125)


async def test_a_line_booked_without_cache_writes_reads_zero(project: dict[str, Any]) -> None:
    line = await _book(project["id"], "m", 0, None)
    assert line.tokens_cache_write == 0


async def test_the_csv_carries_cache_writes_last_so_that_columns_keep_their_position(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await _book(project["id"], "a", 700, 100)
    await _book(project["id"], "b", 50, 25)
    response = await client.get(f"/api/v1/projects/{project['id']}/costs.csv", params={"group_by": "model"})
    lines = [line.split(";") for line in response.text.lstrip("﻿").splitlines() if line]
    assert lines[0] == [
        "model",
        "cout_usd",
        "cout_eur",
        "tokens_in",
        "tokens_out",
        "tokens_caches",
        "runs",
        "tokens_cache_write",
    ]
    by_key = {line[0]: line for line in lines[1:] if line[0] in {"a", "b", "total"}}
    # The first seven cells are the ones a consumer already reads by position; the write is the eighth.
    assert (by_key["a"][5], by_key["a"][7]) == ("700", "100")
    assert (by_key["b"][5], by_key["b"][7]) == ("50", "25")
    assert (by_key["total"][5], by_key["total"][6], by_key["total"][7]) == ("750", "", "125")


async def test_the_work_item_totals_carry_cache_writes() -> None:
    from tranibor_api.services.tickets import totals_from

    assert totals_from({"tokens_cached": 4, "tokens_cache_write": 9}).tokens_cache_write == 9
    assert totals_from({"tokens_cached": 4}).tokens_cache_write == 0, "a total booked before S4-07 reads 0"


async def test_the_report_sums_the_one_hour_writes_apart(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    """S4-12: the 1-hour part of the writes is its own count, per row and in the total."""
    await _book(project["id"], "a", 0, 100, long_writes=60)
    await _book(project["id"], "a", 0, 50, long_writes=0)
    await _book(project["id"], "b", 0, 10, long_writes=10)
    response = await client.get(f"/api/v1/projects/{project['id']}/costs", params={"group_by": "model"})
    report = response.json()
    rows = {r["key"]: (r["tokens_cache_write"], r["tokens_cache_write_1h"]) for r in report["rows"]}
    assert rows == {"a": (150, 60), "b": (10, 10)}
    assert report["total"]["tokens_cache_write_1h"] == 70
