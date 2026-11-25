# SPDX-License-Identifier: Apache-2.0
"""The two programs of the two-container ACA measurement (S86-07), as text for a job definition.

`RUNNER_SIDE` runs in the runner's container of a Container Apps job, with the runner image's own
Python and its `tranibor_runner.launcher` client. It holds a marker secret in its environment, as a
runner holds the run token, waits for the launcher of the agent's container on loopback, writes into
the shared `EmptyDir`, has `AGENT_SIDE` started in the agent's container through the launcher, and
prints one line `S86-07 {json}` with what both sides saw. Its exit code says whether the shape holds:
0 when loopback, the volume both ways and the separation all hold; otherwise the first check that
failed (see `CHECKS`).

`AGENT_SIDE` is what an agent could do: it reports its uid, how many processes it can see, whether
the marker is in any `/proc/<pid>/environ` or `cmdline` it can read, and reads and writes the volume.

Both are standard-library Python so that the job needs no image but the runner's.
"""

from __future__ import annotations

#: Exit codes of the runner's side, in the order the checks run.
CHECKS = {
    0: "every check holds",
    21: "the launcher never answered on loopback",
    22: "the agent's side could not be started through the launcher",
    23: "the agent's side did not see the runner's file in the shared volume",
    24: "the runner did not see the agent's file in the shared volume",
    25: "the agent's side found the runner's marker secret",
}

AGENT_SIDE = r"""
import json, os, pathlib, sys
# The marker comes in two halves so that it is never whole in this process's own command line.
marker = (sys.argv[1] + sys.argv[2]).encode()
seen, found, pids = 0, [], []
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    seen += 1
    pids.append(entry.name)
    for leaf in ("environ", "cmdline"):
        try:
            if marker in (entry / leaf).read_bytes():
                found.append(f"{entry.name}/{leaf}")
        except OSError:
            pass
exchange = pathlib.Path("/exchange")
from_runner = (exchange / "from-runner").read_text() if (exchange / "from-runner").exists() else ""
(exchange / "from-agent").write_text("written by the agent's container")
print(json.dumps({
    "uid": os.getuid(), "gid": os.getgid(), "processes_visible": seen,
    "marker_found_in": found, "read_from_runner": from_runner,
    "hostname": os.uname().nodename,
}))
"""

RUNNER_SIDE = r"""
import asyncio, json, os, pathlib, sys
from tranibor_runner.launcher import Launcher

AGENT_SIDE = os.environ["S86_AGENT_SIDE"]
MARKER = os.environ["S86_MARKER"]
CHECKS = json.loads(os.environ["S86_CHECKS"])

def finish(code: int, report: dict) -> None:
    report["exit"] = code
    report["verdict"] = CHECKS[str(code)]
    print("S86-07 " + json.dumps(report), flush=True)
    pathlib.Path("/exchange/.runner-done").write_text(str(code))
    sys.exit(code)

async def main() -> None:
    report = {"runner_uid": os.getuid(), "runner_hostname": os.uname().nodename}
    exchange = pathlib.Path("/exchange")
    launcher = Launcher()
    if not await launcher.wait_ready(timeout=180):
        finish(21, report)
    (exchange / "from-runner").write_text("written by the runner's container")
    half = len(MARKER) // 2
    process = await launcher.spawn(
        ["python3", "-c", AGENT_SIDE, MARKER[:half], MARKER[half:]],
        cwd="/exchange",
        env={"PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    out, err = await process.communicate()
    report["agent_exit"] = process.returncode
    report["agent_stderr"] = err.decode(errors="replace")[-500:]
    if process.returncode != 0 or process.start_error:
        report["start_error"] = process.start_error
        finish(22, report)
    agent = json.loads(out.decode().strip().splitlines()[-1])
    report["agent"] = agent
    if agent["read_from_runner"] != "written by the runner's container":
        finish(23, report)
    if not (exchange / "from-agent").exists():
        finish(24, report)
    if agent["marker_found_in"]:
        finish(25, report)
    finish(0, report)

asyncio.run(main())
"""
