# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort, tiebreak)`, descending unless a route says otherwise — a timestamp, a name or a
count, then the primary key so that rows sharing a value keep a total order. The cursor is the
`(sort, tiebreak)` pair of the last row returned, base64-encoded: opaque to the client, stable when
rows are inserted ahead of it (an offset is not), and read with an index instead of a scan. One more
row than the page is fetched to know whether another page follows, with no `COUNT(*)`.

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

from sqlalchemy import ColumnElement, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute

from .deps import Page
from .errors import unprocessable
from .schemas import PageMeta

#: What a sort key may be: an instant, a name (a slug, a title) or a count.
SortValue = datetime | str | int


def encode_cursor(sort_value: SortValue, tiebreak_value: str) -> str:
    text = sort_value.isoformat() if isinstance(sort_value, datetime) else sort_value
    raw = json.dumps([text, tiebreak_value]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, kind: type = datetime) -> tuple[Any, str]:
    """The `(sort, tiebreak)` pair a cursor carries, the sort read as a `kind` (`datetime`, `str` or
    `int`); 422 for anything this module did not write, or wrote for another sort."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        sort_raw, tiebreak = json.loads(raw)
        if not isinstance(tiebreak, str):
            raise ValueError("cursor tiebreak")
        if kind is datetime:
            if not isinstance(sort_raw, str):
                raise ValueError("cursor sort")
            return datetime.fromisoformat(sort_raw), tiebreak
        if kind is int:
            if isinstance(sort_raw, bool) or not isinstance(sort_raw, int):
                raise ValueError("cursor sort")
            return sort_raw, tiebreak
        if not isinstance(sort_raw, str):
            raise ValueError("cursor sort")
        return sort_raw, tiebreak
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as error:
        raise unprocessable("invalid cursor: pass back the meta.next_cursor of the previous page") from error


def _kind_of(sort: ColumnElement[Any] | QueryableAttribute[Any]) -> type:
    try:
        python_type = sort.type.python_type
    except NotImplementedError:
        return str
    return python_type if python_type in (datetime, int) else str


def _beyond(
    column: ColumnElement[Any] | QueryableAttribute[Any], value: Any, *, descending: bool
) -> ColumnElement[bool]:
    clause: ColumnElement[bool] = column < value if descending else column > value
    return clause


async def keyset_page(
    session: AsyncSession,
    query: Any,
    *,
    sort: ColumnElement[Any] | QueryableAttribute[Any],
    tiebreak: ColumnElement[Any] | QueryableAttribute[Any],
    page: Page,
    sort_descending: bool = True,
    tiebreak_descending: bool = True,
    row_key: Callable[[Any], tuple[SortValue, str]] | None = None,
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select`, filters already applied) and its meta.

    By default the rows are ORM objects and the cursor reads `sort` and `tiebreak` off the last one
    (their attribute names). A query selecting several things (`select(Release, Agent.slug)`) passes
    `row_key`, which reads the `(sort, tiebreak)` pair off a result row, and gets the rows back whole.
    `sort` may be a column or any SQL expression with a type (a name, an instant, a count); each of the
    two keys has its own direction, so a list ordered by name ascending keeps that order.
    """
    if page.cursor:
        last_sort, last_tiebreak = decode_cursor(page.cursor, _kind_of(sort))
        query = query.where(
            or_(
                _beyond(sort, last_sort, descending=sort_descending),
                and_(sort == last_sort, _beyond(tiebreak, last_tiebreak, descending=tiebreak_descending)),
            )
        )
    query = query.order_by(
        sort.desc() if sort_descending else sort.asc(),
        tiebreak.desc() if tiebreak_descending else tiebreak.asc(),
    ).limit(page.limit + 1)
    result = await session.execute(query)
    rows = list(result.scalars().all() if row_key is None else result.all())
    has_more = len(rows) > page.limit
    window = rows[: page.limit]
    next_cursor = None
    if has_more:
        last = window[-1]
        if row_key is None:
            last_pair: tuple[SortValue, str] = (
                getattr(last, getattr(sort, "key", "")),
                getattr(last, getattr(tiebreak, "key", "")),
            )
        else:
            last_pair = row_key(last)
        next_cursor = encode_cursor(*last_pair)
    return window, PageMeta(next_cursor=next_cursor, has_more=has_more)
