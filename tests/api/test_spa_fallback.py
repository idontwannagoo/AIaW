"""Regression net for the prod-only SPA-vs-API 404 collision.

Background — bug discovered via prod e2e smoke (P9 cross-account isolation):
``src-backend/app.py`` registered a global ``@app.exception_handler(404)``
that returned ``FileResponse("static/index.html")`` for every 404. The
intent was SPA client-side routing — Vue Router needs a fallback so deep
links still serve the bundle. The collateral damage was that *router-raised*
404s on ``/api/*`` paths (e.g. ``workspaces.py::get_workspace`` when a row
doesn't exist or doesn't belong to the caller) were swallowed too. Clients
got 200 HTML instead of 404 JSON; ``await response.json()`` threw
``Unexpected token <`` and the original "row not found" signal was lost.

The 9011 test backend never exposed this because backend-start.sh runs
uvicorn from ``src-backend/`` cwd and there's no ``static/`` directory
there, so ``if os.path.isdir('static')`` short-circuits and the handler
isn't even registered. The Docker image *is* built with ``static/`` mounted,
which is why only prod-side smoke caught it.

This file uses an in-process FastAPI ``TestClient`` against a minimal app
that mirrors the prod mount + handler shape, so the spec has no dependency
on the live backend or database. The shape is duplicated (not imported from
app.py) to avoid bringing up the full backend lifespan + db machinery just
to test 5 lines of routing logic; if you change the prod handler, change
this fixture in lockstep. ``tests/scripts/`` style prod_smoke (P9) is the
secondary regression net at deploy time.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient


@pytest.fixture
def spa_fallback_app(tmp_path):
    static_dir = tmp_path / 'static'
    static_dir.mkdir()
    (static_dir / 'index.html').write_text(
        '<!DOCTYPE html><html><body>SPA</body></html>'
    )

    app = FastAPI()

    @app.get('/api/v1/probe/{x}')
    def probe_handler(x: str):
        # Stand-in for any prod router that raises 404 on missing/foreign rows.
        raise HTTPException(status_code=404, detail=f'no such {x}')

    app.mount('/', StaticFiles(directory=str(static_dir), html=True), name='static')

    # Mirror src-backend/app.py post-fix: API paths get JSON, everything else
    # falls through to the SPA shell.
    @app.exception_handler(404)
    async def return_index(request: Request, exc: HTTPException):
        if request.url.path.startswith('/api/'):
            return JSONResponse(
                status_code=404,
                content={'detail': getattr(exc, 'detail', 'not found')},
            )
        return FileResponse(str(static_dir / 'index.html'))

    return app


def test_unknown_api_path_returns_json(spa_fallback_app):
    """FastAPI's default 404 (no route match) on /api/* must surface as JSON."""
    with TestClient(spa_fallback_app) as client:
        r = client.get('/api/v1/this-does-not-exist')
    assert r.status_code == 404, (r.status_code, r.text[:200])
    ct = r.headers.get('content-type', '')
    assert ct.startswith('application/json'), ct
    assert 'detail' in r.json()


def test_router_raised_api_404_returns_json(spa_fallback_app):
    """The actual prod scenario: a router explicitly raises HTTPException(404).
    Pre-fix the global handler returned SPA HTML and the JSON detail was lost."""
    with TestClient(spa_fallback_app) as client:
        r = client.get('/api/v1/probe/abc')
    assert r.status_code == 404, (r.status_code, r.text[:200])
    ct = r.headers.get('content-type', '')
    assert ct.startswith('application/json'), ct
    assert r.json().get('detail') == 'no such abc'


def test_non_api_404_serves_spa(spa_fallback_app):
    """Non-API paths still route to index.html — Vue Router takes over from
    here. This is the SPA fallback's *intended* behavior."""
    with TestClient(spa_fallback_app) as client:
        r = client.get('/some/spa/path')
    assert r.status_code == 200, r.status_code
    ct = r.headers.get('content-type', '')
    assert ct.startswith('text/html'), ct
    assert 'SPA' in r.text
