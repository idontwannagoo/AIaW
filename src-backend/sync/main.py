from fastapi import FastAPI

from .config import SYNC_ENABLED
from .routes_api import router as api_router
from .routes_auth import router as auth_router
from .routes_files import router as files_router
from .routes_ws import router as ws_router
from .s3 import ensure_bucket


def mount_sync(app: FastAPI) -> None:
    if not SYNC_ENABLED:
        return
    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(files_router)
    app.include_router(ws_router)

    @app.on_event("startup")
    async def _startup() -> None:
        try:
            ensure_bucket()
        except Exception as exc:  # pragma: no cover
            print(f"[sync] ensure_bucket failed: {exc}")
