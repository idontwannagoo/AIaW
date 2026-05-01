from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session

router = APIRouter(prefix='/api/v1', tags=['health'])


@router.get('/health')
async def health(session: AsyncSession = Depends(get_session)):
    db_ok = False
    error = None
    try:
        result = await session.execute(text('SELECT 1'))
        db_ok = result.scalar() == 1
    except Exception as e:
        error = str(e)
    return {
        'status': 'ok' if db_ok else 'degraded',
        'db': 'ok' if db_ok else 'error',
        'error': error,
    }
