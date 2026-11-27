"""What the demonstration history tests share (ADR 0216): a workflow, a document that moves through it, and an
instance that opted in to the import. Kept here, not in a test module (`test_cross_module_imports`)."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient

#: The workflow the document's history moves through: the test's own, so that the history holds with or
#: without the software-delivery plugin, whose workflow a project otherwise starts with.
INVOICES = """
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: invoices
  version: 1
initial: inbox
actors:
  implementer: { type: agent, role: implement }
  verifier: { type: agent, role: verify }
states:
  inbox: { display: Inbox }
  in_progress: { display: In progress }
  done: { display: Done, terminal: true }
transitions:
  - { id: t-implement, from: inbox, to: in_progress, by: implementer }
  - { id: t-done, from: in_progress, to: done, by: verifier }
"""

DOCUMENT: dict[str, Any] = {
    "schema": "choregos/DemoHistory/v1",
    "workflow": "invoices",
    "prices": {
        "anthropic/claude-sonnet-5": {
            "input_usd_per_mtok": 3.0,
            "output_usd_per_mtok": 15.0,
            "cached_input_usd_per_mtok": 0.3,
        }
    },
    "work_items": [
        {
            "id": "inv-142",
            "title": "Credit notes are not deducted from the invoice total",
            "size": "M",
            "created_by": "admin@northwind.dev",
            "created_at": "2026-11-02T09:00:00Z",
            "history": [
                {"state": "inbox", "at": "2026-11-02T09:00:00Z"},
                {"state": "in_progress", "at": "2026-11-02T10:30:00Z", "by": "admin@northwind.dev"},
                {"state": "done", "at": "2026-11-03T08:00:00Z"},
            ],
            "runs": [
                {
                    "id": "implement-1",
                    "stage": "implement",
                    "status": "succeeded",
                    "started_at": "2026-11-02T10:31:00Z",
                    "duration_s": 3600,
                    "model": "anthropic/claude-sonnet-5",
                    "tokens": {"input": 412000, "output": 18500, "cached": 300000},
                    "summary": "Credit notes are subtracted before the VAT is computed.",
                },
                {
                    "id": "verify-1",
                    "stage": "verify",
                    "status": "failed",
                    "started_at": "2026-11-02T12:00:00Z",
                    "duration_s": 120,
                    "cost_usd": 0.25,
                },
            ],
            "decisions": [
                {
                    "id": "spec-ok",
                    "kind": "approval",
                    "summary": "Approve the specification?",
                    "at": "2026-11-02T10:25:00Z",
                    "by": "admin@northwind.dev",
                }
            ],
            "findings": [
                {
                    "id": "reset-link",
                    "title": "The invoice download link does not expire",
                    "type": "security",
                    "severity": "medium",
                    "evidence": "src/invoices/links.py:41",
                    "run": "implement-1",
                }
            ],
        },
        {
            "id": "inv-143",
            "title": "Export invoices as PDF",
            "created_at": "2026-11-04T09:00:00Z",
        },
    ],
}
URL = "/api/v1/projects/{id}/demo-history"


def _document() -> dict[str, Any]:
    return copy.deepcopy(DOCUMENT)


@pytest.fixture(name="demo")
async def demo_history_opted_in(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """An instance that opted in: the variable on, outside production; the project holds `invoices`."""
    from tranibor_api.config import reset_settings_cache

    published = await client.put(
        f"/api/v1/projects/{project['id']}/workflows/invoices", json={"yaml": INVOICES}
    )
    assert published.status_code in (200, 201), published.text
    monkeypatch.setenv("TRANIBOR_DEMO_HISTORY_IMPORT", "1")
    reset_settings_cache()
    yield
    reset_settings_cache()
