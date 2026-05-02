"""Stage 4 硬前置 1 — cursor pagination on list endpoints.

Plan判据 (硬前置 1)：单用户 1 万条 messages，`?since=0&limit=200` 第一次响应
< 1MB，next_cursor 非空；循环续拉 ≥ 50 次拉完。

messages 表本身要等 Stage 4 主体批次-4e 落地，这里用 providers 当代表性 pilot
（同样的 list 路径 + global_change_seq），只验证机制：limit 解析 + 切片 +
next_cursor 正确性 + 边界 + 错误 limit 拒绝 + 向后兼容（不传 limit 行为不变）。
其他 4 张已迁表（reactives / assistants / installed_plugins / avatar_images）按需
后续批次再加 spec — 当前 list 端点不带 limit 走 bare list，本组测试不触动。
"""
from __future__ import annotations

import httpx
import pytest


# ---- happy path -------------------------------------------------------------


async def test_no_limit_returns_bare_list_back_compat(
    client_a: httpx.AsyncClient,
) -> None:
    """No `limit` query → response is `list[Row]` (Stage 1 behavior). This is
    the contract Stage 1-3 frontend code relies on."""
    for i in range(3):
        await client_a.put(
            f'/api/v1/providers/p{i}', json={'kind': 'openai', 'name': f'p{i}'}
        )
    r = await client_a.get('/api/v1/providers')
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list), f'expected bare list, got {type(body).__name__}: {body}'
    assert {row['id'] for row in body} == {'p0', 'p1', 'p2'}


async def test_with_limit_returns_envelope(
    client_a: httpx.AsyncClient,
) -> None:
    """`?limit=N` → response is `{rows, next_cursor}`."""
    for i in range(3):
        await client_a.put(
            f'/api/v1/providers/p{i}', json={'kind': 'openai', 'name': f'p{i}'}
        )
    r = await client_a.get('/api/v1/providers?limit=10')
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict), f'expected envelope dict, got {body}'
    assert 'rows' in body and 'next_cursor' in body
    assert {row['id'] for row in body['rows']} == {'p0', 'p1', 'p2'}
    # 3 rows fits well under limit=10 → no continuation
    assert body['next_cursor'] is None


async def test_full_page_returns_next_cursor(
    client_a: httpx.AsyncClient,
) -> None:
    """When the page exactly fills `limit`, next_cursor is the last row's
    version. Even if no more rows remain — the client gets one extra empty
    fetch but the protocol stays stateless."""
    for i in range(5):
        await client_a.put(
            f'/api/v1/providers/p{i}', json={'name': f'p{i}'}
        )
    r = await client_a.get('/api/v1/providers?limit=3')
    assert r.status_code == 200
    body = r.json()
    rows = body['rows']
    assert len(rows) == 3
    assert [row['id'] for row in rows] == ['p0', 'p1', 'p2']
    assert body['next_cursor'] is not None
    assert body['next_cursor'] == rows[-1]['version']


async def test_cursor_resume_with_since_param(
    client_a: httpx.AsyncClient,
) -> None:
    """The cursor protocol: client uses returned next_cursor as `since` to
    fetch the next page. Loop terminates when rows is empty OR next_cursor
    is null."""
    # Write 7 providers so we get 7/3 = 3 pages of size 3 then a 4th of size 1.
    for i in range(7):
        await client_a.put(f'/api/v1/providers/p{i}', json={'i': i})

    collected: list[str] = []
    since = 0
    page_count = 0
    while True:
        page_count += 1
        r = await client_a.get(f'/api/v1/providers?since={since}&limit=3')
        assert r.status_code == 200
        body = r.json()
        rows = body['rows']
        collected.extend(row['id'] for row in rows)
        if not rows:
            break
        if body['next_cursor'] is None:
            break
        since = body['next_cursor']
    assert collected == [f'p{i}' for i in range(7)], (
        f'expected p0..p6 in version order, got {collected}'
    )
    # 7 rows / page_size=3 → pages of 3, 3, 1 (→ next_cursor=None on last) = 3 pages
    assert page_count == 3, f'expected 3 fetches, got {page_count}'


async def test_clamp_limit_above_max(
    client_a: httpx.AsyncClient,
) -> None:
    """Limit > CURSOR_MAX_LIMIT (1000) is silently clamped, not rejected."""
    # Write a couple of rows then ask for a huge limit.
    await client_a.put('/api/v1/providers/p0', json={'i': 0})
    r = await client_a.get('/api/v1/providers?limit=1000000')
    assert r.status_code == 200
    body = r.json()
    # Behaves correctly: response is envelope, rows came back, no cursor
    # because we have fewer rows than the (clamped) limit.
    assert isinstance(body, dict)
    assert len(body['rows']) == 1
    assert body['next_cursor'] is None


# ---- error paths ------------------------------------------------------------


async def test_zero_limit_rejected(
    client_a: httpx.AsyncClient,
) -> None:
    r = await client_a.get('/api/v1/providers?limit=0')
    assert r.status_code == 400, r.text


async def test_negative_limit_rejected(
    client_a: httpx.AsyncClient,
) -> None:
    r = await client_a.get('/api/v1/providers?limit=-3')
    assert r.status_code == 400, r.text


# ---- isolation --------------------------------------------------------------


async def test_pagination_respects_account_isolation(
    client_a: httpx.AsyncClient,
    client_b: httpx.AsyncClient,
    user_a, user_b,
) -> None:
    """Cursor-pagination must filter by user_id — same as the bare-list path."""
    for i in range(3):
        await client_a.put(f'/api/v1/providers/a{i}', json={'i': i})
    for i in range(2):
        await client_b.put(f'/api/v1/providers/b{i}', json={'i': i})

    r = await client_a.get('/api/v1/providers?limit=10')
    body_a = r.json()
    assert {row['id'] for row in body_a['rows']} == {'a0', 'a1', 'a2'}

    r = await client_b.get('/api/v1/providers?limit=10')
    body_b = r.json()
    assert {row['id'] for row in body_b['rows']} == {'b0', 'b1'}


async def test_pagination_includes_tombstones_after_delete(
    client_a: httpx.AsyncClient,
) -> None:
    """Soft-delete still bumps version; `?since` increment-fetch should see
    the tombstone in cursor mode just like in bare-list mode."""
    await client_a.put('/api/v1/providers/p0', json={'i': 0})
    r = await client_a.delete('/api/v1/providers/p0')
    assert r.status_code == 200
    r = await client_a.get('/api/v1/providers?since=0&limit=10')
    body = r.json()
    rows = body['rows']
    # PUT then DELETE → 2 versions for the same id; the latest (deleted=True)
    # should be included.
    last = [r for r in rows if r['id'] == 'p0'][-1]
    assert last['deleted'] is True
