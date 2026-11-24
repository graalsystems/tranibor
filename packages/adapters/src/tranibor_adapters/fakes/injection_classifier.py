# SPDX-License-Identifier: Apache-2.0
"""A scriptable injection classifier (ADR 0005, ADR 0219): the fake of `InjectionClassifier`.

It scores a text by the phrases the test gave it, or fails as a service that is down would. It
records what it was asked, so a test can say what left for the classifier and what did not.
"""

from __future__ import annotations

from tranibor_core.injection_classifier import ClassifierUnavailable, ModelFinding


class FakeInjectionClassifier:
    def __init__(
        self,
        phrases: dict[str, float] | None = None,
        *,
        threshold: float = 0.5,
        down: bool = False,
    ) -> None:
        #: lower-case phrase -> the score a text holding it gets.
        self.phrases = {k.lower(): v for k, v in (phrases or {}).items()}
        self.threshold = threshold
        self.down = down
        self.calls: list[dict[str, str]] = []

    async def classify(self, sources: dict[str, str]) -> list[ModelFinding]:
        self.calls.append(dict(sources))
        if self.down:
            raise ClassifierUnavailable("fake injection classifier is down")
        found: list[ModelFinding] = []
        for name, text in sources.items():
            score = max((s for p, s in self.phrases.items() if p in text.lower()), default=0.0)
            if score >= self.threshold:
                found.append(ModelFinding(name, score))
        return found
