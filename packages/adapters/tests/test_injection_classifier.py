"""The HTTP adapter of the injection classifier (ADR 0219) against a scripted LLM Guard-shaped service.

The scripted answers are the ones `laiyer/llm-guard-api:0.3.16` was seen sending (`docs/security.md`,
"Measured against a real LLM Guard"): `PromptInjection` is a risk score in [-1, 1], 0 being the
service's own threshold, and `is_valid` is the AND of every scanner the service runs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from tranibor_adapters.fakes.wire import Wire
from tranibor_adapters.injection_classifier import (
    DEFAULT_MAX_WINDOWS,
    DEFAULT_OVERLAP_CHARS,
    DEFAULT_WINDOW_CHARS,
    HttpInjectionClassifier,
    windows,
)
from tranibor_core.injection_classifier import ClassifierUnavailable, to_suspicions

PATH = "/analyze/prompt"


def _ok(risk: float, valid: bool | None = None) -> tuple[int, Any]:
    """A real-shaped answer of a service running `PromptInjection` alone."""
    return 200, {
        "is_valid": risk <= 0 if valid is None else valid,
        "scanners": {"PromptInjection": risk},
        "sanitized_prompt": "...",
    }


def _classifier(wire: Wire, **kw: Any) -> HttpInjectionClassifier:
    return HttpInjectionClassifier("http://guard:8000", client=wire.client(), **kw)


async def test_a_risk_above_the_service_threshold_is_a_finding_and_a_negative_one_is_not() -> None:
    wire = Wire({("POST", PATH): [_ok(1.0), _ok(-1.0)]})
    found = await _classifier(wire).classify({"ticket.body": "a", "ticket.title": "b"})
    assert [(f.source, f.score) for f in found] == [("ticket.body", 1.0)]


async def test_the_service_saying_invalid_counts_even_under_our_threshold() -> None:
    wire = Wire({("POST", PATH): [_ok(0.1, valid=False)]})
    found = await _classifier(wire, threshold=0.5).classify({"ticket.body": "x"})
    assert [f.source for f in found] == ["ticket.body"]


async def test_a_risk_rounded_to_minus_zero_is_recorded_as_zero_not_negative() -> None:
    # Live: a score a hair above the service's threshold comes back as `-0.0` with `is_valid: false`.
    wire = Wire({("POST", PATH): [_ok(-0.0, valid=False)]})
    (found,) = await _classifier(wire).classify({"ticket.body": "x"})
    assert found.score == 0.0 and "-" not in f"{found.score:.2f}"


async def test_another_scanner_saying_invalid_is_not_an_injection() -> None:
    """`is_valid` is the AND of every scanner. Live, the image's shipped `BanSubstrings` (`["test"]`)
    answered this for a benign ticket: no injection."""
    body = {"is_valid": False, "scanners": {"PromptInjection": -1.0, "BanSubstrings": 1.0}}
    wire = Wire({("POST", PATH): [(200, body)]})
    ticket = "Regression test suite fails on the build agent; please run the smoke test again."
    assert await _classifier(wire).classify({"ticket.body": ticket}) == []


async def test_beside_other_scanners_the_prompt_injection_risk_decides_on_the_service_threshold() -> None:
    body = {"is_valid": False, "scanners": {"PromptInjection": 0.3, "Language": -1.0}}
    wire = Wire({("POST", PATH): [(200, body)]})
    found = await _classifier(wire).classify({"ticket.body": "x"})
    assert [(f.source, f.score) for f in found] == [("ticket.body", 0.3)]


@pytest.mark.parametrize(
    "body",
    [
        {"is_valid": True, "scanners": {}},
        {"is_valid": True, "scanners": {"Language": -1.0}},
        {"is_valid": True, "scanners": {"PromptInjection": None}},
        {"is_valid": True},
    ],
)
async def test_an_answer_without_a_prompt_injection_score_is_no_verdict(body: dict[str, Any]) -> None:
    wire = Wire({("POST", PATH): [(200, body)]})
    with pytest.raises(ClassifierUnavailable):
        await _classifier(wire).classify({"ticket.body": "x"})


@pytest.mark.parametrize("threshold", [-1.5, 1.0, 50.0])
def test_a_threshold_off_the_risk_scale_is_refused(threshold: float) -> None:
    with pytest.raises(ValueError):
        HttpInjectionClassifier("http://guard:8000", threshold=threshold)


async def test_the_request_carries_the_text_and_the_token_only_where_it_belongs() -> None:
    wire = Wire({("POST", PATH): [_ok(0.0)]})
    await _classifier(wire, token="s3cret").classify({"ticket.body": "hello"})
    (request,) = wire.requests
    assert json.loads(request.content) == {"prompt": "hello"}
    assert request.headers["authorization"] == "Bearer s3cret"
    assert "s3cret" not in repr(_classifier(wire, token="s3cret"))


async def test_a_long_text_is_cut_into_windows_and_scored_by_its_worst() -> None:
    wire = Wire({("POST", PATH): [_ok(-1.0), _ok(1.0), _ok(-0.5)]})
    found = await _classifier(wire, window_chars=10, overlap_chars=4, max_windows=3).classify(
        {"d": "x" * 100}
    )
    assert len(wire.requests) == 3, "the text beyond the windows is not scored"
    assert [(f.source, f.score) for f in found] == [("d", 1.0)]


def test_windows_overlap_so_a_short_span_is_never_cut_in_two() -> None:
    text = "".join(chr(65 + i % 26) for i in range(1000))
    cut = windows(text, 100, 30, 50)
    assert all(len(w) <= 100 for w in cut) and cut[0] == text[:100] and cut[-1].endswith(text[-5:])
    for start in range(0, len(text) - 30):
        assert any(text[start : start + 30] in w for w in cut), start
    assert windows("short", 100, 30, 50) == ["short"]
    assert len(windows(text, 100, 30, 3)) == 3


def test_the_default_windows_are_short_enough_for_the_model_to_see_a_sentence() -> None:
    """Measured live: the model misses an injection it catches alone once it is diluted in ~800
    characters of ticket text (one 2,000-character window found 3 of 48, windows of 256 found 39).
    A window is at most 256 characters, LLM Guard's own `PROMPT_CHARACTERS_LIMIT`, so no `match_type`
    of the service cuts it; windows overlap by half, and a source is scored over about 8,000 characters."""
    assert DEFAULT_WINDOW_CHARS <= 256 and DEFAULT_OVERLAP_CHARS >= 128
    text = "y" * 20_000
    scored = windows(text, DEFAULT_WINDOW_CHARS, DEFAULT_OVERLAP_CHARS, DEFAULT_MAX_WINDOWS)
    assert all(len(w) <= 256 for w in scored)
    assert (len(scored) - 1) * (DEFAULT_WINDOW_CHARS - DEFAULT_OVERLAP_CHARS) + DEFAULT_WINDOW_CHARS >= 8_000


async def test_empty_sources_call_nothing() -> None:
    wire = Wire({})
    assert await _classifier(wire).classify({"a": "", "b": "  "}) == []
    assert wire.requests == []


@pytest.mark.parametrize(
    "reply",
    [
        (500, {"detail": "boom"}),
        (401, {"detail": "no"}),
        (200, "not json"),
        (200, {"unexpected": True}),
        (200, ["list"]),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
)
async def test_any_failure_is_unavailable_without_leaking_the_url_or_text(reply: Any) -> None:
    wire = Wire({("POST", PATH): reply})
    with pytest.raises(ClassifierUnavailable) as exc:
        await _classifier(wire, token="s3cret").classify({"ticket.body": "private words"})
    assert "s3cret" not in str(exc.value) and "private words" not in str(exc.value)


def test_findings_become_suspicions_recorded_like_the_lexical_ones() -> None:
    from tranibor_core.injection_classifier import ModelFinding

    (s,) = to_suspicions([ModelFinding("ticket.body", 0.931)], {"ticket.body": "line one\nline two"})
    assert s.to_dict() == {
        "source": "ticket.body",
        "motif": "classifier: prompt injection (0.93)",
        "extrait": "line one line two",
        "detector": "model",
    }
