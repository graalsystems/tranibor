# SPDX-License-Identifier: Apache-2.0
"""The port of the model-based injection detector (ADR 0219), beside the lexical one.

`tranibor_core.injection` knows a list of patterns. A text written by a third party (an incident
description, a dealer's claim) can carry an instruction that no pattern names. A classifier service
the deployment provides scores the text instead; this module is only the port and the shape of its
answer, so the core stays free of model weights and of an HTTP client. The adapter that calls an
LLM Guard `PromptInjection` scanner is `tranibor_adapters.injection_classifier`, the scriptable
fake is `tranibor_adapters.fakes.injection_classifier`.

A classifier is probabilistic: it misses attacks and flags innocent text. A finding is a suspicion,
recorded and acted on like a lexical one; no finding is not a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .injection import Suspicion

#: `Suspicion.detector` of a finding of a classifier.
MODEL_DETECTOR = "model"


class ClassifierUnavailable(Exception):  # noqa: N818 - a named refusal, handled by the guard
    """The classifier could not give a verdict: unreachable, timed out, refused, or an answer that
    is not the one expected. Under `block` the step stops (fail closed), under `warn` it is logged."""


@dataclass(frozen=True, slots=True)
class ModelFinding:
    """One source the classifier scored at or above the deployment's threshold."""

    source: str
    score: float
    label: str = "prompt injection"


class InjectionClassifier(Protocol):
    """A service that scores named texts. It returns only the findings (score at or above its
    threshold), and raises `ClassifierUnavailable` when it cannot answer for every source."""

    async def classify(self, sources: dict[str, str]) -> list[ModelFinding]: ...


def to_suspicions(findings: list[ModelFinding], sources: dict[str, str]) -> list[Suspicion]:
    """The classifier's findings in the form the lexical detector's are recorded in."""
    return [
        Suspicion(
            f.source,
            f"classifier: {f.label} ({f.score:.2f})",
            sources.get(f.source, "")[:120].replace("\n", " "),
            detector=MODEL_DETECTOR,
        )
        for f in findings
    ]
