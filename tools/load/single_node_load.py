#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A first load test of ONE node: the local install (`compose.yaml`), in fakes mode (S7, gap B6).

    docker compose up -d
    uv run python tools/load/single_node_load.py                       # 5, 20, 50, 100 users, 2 min each
    uv run python tools/load/single_node_load.py --steps 5,20 --duration 30 --json out.json

What it does, against the running compose (the API on `--url`, default http://localhost:8000):

1. signs in with development sign-in and creates an API token through the API (`POST /me/tokens`);
   the virtual users use only that bearer token, the way a script or the CLI would;
2. for each concurrency step, runs N virtual users in a closed loop (one request, then a think
   time) over the console's hottest reads: the project's work items (the board), one work item,
   the inbox (`/me/pending`), a work item's runs, the project's findings; a small share of the
   iterations creates a work item instead (`--write-ratio`);
3. in the same step, a few approvers poll the project for work items in `awaiting_approval` and
   approve them with the signed-in session (the API refuses a decision from a token): that is what
   lets the fake agents' workflow run end to end (the default workflow has a human gate), and what
   the "completed per minute" figure counts (decisions accepted; the final drain tally checks states);
4. records, per route and per step, the throughput, the error rate and p50/p95/p99 latency, the
   time from a work item's creation to the approver's first sight of it in `awaiting_approval`
   (resolution: the approver's poll, `--approver-poll`, 2 s), and samples `docker stats --no-stream` every
   `--stats-every` seconds for the CPU and memory of every container of the compose project.

What it does not do: it is not a soak, it does not exercise the console's Next.js server (the API is
called directly), and it runs on the same machine as the system under test, so the generator and
the platform compete for the same CPUs. Read the numbers as a floor for this machine, not as a
capacity statement. httpx is already a dependency of the platform: no new one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

READ_ROUTES = ("work_items_list", "work_item_get", "inbox_pending", "runs_list", "findings_list")
#: Relative weights of the reads: the board is opened far more often than a single run.
READ_WEIGHTS = (30, 20, 20, 15, 15)
WRITE_ROUTES = ("work_item_create", "approve")
E2E = "e2e_created_to_awaiting_approval"
MAX_APPROVAL_ATTEMPTS = 30


@dataclass
class Sample:
    route: str
    seconds: float
    ok: bool
    status: int = 0


@dataclass
class StepResult:
    users: int
    duration_s: float
    samples: list[Sample] = field(default_factory=list)
    created: int = 0
    approved: int = 0
    stats: list[dict[str, tuple[float, float]]] = field(default_factory=list)


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list; 0 when empty."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(q / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def summarise(samples: list[Sample], route: str, duration_s: float) -> dict[str, float | int | str]:
    """Count, throughput, error rate and latency percentiles (ms) of one route."""
    mine = [s for s in samples if s.route == route]
    # Latency is read on every answered request, errors included: a fast 500 must not flatter p95.
    latencies = sorted(s.seconds * 1000 for s in mine)
    errors = sum(1 for s in mine if not s.ok)
    return {
        "route": route,
        "count": len(mine),
        "rps": round(len(mine) / duration_s, 2) if duration_s else 0.0,
        "error_pct": round(100 * errors / len(mine), 2) if mine else 0.0,
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "p99_ms": round(percentile(latencies, 99), 1),
    }


def parse_cpu_mem(line: str) -> tuple[str, float, float] | None:
    """`name;12.5%;340MiB / 15GiB` as (name, cpu percent of one core, MiB), None if unreadable."""
    name, _, rest = line.partition(";")
    cpu, _, mem = rest.partition(";")
    used = mem.split(" / ")[0].strip()
    units = {
        "KiB": 1 / 1024,
        "MiB": 1.0,
        "GiB": 1024.0,
        "kB": 1 / 1024,
        "MB": 1.0,
        "GB": 1024.0,
        "B": 1 / 1048576,
    }
    for unit, factor in units.items():
        if used.endswith(unit):
            try:
                return name, float(cpu.strip().rstrip("%")), float(used.removesuffix(unit)) * factor
            except ValueError:
                return None
    return None


def docker_stats(compose_project: str) -> dict[str, tuple[float, float]]:
    """One `docker stats --no-stream` of the compose project: {container: (cpu %, MiB)}."""
    ids = subprocess.run(  # noqa: S603 - fixed argv, `docker` from PATH
        ["docker", "compose", "-p", compose_project, "ps", "-q"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if not ids:
        return {}
    out = subprocess.run(  # noqa: S603 - fixed argv, `docker` from PATH
        ["docker", "stats", "--no-stream", "--format", "{{.Name}};{{.CPUPerc}};{{.MemUsage}}", *ids],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    rows = (parse_cpu_mem(line) for line in out.splitlines())
    return {r[0]: (r[1], r[2]) for r in rows if r is not None}


class Load:
    def __init__(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        project: str,
        think_s: float,
        write_ratio: float,
        approver_poll_s: float,
    ) -> None:
        self.client = client
        #: Deciding is a person's own gesture and needs a session, not a token (`decision_requires_session`).
        self.session_client = session_client
        self.project = project
        self.think_s = think_s
        self.write_ratio = write_ratio
        self.approver_poll_s = approver_poll_s
        self.rng = random.Random(7)  # noqa: S311 - a workload mix, not a secret
        self.ids: list[str] = []
        self.step: StepResult | None = None
        self.created_at: dict[str, float] = {}
        self.seen_approvals: set[str] = set()
        self.first_seen: set[str] = set()
        self.approval_attempts: dict[str, int] = {}
        self.counter = 0

    async def call(
        self,
        route: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        as_person: bool = False,
    ) -> Any:
        started = time.monotonic()
        ok, payload, code = False, None, 0
        try:
            client = self.session_client if as_person else self.client
            response = await client.request(method, path, json=body)
            code = response.status_code
            ok = code < 400
            if ok and response.content:
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            ok = False
        if self.step is not None:
            self.step.samples.append(Sample(route, time.monotonic() - started, ok, code))
        return payload

    async def refresh_ids(self) -> None:
        page = await self.call("work_items_list", "GET", f"/api/v1/projects/{self.project}/work-items")
        if isinstance(page, dict):
            self.ids = [str(i["id"]) for i in page.get("items", [])] or self.ids

    async def read_once(self) -> None:
        route = self.rng.choices(READ_ROUTES, READ_WEIGHTS)[0]
        item = self.rng.choice(self.ids) if self.ids else None
        if route == "work_items_list":
            page = await self.call(route, "GET", f"/api/v1/projects/{self.project}/work-items")
            if isinstance(page, dict) and page.get("items"):
                self.ids = [str(i["id"]) for i in page["items"]]
        elif route == "work_item_get" and item:
            await self.call(route, "GET", f"/api/v1/work-items/{item}")
        elif route == "inbox_pending":
            await self.call(route, "GET", "/api/v1/me/pending")
        elif route == "runs_list" and item:
            await self.call(route, "GET", f"/api/v1/work-items/{item}/runs")
        elif route == "findings_list":
            await self.call(route, "GET", f"/api/v1/projects/{self.project}/findings")

    async def create_once(self) -> None:
        self.counter += 1
        created = await self.call(
            "work_item_create",
            "POST",
            f"/api/v1/projects/{self.project}/work-items",
            {"title": f"Load {self.counter}", "body": "Plan the team offsite: two days, twelve people."},
        )
        if isinstance(created, dict) and "id" in created:
            self.created_at[str(created["id"])] = time.monotonic()
            if self.step is not None:
                self.step.created += 1

    async def user(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.rng.random() < self.write_ratio:
                await self.create_once()
            else:
                await self.read_once()
            await asyncio.sleep(self.think_s * self.rng.uniform(0.5, 1.5))

    async def approver(self, stop: asyncio.Event) -> None:
        """Approve what waits for a person: the fake agents' workflow has a human gate."""
        while not stop.is_set():
            page = await self.call(
                "approver_poll",
                "GET",
                f"/api/v1/projects/{self.project}/work-items?state=awaiting_approval",
                as_person=True,
            )
            waiting = list((page or {}).get("items", [])) if isinstance(page, dict) else []
            for item in waiting:
                item_id = str(item["id"])
                if item_id in self.seen_approvals:
                    continue
                self.seen_approvals.add(item_id)
                born = self.created_at.get(item_id)
                if born is not None and item_id not in self.first_seen and self.step is not None:
                    self.step.samples.append(Sample(E2E, time.monotonic() - born, True))
                self.first_seen.add(item_id)
                done = await self.call(
                    "approve",
                    "POST",
                    f"/api/v1/work-items/{item_id}/decisions",
                    {"kind": "approve"},
                    as_person=True,
                )
                if done is not None and self.step is not None:
                    self.step.approved += 1
                elif done is None:
                    # The state reads `awaiting_approval` a moment before the human request exists
                    # (409 `no human request is pending`): ask again at the next poll, a few times.
                    attempts = self.approval_attempts.get(item_id, 0) + 1
                    self.approval_attempts[item_id] = attempts
                    if attempts < MAX_APPROVAL_ATTEMPTS:
                        self.seen_approvals.discard(item_id)
            await asyncio.sleep(self.approver_poll_s)

    async def run_step(
        self, users: int, duration_s: float, approvers: int, stats_every: float, compose: str
    ) -> StepResult:
        result = StepResult(users=users, duration_s=duration_s)
        self.step = result
        stop = asyncio.Event()
        tasks = [asyncio.create_task(self.user(stop)) for _ in range(users)]
        tasks += [asyncio.create_task(self.approver(stop)) for _ in range(approvers)]
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            sampled = await asyncio.to_thread(docker_stats, compose)
            if sampled:
                result.stats.append(sampled)
            await asyncio.sleep(max(0.0, min(stats_every, deadline - time.monotonic())))
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.step = None
        return result


async def tally_states(load: Load) -> dict[str, int]:
    """Work items of the project by state, read page by page (the drain check at the end)."""
    counts: dict[str, int] = {}
    cursor = ""
    while True:
        path = f"/api/v1/projects/{load.project}/work-items?limit=200" + (
            f"&cursor={cursor}" if cursor else ""
        )
        page = await load.call("tally", "GET", path)
        if not isinstance(page, dict):
            return counts
        for item in page.get("items", []):
            counts[str(item["state"])] = counts.get(str(item["state"]), 0) + 1
        meta = page.get("meta") or {}
        if not meta.get("has_more"):
            return counts
        cursor = str(meta["next_cursor"])


async def burst(load: Load, count: int, concurrency: int, approvers: int, timeout_s: float) -> dict[str, Any]:
    """The workflow's own throughput: `count` work items filed `concurrency` at a time, nothing else running.

    Reports the seconds until the last item has reached `awaiting_approval` and been approved, and
    until the last one is `approved` (the terminal state), then items per minute over the second.
    """
    before = (await tally_states(load)).get("approved", 0)
    result = StepResult(users=0, duration_s=0)
    load.step = result
    stop = asyncio.Event()
    pollers = [asyncio.create_task(load.approver(stop)) for _ in range(approvers)]
    started = time.monotonic()
    gate = asyncio.Semaphore(concurrency)

    async def one() -> None:
        async with gate:
            await load.create_once()

    await asyncio.gather(*(one() for _ in range(count)))
    filed_s = time.monotonic() - started
    decided_s = terminal_s = float("nan")
    while time.monotonic() - started < timeout_s:
        if math.isnan(decided_s) and result.approved >= result.created:
            decided_s = time.monotonic() - started
        if (await tally_states(load)).get("approved", 0) - before >= result.created:
            terminal_s = time.monotonic() - started
            break
        await asyncio.sleep(1)
    stop.set()
    await asyncio.gather(*pollers, return_exceptions=True)
    load.step = None
    failed: dict[str, int] = {}
    for sample in result.samples:
        if not sample.ok:
            key = f"{sample.route} -> {sample.status or 'no answer'}"
            failed[key] = failed.get(key, 0) + 1
    return {
        "failed": failed,
        "requested": count,
        "created": result.created,
        "approved_decisions": result.approved,
        "filed_s": round(filed_s, 1),
        "all_decided_s": round(decided_s, 1),
        "all_approved_s": round(terminal_s, 1),
        "items_per_min": round(result.created / (terminal_s / 60), 1) if not math.isnan(terminal_s) else 0.0,
    }


async def sign_in(client: httpx.AsyncClient, email: str) -> str:
    """Development sign-in, then an API token through the API; the session cookie stays on `client`."""
    # Two hops by hand (login, then the callback it points to): the callback sets the session
    # cookie and redirects to a console page the bare API does not serve.
    response = await client.get("/api/v1/auth/login", params={"as": email, "redirect_to": "/"})
    callback = response.headers.get("location", "")
    if response.status_code != 307 or not callback.startswith("/api/v1/auth/callback"):
        raise SystemExit(f"sign-in answered {response.status_code}: {response.text[:200]}")
    response = await client.get(callback)
    if response.status_code >= 400:
        raise SystemExit(f"sign-in callback answered {response.status_code}: {response.text[:200]}")
    created = await client.post("/api/v1/me/tokens", json={"name": "load-test", "expires_in_days": 1})
    if created.status_code != 201:
        raise SystemExit(f"creating the API token answered {created.status_code}: {created.text[:200]}")
    return str(created.json()["token"])


def render(results: list[StepResult]) -> str:
    lines = [
        "| users | route | count | rps | err % | p50 ms | p95 ms | p99 ms |",
        "|--:|:--|--:|--:|--:|--:|--:|--:|",
    ]
    for r in results:
        for route in (*READ_ROUTES, *WRITE_ROUTES, "approver_poll", E2E):
            row = summarise(r.samples, route, r.duration_s)
            if row["count"]:
                lines.append(
                    f"| {r.users} | {route} | {row['count']} | {row['rps']} | {row['error_pct']} "
                    f"| {row['p50_ms']} | {row['p95_ms']} | {row['p99_ms']} |"
                )
    lines += ["", "| users | created | approved | completed / min |", "|--:|--:|--:|--:|"]
    for r in results:
        lines.append(f"| {r.users} | {r.created} | {r.approved} | {r.approved / (r.duration_s / 60):.1f} |")
    lines += ["", "| users | container | cpu mean % | cpu max % | mem max MiB |", "|--:|:--|--:|--:|--:|"]
    for r in results:
        names = sorted({n for s in r.stats for n in s})
        top = sorted(
            ((n, [s[n][0] for s in r.stats if n in s], [s[n][1] for s in r.stats if n in s]) for n in names),
            key=lambda t: -sum(t[1]) / len(t[1]),
        )[:6]
        for name, cpus, mems in top:
            lines.append(
                f"| {r.users} | {name} | {sum(cpus) / len(cpus):.0f} | {max(cpus):.0f} | {max(mems):.0f} |"
            )
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    steps = [int(s) for s in args.steps.split(",") if s]
    limits = httpx.Limits(max_connections=max([*steps, args.burst_concurrency]) + 20)
    async with (
        httpx.AsyncClient(base_url=args.url, timeout=30, limits=limits) as session_client,
        httpx.AsyncClient(base_url=args.url, timeout=30, limits=limits) as client,
    ):
        token = await sign_in(session_client, args.email)
        client.headers["Authorization"] = f"Bearer {token}"
        load = Load(
            client, session_client, args.project, args.think_ms / 1000, args.write_ratio, args.approver_poll
        )
        await load.refresh_ids()
        print(f"signed in, token created; {len(load.ids)} work items known in {args.project}", flush=True)
        for _ in range(args.seed_items):
            await load.create_once()
        if args.seed_items:
            print(
                f"seeded {args.seed_items} work items, so that the board is a full page (50) in every step",
                flush=True,
            )
        if args.warmup > 0:
            print(f"warm-up: 5 users, {args.warmup:.0f} s (not recorded)", flush=True)
            await load.run_step(5, args.warmup, args.approvers, 1e9, args.compose_project)
        results: list[StepResult] = []
        for users in steps:
            print(f"step: {users} users, {args.duration:.0f} s", flush=True)
            results.append(
                await load.run_step(
                    users, args.duration, args.approvers, args.stats_every, args.compose_project
                )
            )
            await asyncio.sleep(args.pause)
        await asyncio.sleep(args.drain)
        states = await tally_states(load)
        if args.burst:
            print(
                f"burst: {args.burst} work items, {args.burst_concurrency} at a time, nothing else",
                flush=True,
            )
            summary = await burst(load, args.burst, args.burst_concurrency, args.approvers, 300)
            print("burst:", summary)
    print(render(results))
    errors: dict[str, int] = {}
    for r in results:
        for sample in r.samples:
            if not sample.ok:
                key = f"{sample.route} -> {sample.status or 'no answer'}"
                errors[key] = errors.get(key, 0) + 1
    print("\nerrors by route and status:", errors or "none")
    print(f"work items by state after a {args.drain:.0f} s drain:", states)
    if args.json:
        payload = [{"final_states": states, "errors": errors}] + [
            {
                "users": r.users,
                "duration_s": r.duration_s,
                "created": r.created,
                "approved": r.approved,
                "routes": [
                    summarise(r.samples, route, r.duration_s)
                    for route in (*READ_ROUTES, *WRITE_ROUTES, "approver_poll", E2E)
                ],
                "stats": [{k: list(v) for k, v in s.items()} for s in r.stats],
            }
            for r in results
        ]
        await asyncio.to_thread(Path(args.json).write_text, json.dumps(payload, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:8000", help="the API (default: %(default)s)")
    parser.add_argument(
        "--email", default="admin@choregos.local", help="development sign-in e-mail (the org admin)"
    )
    parser.add_argument("--project", default="demo:demo", help="org:project that receives the work items")
    parser.add_argument("--steps", default="5,20,50,100", help="concurrency steps, virtual users")
    parser.add_argument("--duration", type=float, default=120, help="seconds per step")
    parser.add_argument("--warmup", type=float, default=15, help="seconds of unrecorded warm-up (0: none)")
    parser.add_argument("--seed-items", type=int, default=60, help="work items created before the first step")
    parser.add_argument("--pause", type=float, default=10, help="seconds between steps")
    parser.add_argument(
        "--burst", type=int, default=0, help="after the steps, file this many work items at once"
    )
    parser.add_argument(
        "--burst-concurrency", type=int, default=10, help="creations in flight during the burst"
    )
    parser.add_argument("--drain", type=float, default=30, help="seconds to let the last work items finish")
    parser.add_argument("--think-ms", type=float, default=250, help="mean think time of a user, ms")
    parser.add_argument(
        "--write-ratio", type=float, default=0.03, help="share of iterations that create a work item"
    )
    parser.add_argument("--approvers", type=int, default=1, help="pollers that approve work items")
    parser.add_argument(
        "--approver-poll", type=float, default=2.0, help="seconds between an approver's polls"
    )
    parser.add_argument("--stats-every", type=float, default=10, help="seconds between docker stats samples")
    parser.add_argument("--compose-project", default="choregos", help="compose project name")
    parser.add_argument("--json", default="", help="write the raw per-step results to this file")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
