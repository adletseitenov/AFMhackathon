"""REST поверх WATCHLIST (авто-подключается main.py через auto-router).

GET    /api/watchlist            -> {"channels":[...]}
POST   /api/watchlist            body {"channel": "..."}  -> {"channels":[...]}
DELETE /api/watchlist/{channel}  -> {"channels":[...]}
POST   /api/watchlist/scan       -> scan_watchlist(app.state.db) (added/flagged/channels)

Ошибки никогда не всплывают как 500 — ловим и возвращаем структурный ответ.
Русские пользовательские строки.
"""

from fastapi import APIRouter, Body, Request

from app.watchlist import service, store

router = APIRouter()


@router.get("/api/watchlist")
async def api_watchlist_list():
    try:
        return {"channels": store.list_channels()}
    except Exception as exc:
        return {"channels": [], "error": f"не удалось прочитать список: {exc}"}


@router.post("/api/watchlist")
async def api_watchlist_add(payload: dict | None = Body(default=None)):
    payload = payload or {}
    channel = payload.get("channel") or payload.get("name") or ""
    if not str(channel).strip():
        return {"channels": store.list_channels(), "error": "укажите канал"}
    try:
        return {"channels": store.add_channel(str(channel))}
    except Exception as exc:
        return {"channels": [], "error": f"не удалось добавить канал: {exc}"}


@router.delete("/api/watchlist/{channel}")
async def api_watchlist_remove(channel: str):
    try:
        return {"channels": store.remove_channel(channel)}
    except Exception as exc:
        return {"channels": [], "error": f"не удалось удалить канал: {exc}"}


@router.post("/api/watchlist/scan")
async def api_watchlist_scan(request: Request):
    try:
        return service.scan_watchlist(conn=request.app.state.db)
    except Exception as exc:
        return {"added": 0, "flagged": 0, "channels": [], "error": f"скан не выполнен: {exc}"}
