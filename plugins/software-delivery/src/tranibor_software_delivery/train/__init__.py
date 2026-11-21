# SPDX-License-Identifier: Apache-2.0
"""The release train (ADR 0181, round D): its workflow `ReleaseTrain` (`workflow.py`) and its activities
(`activities.py`), carried by the orchestrator's workers through the worker seam. Nothing here is imported
when the extension loads: only the worker's call to `provide_worker_parts` imports them."""
