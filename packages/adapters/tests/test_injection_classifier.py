"""The HTTP adapter of the injection classifier (ADR 0219) against a scripted LLM Guard-shaped service."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from tranibor_adapters.fakes.wire import Wire
from tranibor_adapters.injection_classifier import HttpInjectionClassifier
from tranibor_core.injection_classifier import ClassifierUnavailable, to_suspicions

PATH = "/analyze/prompt"


def _ok(score: float, valid: bool | None = None) -> tuple[int, Any]:
    return 200, {"is_valid": score < 0.5 if valid is None else valid, "scanners": {"PromptInjection": score}}


def _classifier(wire: Wire, **kw: Any) -> HttpInjectionClassifier:
    return HttpInjectionClassifier("http://guard:8000", client=wire.client(), **kw)


async def test_a_score_at_the_threshold_is_a_finding_and_a_low_one_is_not() -> None:
    wire = Wire({("POST", PATH): [_ok(0.97), _ok(0.02)]})
    found = await _classifier(wire).classify({"ticket.body": "a", "ticket.title": "b"})
    assert [(f.source, f.score) for f in found] == [("ticket.body", 0.97)]


async def test_the_service_saying_invalid_counts_even_under_our_threshold() -> None:
    wire = Wire({("POST", PATH): [_ok(0.3, valid=False)]})
    found = await _classifier(wire, threshold=0.9).classify({"ticket.body": "x"})
    assert [f.source for f in found] == ["ticket.body"]


async def test_the_request_carries_the_text_and_the_token_only_where_it_belongs() -> None:
    wire = Wire({("POST", PATH): [_ok(0.0)]})
    await _classifier(wire, token="s3cret").classify({"ticket.body": "hello"})
    (request,) = wire.requests
    assert json.loads(request.content) == {"prompt": "hello"}
    assert request.headers["authorization"] == "Bearer s3cret"
    assert "s3cret" not in repr(_classifier(wire, token="s3cret"))


async def test_a_long_text_is_cut_into_windows_and_scored_by_its_worst() -> None:
    wire = Wire({("POST", PATH): [_ok(0.1), _ok(0.95), _ok(0.2)]})
    found = await _classifier(wire, window_chars=10, max_windows=3).classify({"d": "x" * 100})
    assert len(wire.requests) == 3, "the text beyond the windows is not scored"
    assert [(f.source, f.score) for f in found] == [("d", 0.95)]


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
