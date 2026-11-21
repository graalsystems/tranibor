# SPDX-License-Identifier: Apache-2.0
"""Keyset pagination in SQL (ADR 0206): a list route reads one page from the database, never all of it.

The order is `(sort DESC, tiebreak DESC)` — a timestamp, then the primary key so that rows created in the
same instant keep a total order — or, for a list ordered by a name, `(name ASC, …, id ASC)`: any string
or timestamp column, joined tables included. The cursor is the key values of the last row returned,
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
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from .deps import Page
from .errors import unprocessable
from .schemas import PageMeta

#: What a sort key may hold: a timestamp or a string (a name, a slug, an e-mail). Never a number: the
#: cursor is text, and nothing sorted by a number needs it yet.
Scalar = datetime | str


def encode_cursor(*values: Scalar) -> str:
    """The key values of a row, tagged by type (a timestamp is not a string), base64-encoded."""
    tagged = [["t", value.isoformat()] if isinstance(value, datetime) else ["s", value] for value in values]
    raw = json.dumps(tagged).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[Scalar, ...]:
    """The key values a cursor carries; 422 for anything this module did not write."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values: list[Scalar] = []
        for kind, text in json.loads(raw):
            if not isinstance(text, str) or kind not in ("t", "s"):
                raise ValueError("cursor fields")
            values.append(datetime.fromisoformat(text) if kind == "t" else text)
        if not values:
            raise ValueError("empty cursor")
        return tuple(values)
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
    then: Sequence[InstrumentedAttribute[Any]] = (),
) -> tuple[list[Any], PageMeta]:
    """One page of `query` (a `select` of one ORM entity, filters already applied) and its meta.

    The order is `sort`, then each of `then`, then `tiebreak` (the primary key, so the order is total),
    all descending unless `descending=False` — a list ordered by name reads ascending. A key may be a
    column of a joined table: its values are read from the query, not from the entity.
    """
    keys = [sort, *then, tiebreak]
    if page.cursor:
        values = decode_cursor(page.cursor)
        if len(values) != len(keys) or any(
            isinstance(value, datetime) != isinstance(key.type, DateTime)
            for key, value in zip(keys, values, strict=True)
        ):
            raise unprocessable("invalid cursor: pass back the meta.next_cursor of the previous page")
        # Lexicographic: after the cursor row on the first key, or level with it and after on the next.
        after = [
            (key > value) if not descending else (key < value)
            for key, value in zip(keys, values, strict=True)
        ]
        level = [key == value for key, value in zip(keys, values, strict=True)]
        query = query.where(or_(*(and_(*level[:index], after[index]) for index in range(len(keys)))))
    ordering = [key.desc() if descending else key.asc() for key in keys]
    query = query.add_columns(*keys).order_by(*ordering).limit(page.limit + 1)
    fetched = (await session.execute(query)).all()
    has_more = len(fetched) > page.limit
    window = fetched[: page.limit]
    next_cursor = encode_cursor(*window[-1][1:]) if has_more else None
    return [row[0] for row in window], PageMeta(next_cursor=next_cursor, has_more=has_more)
