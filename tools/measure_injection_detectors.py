# SPDX-License-Identifier: Apache-2.0
"""Measure the injection detectors on a labelled corpus: the patterns, the classifier, both (ADR 0219).

    docker run ... laiyer/llm-guard-api:0.3.16   # docs/security.md, "Measured against a real LLM Guard"
    TRANIBOR_INJECTION_CLASSIFIER_TOKEN=... uv run python tools/measure_injection_detectors.py \\
        --url http://127.0.0.1:18431 --json /tmp/measure.json

It reads `tests/fixtures/injection/corpus.yaml` (or `--corpus`), and for every text asks the lexical
detector (`tranibor_core.injection.suspicions`) and the platform's own HTTP adapter
(`HttpInjectionClassifier`, with its defaults) pointed at a real classifier service, timing each
`classify` call as the guard makes it (one source, its windows sent by the adapter). "Both" is what
the guard does: a text is flagged when either flags it.

Then the long-text set: every English injection of the corpus put at the head, the middle and the
tail of about 1,500 characters of the corpus's English benign tickets, beside two padding-only
controls, scored by the adapter with its windows and with one window of 2,000 characters (the
adapter's window before ADR 0219 was measured), to show what dilution does.

It prints a Markdown report; `--json` also writes every verdict and latency. `--patterns-only` needs no
service: it scores the lexical patterns alone, per language, on `--corpus` (for instance the held-out
`tests/fixtures/injection/heldout-es-fr.yaml`), and names each miss and each benign text flagged.
The token is read from `TRANIBOR_INJECTION_CLASSIFIER_TOKEN` and never printed. Nothing here is a
test: it measures a model on texts we wrote, and says so.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import pathlib
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "injection" / "corpus.yaml"
LABELS = ("benign", "injection")
LANGS = ("en", "es", "fr")
DOMAINS = ("itsm", "warranty")
#: The benign English tickets the long-text set is padded with: plain ones, no trap in them.
PADDING_IDS = ("en-b-02", "en-b-07", "en-b-08", "en-b-09", "en-b-10", "en-b-11", "en-b-14", "en-b-16")
PADDING_CHARS = 1500
#: The adapter's window before this measurement, to compare with: one window of 2,000 characters.
OLD_WINDOW_CHARS = 2000


@dataclass(frozen=True, slots=True)
class Sample:
    id: str
    lang: str
    domain: str
    label: str
    trap: str
    text: str

    @property
    def injection(self) -> bool:
        return self.label == "injection"


def load_corpus(path: pathlib.Path = CORPUS) -> list[Sample]:
    """The corpus, checked: unique ids, known labels, languages and domains, non-empty texts."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    samples = [
        Sample(
            id=str(item["id"]),
            lang=str(item["lang"]),
            domain=str(item["domain"]),
            label=str(item["label"]),
            trap=str(item.get("trap", "none")),
            text=str(item["text"]),
        )
        for item in raw["texts"]
    ]
    seen: set[str] = set()
    for s in samples:
        if s.id in seen:
            raise ValueError(f"{s.id}: duplicate id")
        seen.add(s.id)
        if s.label not in LABELS or s.lang not in LANGS or s.domain not in DOMAINS or not s.text.strip():
            raise ValueError(f"{s.id}: label, lang, domain or text out of the corpus's terms")
    return samples


@dataclass(frozen=True, slots=True)
class Confusion:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def false_positive_rate(self) -> float | None:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else None


def confusion(truth: Sequence[bool], flagged: Sequence[bool]) -> Confusion:
    """True/false positives and negatives of `flagged` against `truth` (True: an injection)."""
    if len(truth) != len(flagged):
        raise ValueError("one verdict per text")
    pairs = list(zip(truth, flagged, strict=True))
    return Confusion(
        tp=sum(t and f for t, f in pairs),
        fp=sum(f and not t for t, f in pairs),
        fn=sum(t and not f for t, f in pairs),
        tn=sum(not t and not f for t, f in pairs),
    )


def percentile(values: Iterable[float], q: float) -> float | None:
    """The nearest-rank percentile `q` (0-100) of `values`, None when there are none."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def _cut(text: str, at: int) -> int:
    """The position of the space nearest before `at`, so a cut never splits a word."""
    space = text.rfind(" ", 0, at)
    return space if space > 0 else at


def long_variants(samples: Sequence[Sample]) -> list[Sample]:
    """Every English injection at the head, middle and tail of ~1,500 characters of benign English
    tickets, and two padding-only controls."""
    by_id = {s.id: s for s in samples}
    padding = " ".join(by_id[i].text for i in PADDING_IDS)
    pad = padding[: _cut(padding, PADDING_CHARS)]
    half = _cut(pad, len(pad) // 2)
    out = [
        Sample("long-control-1", "en", "itsm", "benign", "padding only", pad),
        Sample("long-control-2", "en", "itsm", "benign", "padding only", padding[-PADDING_CHARS:].lstrip()),
    ]
    for s in samples:
        if s.lang != "en" or not s.injection:
            continue
        out += [
            Sample(f"{s.id}-head", "en", s.domain, "injection", "long, head", f"{s.text} {pad}"),
            Sample(
                f"{s.id}-middle",
                "en",
                s.domain,
                "injection",
                "long, middle",
                f"{pad[:half]} {s.text} {pad[half:]}",
            ),
            Sample(f"{s.id}-tail", "en", s.domain, "injection", "long, tail", f"{pad} {s.text}"),
        ]
    return out


@dataclass(frozen=True, slots=True)
class Verdict:
    id: str
    lang: str
    label: str
    trap: str
    chars: int
    patterns: bool
    classifier: bool
    score: float | None
    latency_ms: float


async def judge(classifier: Any, samples: Sequence[Sample]) -> list[Verdict]:
    """Each text through the patterns and the classifier, one `classify` call at a time, timed."""
    from tranibor_core.injection import suspicions

    verdicts = []
    for s in samples:
        start = time.perf_counter()
        found = await classifier.classify({s.id: s.text})
        elapsed = (time.perf_counter() - start) * 1000
        verdicts.append(
            Verdict(
                id=s.id,
                lang=s.lang,
                label=s.label,
                trap=s.trap,
                chars=len(s.text),
                patterns=bool(suspicions(s.text, source=s.id)),
                classifier=bool(found),
                score=found[0].score if found else None,
                latency_ms=round(elapsed, 1),
            )
        )
    return verdicts


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f} %"


def detector_rows(verdicts: Sequence[Verdict]) -> list[tuple[str, Confusion]]:
    truth = [v.label == "injection" for v in verdicts]
    return [
        ("patterns alone", confusion(truth, [v.patterns for v in verdicts])),
        ("classifier alone", confusion(truth, [v.classifier for v in verdicts])),
        ("both (either flags)", confusion(truth, [v.patterns or v.classifier for v in verdicts])),
    ]


def table(verdicts: Sequence[Verdict]) -> str:
    lines = [
        "| detector | TP | FP | FN | TN | precision | recall | false-positive rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, c in detector_rows(verdicts):
        lines.append(
            f"| {name} | {c.tp} | {c.fp} | {c.fn} | {c.tn} | {_pct(c.precision)} | {_pct(c.recall)} "
            f"| {_pct(c.false_positive_rate)} |"
        )
    return "\n".join(lines)


def latency_line(verdicts: Sequence[Verdict]) -> str:
    ms = [v.latency_ms for v in verdicts]
    p50, p95 = percentile(ms, 50), percentile(ms, 95)
    return f"latency per text (ms): p50 {p50}, p95 {p95}, max {max(ms) if ms else None}, n {len(ms)}"


def report(corpus: Sequence[Verdict], long_new: Sequence[Verdict], long_old: Sequence[Verdict]) -> str:
    parts = ["## Corpus, all languages", table(corpus), "", latency_line(corpus), ""]
    for lang in LANGS:
        subset = [v for v in corpus if v.lang == lang]
        parts += [f"## Corpus, `{lang}` ({len(subset)} texts)", table(subset), "", latency_line(subset), ""]
    for name, rows in (("adapter windows", long_new), ("one 2,000-character window", long_old)):
        injected = [v for v in rows if v.label == "injection"]
        controls = [v for v in rows if v.label == "benign"]
        parts += [
            f"## Long texts, {name}",
            f"classifier recall {sum(v.classifier for v in injected)}/{len(injected)}; "
            f"controls flagged {sum(v.classifier for v in controls)}/{len(controls)}; " + latency_line(rows),
            "",
        ]
    misses = [v.id for v in corpus if v.label == "injection" and not (v.patterns or v.classifier)]
    false = [v.id for v in corpus if v.label == "benign" and (v.patterns or v.classifier)]
    parts += [
        f"missed by both: {', '.join(misses) or 'none'}",
        f"benign flagged: {', '.join(false) or 'none'}",
    ]
    return "\n".join(parts)


def pattern_verdicts(samples: Sequence[Sample]) -> list[Verdict]:
    """Each text through the lexical patterns alone; no classifier is asked."""
    from tranibor_core.injection import suspicions

    return [
        Verdict(
            id=s.id,
            lang=s.lang,
            label=s.label,
            trap=s.trap,
            chars=len(s.text),
            patterns=bool(suspicions(s.text, source=s.id)),
            classifier=False,
            score=None,
            latency_ms=0.0,
        )
        for s in samples
    ]


def patterns_report(verdicts: Sequence[Verdict]) -> str:
    """The patterns' confusion over all texts and per language, then what they missed and flagged."""
    rows = [
        "| texts | TP | FP | FN | TN | precision | recall | false-positive rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    subsets = [("all", list(verdicts))] + [(lang, [v for v in verdicts if v.lang == lang]) for lang in LANGS]
    for name, subset in subsets:
        if not subset:
            continue
        c = confusion([v.label == "injection" for v in subset], [v.patterns for v in subset])
        rows.append(
            f"| {name}, {len(subset)} | {c.tp} | {c.fp} | {c.fn} | {c.tn} | {_pct(c.precision)} "
            f"| {_pct(c.recall)} | {_pct(c.false_positive_rate)} |"
        )
    misses = [v.id for v in verdicts if v.label == "injection" and not v.patterns]
    false = [v.id for v in verdicts if v.label == "benign" and v.patterns]
    rows += ["", f"missed: {', '.join(misses) or 'none'}", f"benign flagged: {', '.join(false) or 'none'}"]
    return "\n".join(rows)


async def run(url: str, token: str | None, corpus_path: pathlib.Path) -> dict[str, list[Verdict]]:
    from tranibor_adapters.injection_classifier import HttpInjectionClassifier

    samples = load_corpus(corpus_path)
    current = HttpInjectionClassifier(url, token=token)
    old = HttpInjectionClassifier(
        url, token=token, timeout_s=60.0, window_chars=OLD_WINDOW_CHARS, overlap_chars=0, max_windows=8
    )
    longs = long_variants(samples)
    return {
        "corpus": await judge(current, samples),
        "long_adapter": await judge(current, longs),
        "long_one_window": await judge(old, longs),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", help="the classifier service, e.g. http://127.0.0.1:18431")
    parser.add_argument(
        "--patterns-only", action="store_true", help="score the lexical patterns alone, no service"
    )
    parser.add_argument("--corpus", type=pathlib.Path, default=CORPUS)
    parser.add_argument("--json", type=pathlib.Path, help="also write every verdict here")
    args = parser.parse_args(argv)
    if args.patterns_only:
        print(patterns_report(pattern_verdicts(load_corpus(args.corpus))))
        return 0
    if not args.url:
        parser.error("--url is required unless --patterns-only")
    token = os.environ.get("TRANIBOR_INJECTION_CLASSIFIER_TOKEN") or None
    results = asyncio.run(run(args.url, token, args.corpus))
    print(report(results["corpus"], results["long_adapter"], results["long_one_window"]))
    if args.json:
        args.json.write_text(
            json.dumps({k: [asdict(v) for v in vs] for k, vs in results.items()}, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
