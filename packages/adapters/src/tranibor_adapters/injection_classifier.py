# SPDX-License-Identifier: Apache-2.0
"""An injection classifier behind an HTTP service the deployment provides (ADR 0219).

The shape called is the one of LLM Guard's API (`llm-guard-api`): `POST <path>` with
`{"prompt": "<text>"}`, answered with `{"is_valid": bool, "scanners": {"PromptInjection": <risk>}}`,
the service running its `PromptInjection` scanner. Any service that answers that way will do. The
core ships no model and no client: the weights live in the service.

What the real service sends (measured against `laiyer/llm-guard-api:0.3.16`, see
`docs/security.md`, "Measured against a real LLM Guard"):

- the `PromptInjection` value is a **risk score in [-1, 1]**, not a probability: 0 is the scanner's own
  threshold (set in the service's `scanners.yml`, 0.92 by default), above it is positive, rounded to
  one decimal. Our threshold is on that scale, and its default, 0, follows the service's own verdict.
- `is_valid` is the AND of **every** scanner the service runs. It speaks for `PromptInjection` only
  when that scanner answered alone; beside a `Language` or `BanSubstrings` scanner it does not say
  "injection", and is then not read.
- an answer without a `PromptInjection` score is not a verdict: the scanner is not running.

The service sees the texts a run is about to read: tickets, documents, memories. Point it at a
service the organisation runs; its URL is the deployment's (`TRANIBOR_INJECTION_CLASSIFIER_URL`), never
a project's or an agent's.

The model classifies a whole input: an injection diluted in a long benign text is missed (measured:
a sentence the model catches alone is missed inside 800 characters of ticket text, and one window of
2,000 characters found 3 of 48 injections set in ~1,500 characters of tickets, windows of 256 found
39). So a text is cut into short, overlapping windows of `window_chars` (256), each `overlap_chars`
(128) into the previous one, at most `max_windows` (64, about 8,300 characters) per source; what lies
beyond is **not** scored (the lexical detector still reads all of it). The source's score is its
highest window's.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tranibor_core.injection_classifier import ClassifierUnavailable, ModelFinding

DEFAULT_PATH = "/analyze/prompt"
#: On LLM Guard's risk-score scale, [-1, 1), where 0 is the service's own scanner threshold.
DEFAULT_THRESHOLD = 0.0
DEFAULT_TIMEOUT_S = 5.0
#: 256 is also LLM Guard's own `PROMPT_CHARACTERS_LIMIT`: a window that long is scored whole under
#: every `match_type` the service may run (`full`, `truncate_head_tail`, `chunks`, `sentence`).
DEFAULT_WINDOW_CHARS = 256
DEFAULT_OVERLAP_CHARS = 128
DEFAULT_MAX_WINDOWS = 64
SCANNER = "PromptInjection"
_CONCURRENCY = 4


def windows(text: str, size: int, overlap: int, most: int) -> list[str]:
    """`text` cut into windows of `size` characters, each starting `size - overlap` after the
    previous one, at most `most` of them. Any span of at most `overlap` characters lies whole in a
    window (within the scored part of the text)."""
    size = max(1, size)
    stride = max(1, size - max(0, overlap))
    starts = [0]
    while starts[-1] + size < len(text) and len(starts) < max(1, most):
        starts.append(starts[-1] + stride)
    return [text[start : start + size] for start in starts]


class HttpInjectionClassifier:
    """Calls the service once per window of text. Any failure is `ClassifierUnavailable`."""

    def __init__(  # noqa: PLR0913 - the deployment's knobs, all keyword-only
        self,
        base_url: str,
        *,
        token: str | None = None,
        path: str = DEFAULT_PATH,
        threshold: float = DEFAULT_THRESHOLD,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        window_chars: int = DEFAULT_WINDOW_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        max_windows: int = DEFAULT_MAX_WINDOWS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not -1.0 <= threshold < 1.0:
            raise ValueError("the threshold is on LLM Guard's risk-score scale, [-1, 1)")
        self._url = base_url.rstrip("/") + "/" + path.lstrip("/")
        self._token = token
        self._threshold = threshold
        self._timeout_s = timeout_s
        self._window = max(1, window_chars)
        self._overlap = min(max(0, overlap_chars), self._window - 1)
        self._max_windows = max(1, max_windows)
        self._client = client

    def __repr__(self) -> str:  # never the token
        return f"HttpInjectionClassifier({self._url})"

    async def classify(self, sources: dict[str, str]) -> list[ModelFinding]:
        jobs = [
            (name, window)
            for name, text in sources.items()
            if text and text.strip()
            for window in windows(text, self._window, self._overlap, self._max_windows)
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
            before = best.get(name, (False, -1.0))
            best[name] = (before[0] or invalid, max(before[1], score))
        # The service's own verdict counts as well as our threshold: the stricter of the two wins. A
        # risk score is negative below the service's threshold; a finding never records less than 0.
        return [
            ModelFinding(name, max(0.0, score))
            for name, (invalid, score) in best.items()
            if invalid or score > self._threshold
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
    """(the service calls the text an injection, the scanner's risk score)."""
    if not isinstance(body, dict) or not isinstance(body.get("is_valid"), bool):
        raise ClassifierUnavailable("injection classifier answered something other than a verdict")
    scanners = body.get("scanners")
    raw = scanners.get(SCANNER) if isinstance(scanners, dict) else None
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        # No score is no verdict: a service whose PromptInjection scanner is not running must not
        # read as a service that found nothing.
        raise ClassifierUnavailable(f"injection classifier answered without a {SCANNER} score")
    # `is_valid` is the AND of every scanner the service runs: it speaks for this one only alone.
    alone = isinstance(scanners, dict) and set(scanners) == {SCANNER}
    return (alone and not body["is_valid"], float(raw))
