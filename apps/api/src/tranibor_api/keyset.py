# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort, tiebreak)`, newest first by default — a timestamp, or a name with `descending=False` —
then the primary key so that rows sharing one value keep a total order. The cursor is the `(sort,
tiebreak)` pair of the last row returned,
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

from sqlalchemy import DateTime, Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .deps import Page
from .errors import unprocessable
from .schemas import PageMeta


def encode_cursor(sort_value: datetime | str, tiebreak_value: str) -> str:
    """The cursor of the row a page ends on: its sort value (an instant, or a name) and its tiebreak."""
    text = sort_value.isoformat() if isinstance(sort_value, datetime) else sort_value
    raw = json.dumps([text, tiebreak_value]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, text: bool = False) -> tuple[Any, str]:
    """The `(sort, tiebreak)` pair a cursor carries; 422 for anything this module did not write.

    `text=True` for a list sorted on a string column (a name): the sort value is kept as written.
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
    sort: InstrumentedAttribute[Any],
    tiebreak: InstrumentedAttribute[str],
    page: Page,
    descending: bool = True,
    cursor_values: Callable[[Any], tuple[Any, str]] | None = None,
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select` of ORM rows, filters already applied) and its meta.

    `sort` is a timestamp (newest first, the default) or a string column such as a name
    (`descending=False`: A to Z); `tiebreak` keeps the order total. A query that selects several
    entities (a join whose second column is the sort) passes `cursor_values`, which reads the
    `(sort, tiebreak)` of a result row, and gets the result rows back instead of the first entity.
    """
    ascending = not descending
    text = not isinstance(sort.type, DateTime)
    if page.cursor:
        last_sort, last_tiebreak = decode_cursor(page.cursor, text=text)
        if ascending:
            beyond = or_(sort > last_sort, and_(sort == last_sort, tiebreak > last_tiebreak))
        else:
            beyond = or_(sort < last_sort, and_(sort == last_sort, tiebreak < last_tiebreak))
        query = query.where(beyond)
    order = (sort.asc(), tiebreak.asc()) if ascending else (sort.desc(), tiebreak.desc())
    query = query.order_by(*order).limit(page.limit + 1)
    result = await session.execute(query)
    rows = list(result.all() if cursor_values else result.scalars().all())
    has_more = len(rows) > page.limit
    window = rows[: page.limit]
    next_cursor = None
    if has_more:
        last = window[-1]
        last_sort_value, last_tiebreak_value = (
            cursor_values(last) if cursor_values else (getattr(last, sort.key), getattr(last, tiebreak.key))
        )
        next_cursor = encode_cursor(last_sort_value, last_tiebreak_value)
    return window, PageMeta(next_cursor=next_cursor, has_more=has_more)
