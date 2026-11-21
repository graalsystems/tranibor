# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort, [then…,] tiebreak)`, descending by default or ascending for a log read
chronologically: a timestamp (or any comparable column), then the primary key so that rows created in the
same instant keep a total order. The cursor is the key of the last row returned, base64-encoded: opaque to
the client, stable when rows are inserted ahead of it (an offset is not), and read with an index instead of
a scan. One more row than the page is fetched to know whether another page follows, with no `COUNT(*)`.

`Page.slice` (`deps.py`) is what this replaces: it loads up to a fixed cap in memory and cuts it in
Python, so whatever lies beyond the cap is unreachable and every page pays for the whole read.

`position_page` is the same contract over a list that is not a table: a snapshot the row already holds
(the skills, the inputs or the knowledge bases a run's `StageInput` names), cut by position.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import ColumnElement, Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .errors import unprocessable
from .schemas import PageMeta

if TYPE_CHECKING:  # `deps` imports the services, which import this module
    from .deps import Page

#: What a key of the order may be: a timestamp, a number or a string.
Kind = type[datetime] | type[int] | type[str]
Direction = Literal["asc", "desc"]
#: A column of an entity, or of a subquery.
Key = ColumnElement[Any] | InstrumentedAttribute[Any]


def encode_cursor(*values: datetime | int | str) -> str:
    raw = json.dumps([v.isoformat() if isinstance(v, datetime) else v for v in values]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, kinds: Sequence[Kind] = (datetime, str)) -> tuple[Any, ...]:
    """The key a cursor carries, one value per `kinds`; 422 for anything this module did not write."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) != len(kinds):
            raise ValueError("cursor shape")
        decoded: list[Any] = []
        for value, kind in zip(values, kinds, strict=True):
            if kind is datetime and isinstance(value, str):
                decoded.append(datetime.fromisoformat(value))
            elif (kind is int and isinstance(value, int) and not isinstance(value, bool)) or (
                kind is str and isinstance(value, str)
            ):
                decoded.append(value)
            else:
                raise ValueError("cursor field")
        return tuple(decoded)
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as error:
        raise unprocessable("invalid cursor: pass back the meta.next_cursor of the previous page") from error


def _kind_of(column: Key) -> Kind:
    python_type = column.type.python_type
    if python_type in (datetime, int, str):
        return python_type
    raise TypeError(f"a keyset cannot be built on a {python_type.__name__} column")


def _after(keys: Sequence[Key], values: Sequence[Any], direction: Direction) -> ColumnElement[bool]:
    """The rows strictly after `values` in the order: `k1 > v1 OR (k1 = v1 AND k2 > v2) OR …`."""
    clauses: list[ColumnElement[bool]] = []
    for index, key in enumerate(keys):
        beyond = key > values[index] if direction == "asc" else key < values[index]
        ties = [keys[before] == values[before] for before in range(index)]
        clauses.append(and_(*ties, beyond) if ties else beyond)
    return or_(*clauses)


async def keyset_page(
    session: AsyncSession,
    query: Select[Any],
    *,
    sort: Key,
    tiebreak: Key,
    page: Page,
    then: Sequence[Key] = (),
    direction: Direction = "desc",
    rows: bool = False,
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select` of ORM rows, filters already applied) and its meta.

    The order is `(sort, *then, tiebreak)`, descending unless `direction="asc"` (a log read oldest
    first: the order a client that tails it needs). `rows=True` is for a `select` of columns, whose
    rows come back as they are instead of as entities.
    """
    keys = [sort, *then, tiebreak]
    if page.cursor:
        values = decode_cursor(page.cursor, [_kind_of(key) for key in keys])
        query = query.where(_after(keys, values, direction))
    query = query.order_by(*[key.asc() if direction == "asc" else key.desc() for key in keys])
    result = await session.execute(query.limit(page.limit + 1))
    found = list(result.all() if rows else result.scalars().all())
    has_more = len(found) > page.limit
    window = found[: page.limit]
    next_cursor = None
    if has_more:
        last = window[-1]
        next_cursor = encode_cursor(*[getattr(last, str(key.key)) for key in keys])
    return window, PageMeta(next_cursor=next_cursor, has_more=has_more)


def position_page[T](items: Sequence[T], page: Page) -> tuple[list[T], PageMeta]:
    """One page of a list the caller already holds and that does not change while it is read.

    For a snapshot a row carries (the skills, inputs or knowledge bases a run's `StageInput` names, fixed
    when the run was prepared): the cursor is the position of the next item. The caller cuts BEFORE it
    reads what each item costs to read, so a page pays for its own items only.
    """
    start = 0
    if page.cursor:
        (start,) = decode_cursor(page.cursor, (int,))
        if start < 0:
            raise unprocessable("invalid cursor: pass back the meta.next_cursor of the previous page")
    window = list(items[start : start + page.limit])
    has_more = start + page.limit < len(items)
    return window, PageMeta(
        next_cursor=encode_cursor(start + page.limit) if has_more else None, has_more=has_more
    )
