# SPDX-License-Identifier: Apache-2.0
"""An injection classifier behind an HTTP service the deployment provides (ADR 0219).

The shape called is the one of LLM Guard's API (`llm-guard-api`): `POST <path>` with
`{"prompt": "<text>"}`, answered with `{"is_valid": bool, "scanners": {"PromptInjection": <score>}}`,
the service running its `PromptInjection` scanner. Any service that answers that way will do. The
core ships no model and no client: the weights live in the service.

The service sees the texts a run is about to read: tickets, documents, memories. Point it at a
service the organisation runs; its URL is the deployment's (`TRANIBOR_INJECTION_CLASSIFIER_URL`), never
a project's or an agent's.

A text longer than a classifier's window is cut into windows of `window_chars`, at most `max_windows`
per source; what lies beyond is **not** scored (the lexical detector still reads all of it). The
source's score is its highest window's.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tranibor_core.injection_classifier import ClassifierUnavailable, ModelFinding

DEFAULT_PATH = "/analyze/prompt"
DEFAULT_THRESHOLD = 0.5
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_WINDOW_CHARS = 2000
DEFAULT_MAX_WINDOWS = 8
SCANNER = "PromptInjection"
_CONCURRENCY = 4


class HttpInjectionClassifier:
    """Calls the service once per window of text. Any failure is `ClassifierUnavailable`."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        path: str = DEFAULT_PATH,
        threshold: float = DEFAULT_THRESHOLD,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        window_chars: int = DEFAULT_WINDOW_CHARS,
        max_windows: int = DEFAULT_MAX_WINDOWS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/" + path.lstrip("/")
        self._token = token
        self._threshold = threshold
        self._timeout_s = timeout_s
        self._window = max(1, window_chars)
        self._max_windows = max(1, max_windows)
        self._client = client

    def __repr__(self) -> str:  # never the token
        return f"HttpInjectionClassifier({self._url})"

    async def classify(self, sources: dict[str, str]) -> list[ModelFinding]:
        jobs = [
            (name, text[i : i + self._window])
            for name, text in sources.items()
            if text and text.strip()
            for i in range(0, min(len(text), self._window * self._max_windows), self._window)
        ]
        if not jobs:
            return []
        gate = asyncio.Semaphore(_CONCURRENCY)
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout_s)
        try:

            async def one(window: str) -> tuple[bool, float]:
                async with gate:
                    return await self._score(client, window)

            scores = await asyncio.gather(*(one(w) for _, w in jobs))
        finally:
            if owned:
                await client.aclose()
        best: dict[str, tuple[bool, float]] = {}
        for (name, _), (invalid, score) in zip(jobs, scores, strict=True):
            before = best.get(name, (False, 0.0))
            best[name] = (before[0] or invalid, max(before[1], score))
        # The service's own `is_valid` counts as well as our threshold: the stricter of the two wins.
        return [
            ModelFinding(name, score)
            for name, (invalid, score) in best.items()
            if invalid or score >= self._threshold
        ]

    async def _score(self, client: httpx.AsyncClient, window: str) -> tuple[bool, float]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = await client.post(self._url, json={"prompt": window}, headers=headers)
            response.raise_for_status()
            body: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # The class only: a URL or a body could carry what the caller must not read back.
            raise ClassifierUnavailable(f"injection classifier call failed ({type(exc).__name__})") from exc
        return _verdict_of(body)


def _verdict_of(body: Any) -> tuple[bool, float]:
    """(the service calls it invalid, the scanner's score or 0)."""
    if not isinstance(body, dict) or not isinstance(body.get("is_valid"), bool):
        raise ClassifierUnavailable("injection classifier answered something other than a verdict")
    scanners = body.get("scanners")
    raw = scanners.get(SCANNER) if isinstance(scanners, dict) else None
    score = float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0.0
    return (not body["is_valid"], score)
