from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, Form, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import aiohttp
import logging
from typing import Optional, Dict, Any
from fastapi.staticfiles import StaticFiles
from llama_parse import LlamaParse
import os

logger = logging.getLogger('aiaw.backend')
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

http_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = aiohttp.ClientSession()
    yield
    await http_client.close()

app = FastAPI(lifespan=lifespan)

_cors_origins = [o.strip() for o in os.environ.get(
    'CORS_ALLOW_ORIGINS',
    'http://localhost:9005,http://localhost:9006,http://127.0.0.1:9005,http://127.0.0.1:9006'
).split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*']
)

def _enable_backend_data_api(app: FastAPI) -> None:
    """Conditionally mount the self-hosted data API (auth + providers + health).

    Gated by BACKEND_DATA_API_ENABLED so the container can boot in deployments
    that only need the original CORS proxy / doc-parse / static SPA. When the
    flag is on we fail fast at startup if the required secrets are missing,
    so misconfiguration surfaces in the boot log rather than as runtime 5xx.
    Symmetric with the frontend's BACKEND_DATA_API_URL flag — both off = Stage
    0 behavior; both on = Stage 1.5 full data API.
    """
    flag = os.environ.get('BACKEND_DATA_API_ENABLED', '').strip().lower()
    if flag != 'true':
        logger.info(
            'backend data API disabled (set BACKEND_DATA_API_ENABLED=true to enable)'
        )
        return

    missing = [v for v in ('JWT_SECRET', 'DATABASE_URL') if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            'BACKEND_DATA_API_ENABLED=true but required env vars missing: '
            + ', '.join(missing)
            + '. Generate JWT_SECRET via: '
            'python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )

    # Imported lazily so module-level side effects (engine creation, JWT_SECRET
    # check inside data/auth.py) only happen when the feature is on.
    from data.routers import auth as auth_router
    from data.routers import health as health_router
    from data.routers import providers as providers_router
    from data.routers import stream as stream_router

    app.include_router(health_router.router)
    app.include_router(auth_router.router)
    app.include_router(providers_router.router)
    app.include_router(stream_router.router)
    logger.info(
        'backend data API enabled (auth + providers + health + stream mounted)'
    )


_enable_backend_data_api(app)

ALLOWED_PREFIXES = [
    'https://lobehub.search1api.com/api/search',
    'https://pollinations.ai-chat.top/api/drawing',
    'https://web-crawler.chat-plugin.lobehub.com/api/v1'
]

class ProxyRequest(BaseModel):
    method: str
    url: str
    headers: Optional[Dict[str, str]] = None
    body: Optional[Any] = None

@app.post('/cors/proxy')
async def proxy(request: ProxyRequest):
    if not any(request.url.startswith(prefix) for prefix in ALLOWED_PREFIXES):
        raise HTTPException(status_code=403, detail='URL not allowed')

    kwargs = {
        'method': request.method,
        'url': request.url,
        'headers': request.headers or {}
    }

    if request.body is not None:
        if isinstance(request.body, (dict, list)):
            kwargs['json'] = request.body
        else:
            kwargs['data'] = request.body

    try:
        async with http_client.request(**kwargs) as response:
            content = await response.read()
            return Response(content=content, status_code=response.status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post('/doc-parse/parse')
async def parse_document(
    file: UploadFile = File(...),
    language: Optional[str] = Form(default='en'),
    target_pages: Optional[str] = Form(default=None)
):
    parser = LlamaParse(
        result_type='markdown',
        language=language,
        target_pages=target_pages
    )

    file_content = await file.read()

    try:
        documents = await parser.aload_data(
            file_content,
            {'file_name': file.filename}
        )

        return {
            'success': True,
            'content': [{'text': doc.text, 'meta': doc.metadata} for doc in documents]
        }

    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

    finally:
        await file.close()

@app.get('/searxng')
async def searxng(request: Request):
    searxng_url = os.environ.get('SEARXNG_URL')

    if not searxng_url:
        raise HTTPException(status_code=502, detail="SEARXNG_URL environment variable not set")

    query_string = request.url.query
    target_url = f"{searxng_url}?{query_string}" if query_string else searxng_url

    headers = dict(request.headers)
    # 移除 host header 以避免冲突
    headers.pop('host', None)

    try:
        async with http_client.get(target_url, headers=headers) as response:
            content = await response.read()
            return Response(
                content=content,
                status_code=response.status
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if os.path.isdir('static'):
    app.mount('/', StaticFiles(directory='static', html=True), name='static')

    @app.exception_handler(404)
    async def return_index(request: Request, exc: HTTPException):
        return FileResponse("static/index.html")
