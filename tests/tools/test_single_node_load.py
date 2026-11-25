"""The single-node load script: its arithmetic, and the one rule it must keep (S7, gap B6).

The script is a measuring instrument: a wrong percentile or an approval sent with the token would
produce a number nobody can tell is wrong. The platform is replaced by an `httpx.MockTransport`.
"""

from __future__ import annotations

import asyncio

import httpx

from tools.load.single_node_load import (
    E2E,
    Load,
    Sample,
    StepResult,
    parse_cpu_mem,
    percentile,
    render,
    sign_in,
    summarise,
    tally_states,
)


def test_percentile_is_the_nearest_rank():
    values = sorted(float(n) for n in range(1, 101))
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile(values, 99) == 99.0
    assert percentile([], 95) == 0.0
    assert percentile([7.0], 99) == 7.0


def test_summarise_counts_errors_and_keeps_their_latency():
    samples = [
        Sample("a", 0.1, True),
        Sample("a", 0.3, True),
        Sample("a", 2.0, False, 500),
        Sample("b", 1.0, True),
    ]
    row = summarise(samples, "a", duration_s=10)
    assert row["count"] == 3
    assert row["rps"] == 0.3
    assert row["error_pct"] == 33.33
    # A failure that takes two seconds is in the tail: errors do not leave the percentiles.
    assert row["p99_ms"] == 2000.0
    assert summarise(samples, "missing", duration_s=10)["count"] == 0


def test_docker_stats_lines_are_read_in_mebibytes():
    assert parse_cpu_mem("choregos-api-1;117.5%;1.5GiB / 15GiB") == ("choregos-api-1", 117.5, 1536.0)
    assert parse_cpu_mem("x;0.00%;512KiB / 1GiB") == ("x", 0.0, 0.5)
    assert parse_cpu_mem("garbage") is None
    assert parse_cpu_mem("x;1%;lots / 1GiB") is None


def platform(seen: list[httpx.Request]) -> httpx.MockTransport:
    """A platform with one work item waiting for approval, which refuses a token's decision."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v1/auth/login":
            return httpx.Response(307, headers={"location": "/api/v1/auth/callback?code=dev:a%40b"})
        if path == "/api/v1/auth/callback":
            return httpx.Response(307, headers={"set-cookie": "session=s1; Path=/", "location": "/"})
        if path == "/api/v1/me/tokens":
            return httpx.Response(201, json={"token": "chg_t"})
        if path.endswith("/decisions"):
            if request.headers.get("authorization"):
                return httpx.Response(403, json={"code": "decision_requires_session"})
            return httpx.Response(202, json={})
        if path.endswith("/work-items") and request.method == "POST":
            return httpx.Response(201, json={"id": "wi-1"})
        if path.endswith("/work-items"):
            page = {"items": [{"id": "wi-1", "state": "awaiting_approval"}], "meta": {"has_more": False}}
            return httpx.Response(200, json=page)
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)


async def test_the_approver_decides_with_the_session_and_the_users_with_the_token():
    seen: list[httpx.Request] = []
    async with (
        httpx.AsyncClient(base_url="http://api", transport=platform(seen)) as session_client,
        httpx.AsyncClient(base_url="http://api", transport=platform(seen)) as client,
    ):
        token = await sign_in(session_client, "a@b")
        assert token == "chg_t"
        client.headers["authorization"] = f"Bearer {token}"
        load = Load(client, session_client, "org:p", think_s=0, write_ratio=0, approver_poll_s=0)
        load.ids = ["wi-1"]
        stop = asyncio.Event()
        result = StepResult(users=1, duration_s=1)
        load.step = result
        await load.create_once()
        approver = asyncio.create_task(load.approver(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await approver
        await load.read_once()
    decisions = [r for r in seen if r.url.path.endswith("/decisions")]
    assert decisions
    assert all(not r.headers.get("authorization") and "session=s1" in r.headers["cookie"] for r in decisions)
    assert result.created == 1
    assert result.approved == 1
    assert [s.route for s in result.samples].count(E2E) == 1
    assert all(s.ok for s in result.samples)


async def test_tally_states_walks_the_pages():
    pages = {
        "": {
            "items": [{"state": "approved"}, {"state": "needs_human"}],
            "meta": {"has_more": True, "next_cursor": "c"},
        },
        "c": {"items": [{"state": "approved"}], "meta": {"has_more": False}},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("cursor", "")])

    async with httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handle)) as client:
        load = Load(client, client, "org:p", think_s=0, write_ratio=0, approver_poll_s=0)
        assert await tally_states(load) == {"approved": 2, "needs_human": 1}


def test_render_gives_the_three_tables():
    step = StepResult(users=5, duration_s=60, created=3, approved=3)
    step.samples = [Sample("work_items_list", 0.2, True), Sample(E2E, 1.5, True)]
    step.stats = [{"choregos-api-1": (90.0, 200.0)}]
    text = render([step])
    assert "| 5 | work_items_list | 1 |" in text
    assert "| 5 | 3 | 3 | 3.0 |" in text
    assert "| 5 | choregos-api-1 | 90 | 90 | 200 |" in text


async def test_an_approval_refused_before_the_request_exists_is_asked_again():
    answers = [409, 202]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/decisions"):
            return httpx.Response(answers.pop(0), json={})
        page = {"items": [{"id": "wi-1", "state": "awaiting_approval"}], "meta": {"has_more": False}}
        return httpx.Response(200, json=page)

    async with httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(handle)) as client:
        load = Load(client, client, "org:p", think_s=0, write_ratio=0, approver_poll_s=0)
        load.step = result = StepResult(users=1, duration_s=1)
        stop = asyncio.Event()
        approver = asyncio.create_task(load.approver(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await approver
    assert result.approved == 1
    assert [s.status for s in result.samples if s.route == "approve"] == [409, 202]
