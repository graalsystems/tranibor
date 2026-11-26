# SPDX-License-Identifier: Apache-2.0
"""Train metrics read the train's real query and field (S26-03).

`_trains` asked for a query named `status` — the train defines `status_query` — and read a `state`
field the train does not return. The error was swallowed, so `choregos_train_frozen` and
`choregos_train_status` (`tranibor_*` since ADR 0226, R9b) never had a sample.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def test_a_frozen_train_is_exported(client: AsyncClient, project: dict[str, Any]) -> None:
    from tranibor_api.metrics import refresh
    from tranibor_api.temporal import FakeTemporal, get_temporal, train_id

    fake = get_temporal()
    assert isinstance(fake, FakeTemporal)
    # The answer the workflow's `status_query` gives (the plugin's `train/workflow.py`), nothing more.
    fake.queries[f"{train_id('billing-api', 'prod')}:status_query"] = {
        "status": "staging",
        "batch_size": 1,
        "pending_items": ["BILLING-API-1"],
        "frozen": True,
        "freeze_reason": "incident",
        "next_departure": None,
        "window_open": True,
        "release_id": None,
    }
    await refresh()
    body = (await client.get("/metrics")).text
    for prefix in ("tranibor", "choregos"):  # the former name too, until 0.32.0 (ADR 0226, R9b)
        assert f'{prefix}_train_frozen{{env="prod",project="billing-api"}} 1.0' in body
        assert f'{prefix}_train_status{{env="prod",project="billing-api"}} 2.0' in body


def test_the_metrics_ask_a_query_the_train_defines() -> None:
    from tranibor_software_delivery.names import ReleaseStatus
    from tranibor_software_delivery.train.metrics import TRAIN_STATES, TRAIN_STATUS_QUERY
    from tranibor_software_delivery.train.workflow import ReleaseTrain

    assert callable(getattr(ReleaseTrain, TRAIN_STATUS_QUERY, None))
    assert set(TRAIN_STATES) <= {str(s) for s in ReleaseStatus}, "every mapped state is a real status"
