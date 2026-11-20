# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort DESC, tiebreak DESC)` for a timestamp — newest first, the primary key making rows created
in the same instant a total order — or `(sort ASC, tiebreak ASC)` for a name or a slug. The cursor is the
`(sort, tiebreak)` pair of the last row returned, base64-encoded: opaque to the client, stable when rows are
inserted ahead of it (an offset is not), and read with an index instead of a scan. One more row than the page
is fetched to know whether another page follows, with no `COUNT(*)`.

`Page.slice` (`deps.py`) is what this replaces: it loads up to a fixed cap in memory and cuts it in
Python, so whatever lies beyond the cap is unreachable and every page pays for the whole read.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .deps import Page
from .errors import unprocessable
from .schemas import PageMeta


def encode_cursor(sort_value: datetime | str, tiebreak_value: str) -> str:
    text = sort_value.isoformat() if isinstance(sort_value, datetime) else sort_value
    raw = json.dumps([text, tiebreak_value]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, text: bool = False) -> tuple[Any, str]:
    """The `(sort, tiebreak)` pair a cursor carries; 422 for anything this module did not write.

    The sort value is a `datetime` (a timestamp column) or, with `text=True`, the string of a name or slug.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        sort_text, tiebreak = json.loads(raw)
        if not isinstance(sort_text, str) or not isinstance(tiebreak, str):
            raise ValueError("cursor fields")
        return (sort_text if text else datetime.fromisoformat(sort_text)), tiebreak
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as error:
        raise unprocessable("invalid cursor: pass back the meta.next_cursor of the previous page") from error


#: Rows a `keep` filter looks at per round trip while it fills a page (`keyset_page`).
SCAN_BATCH = 200


async def keyset_page(
    session: AsyncSession,
    query: Select[Any],
    *,
    sort: InstrumentedAttribute[Any],
    tiebreak: InstrumentedAttribute[str],
    page: Page,
    descending: bool = True,
    keep: Callable[[list[Any]], Awaitable[list[Any]]] | None = None,
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select` of ORM rows, filters already applied) and its meta.

    `sort` is a timestamp column (newest first, the default) or a string column read in its natural order
    (`descending=False`: a name or a slug); `tiebreak` is the primary key. The cursor is typed by the column.

    `keep` is for a rule SQL cannot express (a permission read from a JSON column, a grant table): it
    receives a batch of rows in order and returns those that stay. The page is filled from successive
    batches of `SCAN_BATCH` rows read after the cursor, so the cost is the rows scanned to find
    `limit + 1` that stay, never the whole table, and the cursor is the last row *returned*.
    """
    as_text = issubclass(sort.type.python_type, str)
    sort_col, tie_col = (sort.desc(), tiebreak.desc()) if descending else (sort.asc(), tiebreak.asc())
    if page.cursor:
        last_sort, last_tiebreak = decode_cursor(page.cursor, text=as_text)
        ahead = (
            (sort < last_sort, tiebreak < last_tiebreak)
            if descending
            else (sort > last_sort, tiebreak > last_tiebreak)
        )
        query = query.where(or_(ahead[0], and_(sort == last_sort, ahead[1])))
    query = query.order_by(sort_col, tie_col)
    wanted = page.limit + 1
    if keep is None:
        rows = list((await session.execute(query.limit(wanted))).scalars().all())
    else:
        rows = []
        offset = 0
        while len(rows) < wanted:
            batch = list((await session.execute(query.limit(SCAN_BATCH).offset(offset))).scalars().all())
            rows += await keep(batch)
            offset += SCAN_BATCH
            if len(batch) < SCAN_BATCH:
                break
    has_more = len(rows) > page.limit
    window = rows[: page.limit]
    next_cursor = None
    if has_more:
        last = window[-1]
        next_cursor = encode_cursor(getattr(last, sort.key), getattr(last, tiebreak.key))
    return window, PageMeta(next_cursor=next_cursor, has_more=has_more)
