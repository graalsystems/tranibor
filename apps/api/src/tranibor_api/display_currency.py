# SPDX-License-Identifier: Apache-2.0
"""The currency an organisation reads its spend in (S29-03, ADR 0214).

The ledger stays in USD as the gateway measured it (and, beside it, in EUR at the rate of the day of
each write). An organisation may choose any ISO 4217 currency to read it in. `EUR` and `USD` come from
the ledger's own columns. Any other code is USD converted at **the rate the organisation declared**, with
the date it set it: there is no default rate, so a converted amount is never a guess of ours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

#: The currencies the ledger holds natively: they need no declared rate.
LEDGER_CURRENCIES = frozenset({"EUR", "USD"})

CURRENCY_SETTING = "currency"
RATE_SETTING = "fx_rate"
RATE_DATE_SETTING = "fx_rate_set_on"

#: The ISO 4217 codes in circulation (active codes, funds and the precious metals left out).
ISO_4217 = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN
    BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF
    CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB
    EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG
    HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF
    KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA
    MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO
    NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD
    RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN
    SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX
    USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW
    ZWG
    """.split()  # noqa: SIM905 - a readable table
)


@dataclass(frozen=True)
class DisplayCurrency:
    """What an organisation reads its spend in: a code and, for a non-ledger code, its declared rate."""

    code: str = "EUR"
    #: Units of `code` per 1 USD, as the organisation declared it; `None` for `EUR` and `USD`.
    rate: float | None = None
    set_on: date | None = None

    @property
    def declared(self) -> bool:
        return self.rate is not None

    def convert(self, usd: float) -> float:
        """USD in the display currency, to the ledger's six decimals."""
        return round(usd * self.rate, 6) if self.rate is not None else usd


def read(settings: dict[str, Any] | None) -> DisplayCurrency:
    """The display currency an organisation's settings hold: EUR when it has none, and a code that is
    not a ledger currency with no usable rate falls back to the ledger's EUR rather than to a guess."""
    settings = settings or {}
    code = settings.get(CURRENCY_SETTING)
    if code == "USD":
        return DisplayCurrency("USD")
    rate = settings.get(RATE_SETTING)
    if (
        isinstance(code, str)
        and code in ISO_4217
        and code not in LEDGER_CURRENCIES
        and isinstance(rate, int | float)
        and not isinstance(rate, bool)
        and rate > 0
    ):
        stamped = settings.get(RATE_DATE_SETTING)
        return DisplayCurrency(code, float(rate), date.fromisoformat(stamped) if stamped else None)
    return DisplayCurrency("EUR")
