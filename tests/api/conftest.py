"""Shared pytest fixtures for the backend API layer (Phase 3).

Assumes the test stack is already up:
    pnpm test:up && pnpm test:backend:start

The backend-start.sh script runs `alembic upgrade head` for us, so we only
need to TRUNCATE tables (and reset the global_change_seq) between tests; we
do not drop+recreate the schema. TRUNCATE is materially faster and avoids
having to reach into the running backend's connection pool.

Fixtures provided:
- _backend_up        — session-scoped, hard-fails the run if 9011 is silent
- db_reset           — autouse function-scoped table reset
- pg_conn            — sync psycopg connection to 5434 for direct asserts
- register_user      — helper: registers a fresh account, returns dict with tokens
- user_a / user_b    — pre-registered accounts for two-user isolation cases
- client_a / client_b — httpx.AsyncClient pre-loaded with each user's bearer
- anon_client        — unauth httpx.AsyncClient
- ws_connect         — async helper to open the realtime WS with bearer subproto
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
import websockets
from websockets.asyncio.client import ClientConnection

BACKEND_URL = os.environ.get('TEST_BACKEND_URL', 'http://127.0.0.1:9011')
WS_URL = os.environ.get(
    'TEST_BACKEND_WS_URL', 'ws://127.0.0.1:9011/api/v1/stream'
)
PG_DSN = os.environ.get(
    'TEST_PG_DSN',
    'postgresql://aiaw:aiaw_test@localhost:5434/aiaw_test',
)


# ---- session bootstrap ------------------------------------------------------


@pytest.fixture(scope='session', autouse=True)
def _backend_up() -> None:
    """Hard-fail early if the test backend is not reachable.

    The plan keeps backend lifecycle out of pytest itself — `pnpm test:api`
    is a thin wrapper that starts the stack and then runs pytest. Fail loud
    here rather than letting every test trip with a connection error.
    """
    try:
        r = httpx.get(f'{BACKEND_URL}/api/v1/health', timeout=5.0)
    except Exception as e:  # pragma: no cover — env-not-ready path
        pytest.exit(
            f'Backend not reachable at {BACKEND_URL}: {e}\n'
            'Start with: pnpm test:up && pnpm test:backend:start',
            returncode=2,
        )
    if r.status_code != 200:  # pragma: no cover — env-not-ready path
        pytest.exit(
            f'Backend health returned {r.status_code}: {r.text!r}',
            returncode=2,
        )


# ---- per-test DB reset -------------------------------------------------------


@pytest.fixture(autouse=True)
def db_reset() -> Iterator[None]:
    """TRUNCATE all data tables + reset the global_change_seq.

    Faster than DROP SCHEMA + alembic upgrade. Backend's connection pool keeps
    its connections — TRUNCATE doesn't invalidate them. We do this with a
    sync psycopg connection because pytest's collection / setup phase runs
    outside the asyncio loop owned by httpx fixtures.
    """
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'TRUNCATE TABLE refresh_tokens, users, providers, reactives, '
                'assistants, avatar_images, installed_plugins, messages, '
                'items, artifacts, dialogs, workspaces, blob_refs, blobs '
                'RESTART IDENTITY CASCADE'
            )
            cur.execute('ALTER SEQUENCE global_change_seq RESTART WITH 1')
    yield


# ---- direct DB inspection ----------------------------------------------------


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection]:
    """Sync psycopg connection for assert-against-DB cases.

    Async psycopg would force every test that wants to peek at a row into
    `async def`. Sync is fine because these are short read queries.
    """
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        yield conn


# ---- user / client helpers ---------------------------------------------------


def _new_email(prefix: str) -> str:
    # 8 hex chars is plenty when db_reset wipes between tests, but we still
    # randomize to keep parallel runs (xdist) from colliding if anyone enables
    # it later.
    return f'{prefix}-{secrets.token_hex(4)}@example.com'


@pytest_asyncio.fixture
async def anon_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=BACKEND_URL, timeout=10.0) as c:
        yield c


@pytest_asyncio.fixture
async def register_user(
    anon_client: httpx.AsyncClient,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory: register a fresh user, return dict with access/refresh + email/id.

    Caller can override email / password if a test needs deterministic creds.
    """

    async def _register(
        email: str | None = None, password: str = 'test-password-123'
    ) -> dict[str, Any]:
        email = email or _new_email('u')
        r = await anon_client.post(
            '/api/v1/auth/register',
            json={'email': email, 'password': password},
        )
        assert r.status_code == 201, f'register failed: {r.status_code} {r.text}'
        body = r.json()
        return {
            'id': body['user']['id'],
            'email': email,
            'password': password,
            'access_token': body['access_token'],
            'refresh_token': body['refresh_token'],
            'user': body['user'],
        }

    return _register


@pytest_asyncio.fixture
async def user_a(register_user) -> dict[str, Any]:
    return await register_user(_new_email('a'))


@pytest_asyncio.fixture
async def user_b(register_user) -> dict[str, Any]:
    return await register_user(_new_email('b'))


def _bearer_client(token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BACKEND_URL,
        timeout=10.0,
        headers={'Authorization': f'Bearer {token}'},
    )


@pytest_asyncio.fixture
async def client_a(user_a) -> AsyncIterator[httpx.AsyncClient]:
    async with _bearer_client(user_a['access_token']) as c:
        yield c


@pytest_asyncio.fixture
async def client_b(user_b) -> AsyncIterator[httpx.AsyncClient]:
    async with _bearer_client(user_b['access_token']) as c:
        yield c


# ---- websocket helper --------------------------------------------------------


@pytest_asyncio.fixture
async def ws_connect():
    """Async context manager factory for the realtime WS endpoint.

    Usage:
        async with ws_connect(token) as ws:
            await ws.send(json.dumps({'type': 'subscribe', 'table': 'providers'}))
            ...

    The bearer.<token> subprotocol is the only auth path the server accepts;
    we mirror what `src/data/realtime-ws.ts` does on the client side.
    """
    opened: list[ClientConnection] = []

    @asynccontextmanager
    async def _open(token: str, **kw):
        ws = await websockets.connect(
            WS_URL,
            subprotocols=[f'bearer.{token}'],
            **kw,
        )
        opened.append(ws)
        try:
            yield ws
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    yield _open

    # belt-and-suspenders: close anything the test forgot
    for ws in opened:
        try:
            await ws.close()
        except Exception:
            pass
