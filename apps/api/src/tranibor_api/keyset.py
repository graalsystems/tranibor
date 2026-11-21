# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort, tiebreak)`, newest first by default: a timestamp (or a string such as a slug), then
the primary key so that rows created in the same instant keep a total order. The cursor is the
`(sort, tiebreak)` pair of the last row returned, base64-encoded: opaque to the client, stable when rows
are inserted ahead of it (an offset is not), and read with an index instead of a scan. One more row than
the page is fetched to know whether another page follows, with no `COUNT(*)`.

The in-memory `Page.slice` this replaced loaded up to a fixed cap and cut it in Python, so whatever lay
beyond the cap was unreachable and every page paid for the whole read.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .deps import Page
from .errors import unprocessable
from .schemas import PageMeta


def encode_cursor(sort_value: datetime | str, tiebreak_value: str) -> str:
    """A timestamp is tagged `t` and a string `s`, so that each comes back as the type it left as."""
    tagged = ["t", sort_value.isoformat()] if isinstance(sort_value, datetime) else ["s", sort_value]
    raw = json.dumps([*tagged, tiebreak_value]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime | str, str]:
    """The `(sort, tiebreak)` pair a cursor carries; 422 for anything this module did not write."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        kind, sort_text, tiebreak = json.loads(raw)
        if kind not in ("t", "s") or not isinstance(sort_text, str) or not isinstance(tiebreak, str):
            raise ValueError("cursor fields")
        return (datetime.fromisoformat(sort_text) if kind == "t" else sort_text), tiebreak
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
    tiebreak_descending: bool | None = None,
    sort_of: Callable[[Any], datetime | str] | None = None,
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select` of ORM rows, filters already applied) and its meta.

    `sort` is a column or any SQL expression (a `coalesce` of two timestamps); it is read off a row with
    `sort_of` when it is not a plain column. `descending` is the direction of `sort`, and the tiebreak
    follows it unless `tiebreak_descending` says otherwise (a route whose order was `ts DESC, id ASC`).
    """
    tiebreak_desc = descending if tiebreak_descending is None else tiebreak_descending
    if page.cursor:
        last_sort, last_tiebreak = decode_cursor(page.cursor)
        sort_after = sort < last_sort if descending else sort > last_sort
        tie_after = tiebreak < last_tiebreak if tiebreak_desc else tiebreak > last_tiebreak
        query = query.where(or_(sort_after, and_(sort == last_sort, tie_after)))
    query = query.order_by(
        sort.desc() if descending else sort.asc(), tiebreak.desc() if tiebreak_desc else tiebreak.asc()
    ).limit(page.limit + 1)
    rows = list((await session.execute(query)).scalars().all())
    has_more = len(rows) > page.limit
    window = rows[: page.limit]
    next_cursor = None
    if has_more:
        last = window[-1]
        value = sort_of(last) if sort_of else getattr(last, str(sort.key))
        next_cursor = encode_cursor(value, getattr(last, tiebreak.key))
    return window, PageMeta(next_cursor=next_cursor, has_more=has_more)
