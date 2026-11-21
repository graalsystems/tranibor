# SPDX-License-Identifier: Apache-2.0
"""The extension of the `software-delivery` plugin (ADR 0181, round C2): what its trade needs inside the
platform's processes, registered through the core's seams when the API and the worker load the
`choregos.plugins` entry point.

- the twelve gates that read a diff, a pipeline or a pull request (`scope_respected`, `diff_size_max`,
  `no_secrets`, `markdown_sections`, `adr_number_free`, `docs_updated`, `coverage_delta_min`,
  `ci_green`, `review_approved`, `scans_ok`, `provenance_signed`, `already_fixed_proven`), under the
  names stored workflows carry (`gates.py`);
- the context provider `delivery` their facts come from, and its `judged` hook (`facts.py`);
- the step effects `open_pr` and `merge_pr` of `does:` (`effects.py`);
- the `scm.*`, `ci.*` and `cd.*` inbound event types, four finding types and two memory kinds, in the
  contracts' open lists (`lists.py`);
- the release train, its workflow `ReleaseTrain` and its activities, on the orchestrator's workers through
  the worker seam (round D, `train/`), under the names every recorded history carries;
- the train's routes (`listReleases`, `getRelease`, `getTrainStatus`, `departTrain`, `freezeTrain`,
  `unfreezeTrain`, `approveRelease`, `abortRelease`), through `extensions.declare_router` (`train/api.py`).

`register` is idempotent: the API loads the extensions twice (through the worker it imports, then in
`create_app`).
"""

from __future__ import annotations

from tranibor_core.gates import context_provider
from tranibor_core.worker_parts import WorkerParts, declare_worker_parts

from .effects import register_effects
from .facts import judged, read_delivery_facts
from .gates import PROVIDER, register_gates
from .lists import declare_values
from .names import NAME


def provide_worker_parts() -> WorkerParts:
    """What the plugin adds to the orchestrator's workers: called by the worker only, so that loading the
    extension (the API does too) imports no Temporal code. Every train activity runs on the queue of the
    workflow that calls it, `orchestrator`, as it did when the orchestrator carried them."""
    from .train import activities
    from .train.workflow import ReleaseTrain

    return WorkerParts(
        workflows=[ReleaseTrain],
        activities=[
            activities.load_train_config,
            activities.check_window,
            activities.create_release,
            activities.promote,
            activities.run_smoke,
            activities.soak,
            activities.request_approval,
            activities.promote_canary_step,
            activities.verify_prod,
            activities.finish_release,
            activities.confirm_auto_sync,
            # The French name stays registered: recorded histories and trains in flight use it (S34-02).
            activities.confirmer_auto_sync,  # lang: fr-ok
            activities.rollback,
            activities.mark_release,
            activities.apply_terraform,
            activities.train_stats,
        ],
    )


def declare_routes() -> None:
    """The train's routes, served by the API after the core's (`create_app` includes the declared routers
    when it starts). Idempotent: `declare_router` keeps one copy of a router."""
    from tranibor_api.extensions import declare_router

    from .train.api import router

    declare_router(router)


def register() -> None:
    """The `choregos.plugins` entry point of `tranibor-software-delivery`."""
    context_provider(PROVIDER, plugin=NAME, judged=judged)(read_delivery_facts)
    register_gates()
    register_effects()
    declare_values()
    declare_worker_parts(NAME, provide_worker_parts)
    declare_routes()
