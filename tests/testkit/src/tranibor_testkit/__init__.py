# SPDX-License-Identifier: Apache-2.0
"""The test kit of the core's suites (ADR 0181, round D, S83-05l).

The API's and the orchestrator's suites set their scene with fixtures (an application on a fresh database,
a project, a work item, a Temporal test server and the workers of each queue) and helpers (a scripted
stage result, a workflow started, a state waited for). A first-party plugin's tests need the same scene: a
software delivery test that moved to `plugins/software-delivery/tests` runs on the fixtures it ran on. The
kit is that scene, in a package both import:

- `tranibor_testkit.api`: the API suite's fixtures (`app`, `client`, `org`, `admin`, `project`, `pg_app`,
  `github`, `login`), which `apps/api/tests/conftest.py` re-exports;
- `tranibor_testkit.orchestrator`: the orchestrator suite's (`setup`, `temporal_env`, `worker_factory`,
  `stage_result`, `refine_result`), which `apps/orchestrator/tests/conftest.py` re-exports, and the
  interpreter's helpers (`scripted`, `start`, `wait_state`, `wait_until`, `runs`);
- `tranibor_testkit.sqlite` and `tranibor_testkit.postgres`: a database per test.

A suite's `conftest.py` makes the fixtures its own with `from tranibor_testkit.api import *`. The kit is a
development dependency of the workspace, never one of the published images.
"""
