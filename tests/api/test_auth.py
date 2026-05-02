"""Stage 1.5 — self-hosted JWT auth.

Maps the cloud-sync-migration plan's 4 Stage-1.5 verification scenarios:
  1. register two accounts, /me returns each user's own id
  2. account isolation — A's PUT not visible to B (covered also in
     test_providers; here we cross-check from the auth angle)
  3. expired access token → refresh → new token works
  4. logout revokes refresh — subsequent /refresh returns 401
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest


# Mirrors backend-start.sh's hard-coded test secret. We sign expired tokens
# locally so we don't have to wait 30 minutes in real time.
TEST_JWT_SECRET = 'test-only-secret-do-not-use-in-prod'


def _sign_access(user_id: str, *, exp_offset_seconds: int) -> str:
    now = int(time.time())
    payload = {
        'sub': user_id,
        'iat': now,
        'exp': now + exp_offset_seconds,
        'type': 'access',
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm='HS256')


# ---- 1. registration + /me identity ------------------------------------------


async def test_register_returns_distinct_user_ids(
    register_user, anon_client: httpx.AsyncClient,
) -> None:
    a = await register_user()
    b = await register_user()
    assert a['id'] != b['id']
    assert a['email'] != b['email']

    r_a = await anon_client.get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {a["access_token"]}'},
    )
    assert r_a.status_code == 200
    assert r_a.json()['id'] == a['id']

    r_b = await anon_client.get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {b["access_token"]}'},
    )
    assert r_b.status_code == 200
    assert r_b.json()['id'] == b['id']


async def test_register_duplicate_email_returns_409(
    register_user, anon_client: httpx.AsyncClient,
) -> None:
    a = await register_user()
    r = await anon_client.post(
        '/api/v1/auth/register',
        json={'email': a['email'], 'password': 'another-password-1'},
    )
    assert r.status_code == 409


async def test_password_min_length_enforced(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.post(
        '/api/v1/auth/register',
        json={'email': 'short@example.com', 'password': 'short'},
    )
    # pydantic min_length=8 → 422.
    assert r.status_code == 422


# ---- 2. login + wrong-password path ------------------------------------------


async def test_login_returns_token_pair(
    register_user, anon_client: httpx.AsyncClient,
) -> None:
    u = await register_user()
    r = await anon_client.post(
        '/api/v1/auth/login',
        json={'email': u['email'], 'password': u['password']},
    )
    assert r.status_code == 200
    body = r.json()
    assert body['access_token']
    assert body['refresh_token']
    assert body['user']['id'] == u['id']

    # Refresh tokens are random per issuance, so they must differ from the
    # registration ones. Access tokens carry only `sub` + integer `iat`/`exp`
    # — if login lands in the same second as register the HS256 output is
    # bit-identical, so we don't assert on it.
    assert body['refresh_token'] != u['refresh_token']


async def test_login_with_wrong_password_returns_401(
    register_user, anon_client: httpx.AsyncClient,
) -> None:
    u = await register_user()
    r = await anon_client.post(
        '/api/v1/auth/login',
        json={'email': u['email'], 'password': 'definitely-wrong'},
    )
    assert r.status_code == 401


async def test_login_unknown_email_returns_401(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.post(
        '/api/v1/auth/login',
        json={'email': 'nobody@example.com', 'password': 'whatever-1234'},
    )
    assert r.status_code == 401


# ---- 3. expired access → refresh round-trip ---------------------------------


async def test_expired_access_token_yields_401(
    register_user, anon_client: httpx.AsyncClient,
) -> None:
    u = await register_user()
    expired = _sign_access(u['id'], exp_offset_seconds=-60)
    r = await anon_client.get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {expired}'},
    )
    assert r.status_code == 401


async def test_refresh_issues_fresh_pair_and_revokes_old(
    register_user, anon_client: httpx.AsyncClient,
) -> None:
    u = await register_user()

    r = await anon_client.post(
        '/api/v1/auth/refresh', json={'refresh_token': u['refresh_token']}
    )
    assert r.status_code == 200
    body = r.json()
    new_access = body['access_token']
    new_refresh = body['refresh_token']
    # Refresh tokens are random opaque strings → guaranteed different.
    assert new_refresh != u['refresh_token']

    # New access works.
    r = await anon_client.get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {new_access}'},
    )
    assert r.status_code == 200

    # Old refresh now revoked — replay should 401.
    r = await anon_client.post(
        '/api/v1/auth/refresh', json={'refresh_token': u['refresh_token']}
    )
    assert r.status_code == 401


# ---- 4. logout revokes refresh ----------------------------------------------


async def test_logout_revokes_refresh_token(
    register_user, anon_client: httpx.AsyncClient,
) -> None:
    u = await register_user()
    r = await anon_client.post(
        '/api/v1/auth/logout', json={'refresh_token': u['refresh_token']}
    )
    assert r.status_code == 204

    r = await anon_client.post(
        '/api/v1/auth/refresh', json={'refresh_token': u['refresh_token']}
    )
    assert r.status_code == 401


async def test_logout_unknown_refresh_is_idempotent(
    anon_client: httpx.AsyncClient,
) -> None:
    # Logout with a never-issued token must not error — it's a "make sure
    # this is dead" call, not a presence check.
    r = await anon_client.post(
        '/api/v1/auth/logout',
        json={'refresh_token': 'totally-not-real'},
    )
    assert r.status_code == 204


# ---- 5. /me requires auth ---------------------------------------------------


async def test_me_without_token_returns_401(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.get('/api/v1/auth/me')
    assert r.status_code == 401


async def test_me_with_garbage_token_returns_401(
    anon_client: httpx.AsyncClient,
) -> None:
    r = await anon_client.get(
        '/api/v1/auth/me', headers={'Authorization': 'Bearer not-a-jwt'}
    )
    assert r.status_code == 401


# ---- 6. cross-account read isolation (auth angle) ----------------------------


async def test_a_token_cannot_read_b_data(
    user_a, user_b, anon_client: httpx.AsyncClient,
) -> None:
    # Each token's /me returns only its owner. We assert this from the auth
    # angle here; the providers-level isolation lives in test_providers.
    r_a = await anon_client.get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {user_a["access_token"]}'},
    )
    r_b = await anon_client.get(
        '/api/v1/auth/me',
        headers={'Authorization': f'Bearer {user_b["access_token"]}'},
    )
    assert r_a.json()['id'] == user_a['id']
    assert r_b.json()['id'] == user_b['id']
    assert r_a.json()['id'] != r_b.json()['id']
