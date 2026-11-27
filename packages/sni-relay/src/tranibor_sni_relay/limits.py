# SPDX-License-Identifier: Apache-2.0
"""The relay's limits (ADR 0230 §8): per installation, concurrent streams, new streams per minute and
bytes per month; per client IP address, new connections per minute. Their values are the relay
operator's; past one, the connection is closed and its metadata line says which.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

#: How often a window forgets the keys it has not seen for a whole window.
_SWEEP_EVERY: Final = 1_024


@dataclass(frozen=True)
class RelayLimits:
    """The operator's values. The defaults suit one person's phone and a few webhooks."""

    #: Streams of one installation open at once, a pending dial-back included.
    concurrent_streams: int = 32
    #: New streams of one installation per minute.
    streams_per_minute: int = 240
    #: Bytes both ways through one installation's streams in a calendar month (UTC).
    bytes_per_month: int = 100 * 2**30
    #: New connections from one client address per minute, counted before anything is read.
    client_connections_per_minute: int = 120
    #: Control hellos per installation and per address per minute.
    hellos_per_minute: int = 12
    #: Pairing requests per address per minute.
    pairings_per_minute: int = 12

    def __post_init__(self) -> None:
        if min(vars(self).values()) < 1:
            raise ValueError("every limit is a positive number")


class SlidingWindow:
    """At most `limit` events per key over the last `window_s` seconds of `now`."""

    def __init__(self, limit: int, window_s: float, now: Callable[[], float]) -> None:
        self._limit = limit
        self._window = window_s
        self._now = now
        self._events: dict[str, deque[float]] = {}
        self._calls = 0

    def allow(self, key: str) -> bool:
        """Counts an event of `key`, unless the window is full."""
        moment = self._now()
        self._calls += 1
        if self._calls % _SWEEP_EVERY == 0:
            self._sweep(moment)
        events = self._events.setdefault(key, deque())
        while events and events[0] <= moment - self._window:
            events.popleft()
        if len(events) >= self._limit:
            return False
        events.append(moment)
        return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until `key` may have another event."""
        events = self._events.get(key)
        if not events or len(events) < self._limit:
            return 0
        return max(1, int(events[0] + self._window - self._now()) + 1)

    def _sweep(self, moment: float) -> None:
        for key in [
            key for key, events in self._events.items() if not events or events[-1] <= moment - self._window
        ]:
            del self._events[key]


class MonthlyBytes:
    """The bytes each installation moved in the current calendar month (UTC)."""

    def __init__(self, limit: int, now: Callable[[], float]) -> None:
        self._limit = limit
        self._now = now
        self._month = ""
        self._used: dict[str, int] = {}

    def _current(self) -> dict[str, int]:
        month = datetime.fromtimestamp(self._now(), UTC).strftime("%Y-%m")
        if month != self._month:
            self._month, self._used = month, {}
        return self._used

    def available(self, relay_id: str) -> bool:
        """Whether `relay_id` may open a stream: it has not reached the month's bytes."""
        return self._current().get(relay_id, 0) < self._limit

    def add(self, relay_id: str, size: int) -> bool:
        """Counts `size` bytes; whether `relay_id` is still within the month's bytes."""
        used = self._current()
        used[relay_id] = used.get(relay_id, 0) + size
        return used[relay_id] <= self._limit
