from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from .auth import decode_token
from .broadcaster import broadcaster

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query(...)):
    try:
        user_id = decode_token(token)
    except Exception:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    await broadcaster.register(user_id, ws)
    try:
        while True:
            # Keep-alive loop; ignore any client payloads.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.unregister(user_id, ws)
