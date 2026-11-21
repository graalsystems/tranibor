# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort, tiebreak)`, newest first by default (`descending`) — a timestamp, then the primary
key so that rows created in the same instant keep a total order. `sort` may also be a name (a string, read
ascending: the order of a list sorted by name) or a number (a version). The cursor is the `(sort,
tiebreak)` pair of the last row returned, base64-encoded: opaque to the client, stable when rows are
inserted ahead of it (an offset is not), and read with an index instead of a scan. One more row than the
page is fetched to know whether another page follows, with no `COUNT(*)`.

`Page.slice` (`deps.py`) is what this replaces: it loads up to a fixed cap in memory and cuts it in
Python, so whatever lies beyond the cap is unreachable and every page pays for the whole read.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .deps import Page
from .errors import unprocessable
from .schemas import PageMeta

#: What a keyset can sort on: a timestamp, a name or a number. The cursor tags the type it carries.
Scalar = datetime | str | int


def encode_cursor(sort_value: Scalar, tiebreak_value: str) -> str:
    if isinstance(sort_value, datetime):
        tagged: list[Any] = ["t", sort_value.isoformat()]
    elif isinstance(sort_value, int):
        tagged = ["i", sort_value]
    else:
        tagged = ["s", sort_value]
    raw = json.dumps([tagged, tiebreak_value]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[Scalar, str]:
    """The `(sort, tiebreak)` pair a cursor carries; 422 for anything this module did not write."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        (tag, value), tiebreak = json.loads(raw)
        if not isinstance(tiebreak, str):
            raise ValueError("cursor tiebreak")
        if tag == "t" and isinstance(value, str):
            return datetime.fromisoformat(value), tiebreak
        if tag == "i" and isinstance(value, int) and not isinstance(value, bool):
            return value, tiebreak
        if tag == "s" and isinstance(value, str):
            return value, tiebreak
        raise ValueError("cursor sort")
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as error:
        raise unprocessable("invalid cursor: pass back the meta.next_cursor of the previous page") from error


def _entity_value(row: Any, column: InstrumentedAttribute[Any]) -> Any:
    """The value of `column` on `row`: the row itself for a one-entity `select`, else the entity of the
    `select` the column belongs to (a join selects several)."""
    if hasattr(row, "_mapping"):
        row = row._mapping[column.class_]
    return getattr(row, column.key)


async def keyset_page(
    session: AsyncSession,
    query: Select[*tuple[Any, ...]],
    *,
    sort: InstrumentedAttribute[Any],
    tiebreak: InstrumentedAttribute[str],
    page: Page,
    descending: bool = True,
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select` of ORM rows, filters already applied) and its meta.

    A `select` of one entity gives its rows; a `select` of several (a join) gives `Row` tuples, in the
    order of the entities."""
    if page.cursor:
        last_sort, last_tiebreak = decode_cursor(page.cursor)
        before = (
            (sort < last_sort, tiebreak < last_tiebreak)
            if descending
            else (sort > last_sort, tiebreak > last_tiebreak)
        )
        query = query.where(or_(before[0], and_(sort == last_sort, before[1])))
    order = (sort.desc(), tiebreak.desc()) if descending else (sort.asc(), tiebreak.asc())
    query = query.order_by(*order).limit(page.limit + 1)
    result = await session.execute(query)
    rows = list(result.scalars().all()) if len(query.column_descriptions) == 1 else list(result.all())
    has_more = len(rows) > page.limit
    window = rows[: page.limit]
    next_cursor = None
    if has_more:
        last = window[-1]
        next_cursor = encode_cursor(_entity_value(last, sort), _entity_value(last, tiebreak))
    return window, PageMeta(next_cursor=next_cursor, has_more=has_more)
