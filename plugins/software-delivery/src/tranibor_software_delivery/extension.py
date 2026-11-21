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
  the worker seam (round D, `train/`), under the names every recorded history carries, and the carrier
  `via: release_train` (`carriers.declare_carrier`), whose boarding activity `signal_train` it registers;
- the train's routes (`listReleases`, `getRelease`, `getTrainStatus`, `departTrain`, `freezeTrain`,
  `unfreezeTrain`, `approveRelease`, `abortRelease`), through `extensions.declare_router` (`train/api.py`);
- a run's diff (`getRunDiff`) and a project's DORA metrics (`getProjectDora`), the same way (`api.py`), and
  the webhooks of its pipelines and deployments (`tektonWebhook`, `jenkinsWebhook`, `argocdWebhook`,
  `webhooks.py`);
- the project figures `Project.has_release_train` and `ProjectStats.trains_pending`, through
  `extensions.declare_project_figure` (`figures.py`);
- the train's Prometheus series (`choregos_releases_total`, `choregos_train_frozen`,
  `choregos_train_status`), through `metrics.declare_series` (`train/metrics.py`);
- the `scm`, `ci` and `cd` connector types (GitHub, GitLab, Azure Repos; Tekton, GitLab CI, GitHub Actions,
  Jenkins, Azure Pipelines; Argo CD and the demonstration environment) and their fakes, through
  `tranibor_adapters.register`, and the `scm`, `ci` and `cd` slots of every project's adapter set, through
  `declare_adapter_slot` (`connectors/`); the source control's activities (`open_pull_request`,
  `enqueue_merge`, `ensure_branch`, `collect_run_artifacts`, `check_scope_violations`, `source_control.py`)
  through the worker seam; and the
  provisioning steps that use the `cd` connector (`gitops.write_project_manifests`, `argocd.wait_synced`),
  through the worker seam (`provisioning.py`).

`register` is idempotent: the API loads the extensions twice (through the worker it imports, then in
`create_app`).
"""

from __future__ import annotations

from tranibor_core.carriers import declare_carrier
from tranibor_core.gates import context_provider
from tranibor_core.worker_parts import WorkerParts, declare_worker_parts

from .connectors import register_connectors
from .effects import register_effects
from .facts import judged, read_delivery_facts
from .gates import PROVIDER, register_gates
from .lists import declare_values
from .names import NAME


def provide_worker_parts() -> WorkerParts:
    """What the plugin adds to the orchestrator's workers: called by the worker only, so that loading the
    extension (the API does too) imports no Temporal code. Every train activity runs on the queue of the
    workflow that calls it, `orchestrator`, as it did when the orchestrator carried them."""
    from . import source_control
    from .provisioning import STEPS
    from .train import activities, boarding
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
            # The interpreter's boarding of a `via: release_train` transition, called by name.
            boarding.signal_train,
            # The source control's, called by name by the interpreter (`does: open_pr`, `does: merge_pr`,
            # after every agent step), on the `tracker` queue as they were.
            source_control.open_pull_request,
            source_control.enqueue_merge,
            source_control.ensure_branch,
            source_control.collect_run_artifacts,
            source_control.check_scope_violations,
        ],
        # The source control's on the `tracker` queue, as when the orchestrator carried them;
        # `collect_run_artifacts` is placed there by the core's table, for its stand-in too.
        queues=dict.fromkeys(
            ("open_pull_request", "enqueue_merge", "ensure_branch", "check_scope_violations"), "tracker"
        ),
        # The steps of a template's provisioning that write to and read from the `cd` connector.
        provisioning_steps=STEPS,
    )


def declare_api_parts() -> None:
    """What the plugin adds to the API: its routes, served after the core's (`create_app` includes the
    declared routers when it starts), the project figures it fills and its Prometheus series. Idempotent:
    `declare_router` keeps one copy of a router, `declare_project_figure` one function per field,
    `declare_series` one declaration per plugin."""
    from tranibor_api.extensions import (
        declare_inbound_hook,
        declare_project_figure,
        declare_readiness_item,
        declare_router,
    )
    from tranibor_api.metric_series import declare_series
    from tranibor_contracts import InboundEventType

    from . import figures
    from .api import router as delivery_router
    from .merged import board_merged_item
    from .readiness import item_repository
    from .train import metrics
    from .train.api import router
    from .webhooks import router as webhooks_router

    declare_router(router)
    declare_router(delivery_router)
    declare_router(webhooks_router)
    declare_project_figure("Project", "has_release_train", figures.has_release_train, plugin=NAME)
    declare_project_figure("ProjectStats", "trains_pending", figures.trains_pending, plugin=NAME)
    declare_series(NAME, metrics.NAMES, metrics.refresh)
    declare_readiness_item("repository", item_repository, plugin=NAME)
    declare_inbound_hook(str(InboundEventType.PR_MERGED), board_merged_item, plugin=NAME)


def register() -> None:
    """The `choregos.plugins` entry point of `tranibor-software-delivery`."""
    register_connectors()
    context_provider(PROVIDER, plugin=NAME, judged=judged)(read_delivery_facts)
    register_gates()
    register_effects()
    declare_values()
    declare_worker_parts(NAME, provide_worker_parts)
    declare_carrier("release_train", plugin=NAME)
    declare_api_parts()
