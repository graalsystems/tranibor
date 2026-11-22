# tranibor-software-delivery

The extension of the `software-delivery` plugin ([ADR 0181](../../docs/adr/0181-the-contracts-speak-no-trade.md)):
the code software delivery needs inside the platform's processes. The plugin's content (agents, skills,
procedures) is published from the publishing identity's agents repository (`tranibor_core.publishing`).

Since round C2 it holds the twelve gates that read a diff, a pipeline or a pull request, and the context
provider `delivery` whose facts they judge. The published API and worker images install it; the
`choregos.plugins` entry point `software-delivery` registers it at start-up.

Since round D it also holds the release train: its workflow `ReleaseTrain` and its activities
(`train/`), which the orchestrator's workers carry through the worker seam
(`tranibor_core.worker_parts`) under the names every recorded history carries, and the train's routes
(`train/api.py`), which the API serves through `extensions.declare_router`.
