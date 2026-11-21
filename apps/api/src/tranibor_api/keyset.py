# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort DESC, tiebreak DESC)` — a timestamp, then the primary key so that rows created in the
same instant keep a total order — or the same pair ascending, on a string (a slug) for a list kept in
alphabetical order. The cursor is the `(sort, tiebreak)` pair of the last row returned,
base64-encoded: opaque to the client, stable when rows are inserted ahead of it (an offset is not), and
read with an index instead of a scan. One more row than the page is fetched to know whether another
page follows, with no `COUNT(*)`.

`Page.slice` (`deps.py`) is what this replaces: it loads up to a fixed cap in memory and cuts it in
Python, so whatever lies beyond the cap is unreachable and every page pays for the whole read.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, DateTime, Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .deps import Page
from .errors import unprocessable
from .schemas import PageMeta

#: What a list can be ordered by: a timestamp, or a string (a slug, a name).
SortValue = datetime | str


def encode_cursor(sort_value: SortValue, tiebreak_value: str) -> str:
    text = sort_value.isoformat() if isinstance(sort_value, datetime) else sort_value
    raw = json.dumps([text, tiebreak_value]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, text: bool = False) -> tuple[SortValue, str]:
    """The `(sort, tiebreak)` pair a cursor carries; 422 for anything this module did not write.

    `text` says the route orders by a string (a slug), not by a timestamp.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        sort_text, tiebreak = json.loads(raw)
        if not isinstance(sort_text, str) or not isinstance(tiebreak, str):
            raise ValueError("cursor fields")
        return (sort_text if text else datetime.fromisoformat(sort_text)), tiebreak
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as error:
        raise unprocessable("invalid cursor: pass back the meta.next_cursor of the previous page") from error


async def keyset_page(
    session: AsyncSession,
    query: Select[Any],
    *,
    sort: ColumnElement[Any] | InstrumentedAttribute[Any],
    tiebreak: InstrumentedAttribute[str],
    page: Page,
    descending: bool = True,
    sort_of: Callable[[Any], SortValue] | None = None,
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select` of ORM rows, filters already applied) and its meta.

    Ordered by `(sort, tiebreak)`, newest first unless `descending` is false (a list kept in alphabetical
    order). `sort` is a timestamp or string column — or an expression such as a `coalesce`, in which case
    `sort_of` reads the same value off a row, for the cursor.
    """
    as_text = not isinstance(sort.type, DateTime)
    if page.cursor:
        last_sort, last_tiebreak = decode_cursor(page.cursor, text=as_text)
        if descending:
            after = or_(sort < last_sort, and_(sort == last_sort, tiebreak < last_tiebreak))
        else:
            after = or_(sort > last_sort, and_(sort == last_sort, tiebreak > last_tiebreak))
        query = query.where(after)
    ordered = (sort.desc(), tiebreak.desc()) if descending else (sort.asc(), tiebreak.asc())
    query = query.order_by(*ordered).limit(page.limit + 1)
    rows = list((await session.execute(query)).scalars().all())
    has_more = len(rows) > page.limit
    window = rows[: page.limit]
    next_cursor = None
    if has_more:
        last = window[-1]
        value = sort_of(last) if sort_of else getattr(last, str(getattr(sort, "key", "")))
        next_cursor = encode_cursor(value, getattr(last, str(getattr(tiebreak, "key", ""))))
    return window, PageMeta(next_cursor=next_cursor, has_more=has_more)
