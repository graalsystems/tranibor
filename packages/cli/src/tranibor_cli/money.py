# SPDX-License-Identifier: Apache-2.0
"""Every amount in the organisation's currency (ADR 0187 §1, S67-25).

The organisation's `currency` setting (`EUR`, `USD`, or any other ISO 4217 code with the rate the organisation
declared, S29-03, ADR 0214) is read once per process and organisation (`GET /orgs`), then every amount is
printed with its symbol (a code with no symbol of ours is printed with the code: `MXN 87.50`). A declared
currency is USD as measured times the organisation's declared rate; without that rate, never a guess.

The API serves some amounts in both currencies
(a cost report's total, a work item's totals), some in one only (a project's month in euros, a run's spend in
dollars). An amount the API gives in the other currency only is converted with the ratio the API's own cost
report shows (what the console does); with no ratio yet it is printed in the one it has, **with its own
symbol, never relabelled**. Unknown is "—",
never 0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .client import ApiError, Client

DASH = "—"
SYMBOLS = {"EUR": "€", "USD": "$"}
CURRENCY_CODE = re.compile(r"[A-Z]{3}")

#: `(API url, organisation)` -> its currency, or `None` when the API would not say: one read per process.
_CURRENCIES: dict[tuple[str, str], str | None] = {}
#: `(API url, organisation)` -> the rate it declared for a currency that is not EUR or USD (units per dollar).
_DECLARED: dict[tuple[str, str], float | None] = {}


def currency_code(org: dict[str, Any]) -> str | None:
    """The currency an `Org` payload names (EUR is the default, S29-03); `None` when it is no code."""
    value = org.get("currency") or "EUR"
    return value if isinstance(value, str) and CURRENCY_CODE.fullmatch(value) else None


def declared_rate(org: dict[str, Any]) -> float | None:
    """The units of the organisation's currency per dollar it declared (ADR 0214); `None` when none
    (EUR and USD need none: the ledger holds them)."""
    value = org.get("fx_rate")
    return (
        float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value > 0 else None
    )


def forget() -> None:
    """Drop what was read (tests, and a process that switches profile)."""
    _CURRENCIES.clear()
    _DECLARED.clear()
    _RATES.clear()


def currency_of(api: Client, organisation: str) -> str | None:
    """The organisation's currency; `None` when it cannot be read (then every amount keeps its own symbol)."""
    key = (api.profile.api_url, organisation)
    if key not in _CURRENCIES:
        found: str | None = None
        try:
            # the server finds it by its slug or a former one (ADR 0206): no list is read to look it up
            for org in api.get("/orgs", params={"slug": organisation, "limit": 1}, quiet=True)["items"]:
                if organisation == org.get("slug") or organisation in (org.get("aliases") or []):
                    found = currency_code(org)
                    _DECLARED[key] = declared_rate(org)
                    break
        except ApiError:
            found = None
        _CURRENCIES[key] = found
    return _CURRENCIES[key]


_RATES: dict[tuple[str, str], float | None] = {}


def rate_of(api: Client, organisation: str) -> float | None:
    """Euros per dollar as the organisation's own ledger applied them this month (the console's `usdTo`,
    `apps/web/src/lib/spend.ts`): the cost report's total carries both, so the ratio is the API's own figure.
    `None` when nothing was spent yet or the report cannot be read."""
    key = (api.profile.api_url, organisation)
    if currency_of(api, organisation) not in (None, *SYMBOLS):
        return _DECLARED.get(key)  # a declared currency: the organisation's own rate, not the ledger's ratio
    if key not in _RATES:
        found: float | None = None
        today = date.today()
        try:
            report = api.get(
                f"/orgs/{organisation}/costs",
                params={"since": today.replace(day=1).isoformat(), "until": today.isoformat()},
                quiet=True,
            )
            total = amount((report or {}).get("total"))
            if total.usd and total.eur:
                found = total.eur / total.usd
        except ApiError:
            found = None
        _RATES[key] = found
    return _RATES[key]


@dataclass(frozen=True)
class Amount:
    """What the API gave for one figure: its dollars, its euros, either or both (`None` is unknown)."""

    usd: float | None = None
    eur: float | None = None

    def text(self, currency: str | None, places: int = 2, rate: float | None = None) -> str:
        """`€1.24`: the organisation's currency when the API gave it or `rate` (euros per dollar) converts
        it; otherwise the other one with its own symbol, never relabelled."""
        order = ("EUR", "USD") if currency != "USD" else ("USD", "EUR")
        if currency is None:  # unknown preference: whatever is given, in its own symbol
            order = ("USD", "EUR") if self.eur is None else ("EUR", "USD")
        if rate:
            if currency is not None and currency not in SYMBOLS and self.usd is not None:
                return _show(
                    currency, self.usd * rate, places
                )  # declared: dollars as measured times the rate
            if currency == "EUR" and self.eur is None and self.usd is not None:
                return _show("EUR", self.usd * rate, places)
            if currency == "USD" and self.usd is None and self.eur is not None:
                return _show("USD", self.eur / rate, places)
        for code in order:
            value = self.eur if code == "EUR" else self.usd
            if value is not None:
                return _show(code, value, places)
        return DASH


def _show(code: str, value: float, places: int) -> str:
    return f"{SYMBOLS.get(code, code + ' ')}{float(value):.{places}f}"


class Pricing:
    """One organisation's currency and (read only when an amount needs it) its rate, for one command."""

    def __init__(self, api: Client, organisation: str | None) -> None:
        self.api, self.organisation = api, organisation

    @property
    def currency(self) -> str | None:
        return currency_of(self.api, self.organisation) if self.organisation else None

    def text(self, value: Amount, places: int = 2) -> str:
        currency = self.currency
        rate = None
        if self.organisation and (
            (currency == "EUR" and value.eur is None and value.usd is not None)
            or (currency == "USD" and value.usd is None and value.eur is not None)
            or (currency not in (None, *SYMBOLS) and value.usd is not None)
        ):
            rate = rate_of(self.api, self.organisation)
        return value.text(currency, places, rate)


def amount(source: object, *, usd: str = "cost_usd", eur: str = "cost_eur") -> Amount:
    """An `Amount` read from a payload's keys (a missing or null key is unknown)."""
    if not isinstance(source, dict):
        return Amount()
    return Amount(usd=_number(source.get(usd)), eur=_number(source.get(eur)))


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
