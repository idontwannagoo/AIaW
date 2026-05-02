"""Cursor pagination helpers — Stage 4 硬前置 1.

Goal: when a list endpoint may return tens of thousands of rows (messages /
artifacts), let the client opt into bounded-page cursor续拉 instead of pulling
the whole table in one response.

Design:
- Page key = `version` (the global_change_seq value on each row). Versions are
  strictly increasing per row, so `since=<last_version>` resumes exactly where
  the previous page left off.
- Backwards compatible: only when the client sends `?limit=`, the endpoint
  switches its response shape from `list[Row]` to `{rows, next_cursor}`. Old
  callers (Stage 1-3 small-table list consumers) keep their array shape.
- `next_cursor` is non-null whenever the current page is full (`len(rows) ==
  limit`); the client should re-fetch with `since=next_cursor` until it gets
  back null. We deliberately let the client see one extra empty page rather
  than try to "peek ahead" — saves a +1 fetch per page on the hot path and
  makes the protocol stateless.
- Hard cap (`CURSOR_MAX_LIMIT`) prevents a misbehaving client from asking for
  the whole table in one request. Values above the cap are silently clamped.

Use from a router:

    page = paginate(rows, limit=fetch_limit, version_attr='version')
    if page is None:
        return rows  # no limit → bare list (back-compat)
    return page  # CursorPage envelope

`fetch_limit` is the SQL `LIMIT` you ran with — i.e. what `normalize_limit`
returned for the query. Pass `None` to skip cursor mode.
"""
from __future__ import annotations

from typing import Generic, Optional, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel

T = TypeVar('T')

# Keep above what real Stage 4 pages will request (200 in plan) so a sane
# "give me more than the default" still works without server-side rejection.
CURSOR_MAX_LIMIT = 1000


class CursorPage(BaseModel, Generic[T]):
    rows: list[T]
    next_cursor: Optional[int] = None


def normalize_limit(limit: Optional[int]) -> Optional[int]:
    """Validate the client-provided limit. Returns the SQL LIMIT to use, or
    None when the client didn't ask for pagination.

    Raises 400 on `<= 0`. Silently clamps values above CURSOR_MAX_LIMIT — we'd
    rather give the client a partial page than reject. The clamp is observable
    via `next_cursor` so honest clients still see "more rows pending".
    """
    if limit is None:
        return None
    if limit <= 0:
        raise HTTPException(status_code=400, detail='limit must be > 0')
    return min(limit, CURSOR_MAX_LIMIT)


def build_page(rows: list[T], fetch_limit: int) -> CursorPage[T]:
    """Wrap fetched rows in a CursorPage. Cursor is the last row's `version`
    when the page is full, else None. Caller is responsible for ordering rows
    by ascending version so `rows[-1]` is the highest-watermark row.
    """
    next_cursor: Optional[int] = None
    if rows and len(rows) >= fetch_limit:
        last = rows[-1]
        # Pydantic models / SQLAlchemy rows both expose `.version`; bare dicts
        # don't, so we accept either shape rather than force one.
        if hasattr(last, 'version'):
            next_cursor = int(getattr(last, 'version'))
        elif isinstance(last, dict):
            next_cursor = int(last.get('version', 0)) or None
    return CursorPage[T](rows=rows, next_cursor=next_cursor)
