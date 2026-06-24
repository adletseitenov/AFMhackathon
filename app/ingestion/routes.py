"""Роут реального live-сканирования Telegram (авто-подключается main.py).

POST /api/scan/telegram  body: {"channels": ["@name", ...]}  ИЛИ  {"channel": "@name"}
-> реально тянет посты публичных каналов, скорит своей моделью, кладёт в ленту.
"""

from fastapi import APIRouter, Body, Request

from app.ingestion.scan import scan_telegram

router = APIRouter()


@router.post("/api/scan/telegram")
async def api_scan_telegram(request: Request, payload: dict | None = Body(default=None)):
    payload = payload or {}
    channels = payload.get("channels") or payload.get("channel") or []
    if isinstance(channels, str):
        channels = [channels]
    channels = [c for c in channels if c and str(c).strip()][:6]  # не больше 6 за раз
    if not channels:
        return {"added": 0, "flagged": 0, "channels": [], "error": "укажите канал(ы)"}
    return scan_telegram(channels, conn=request.app.state.db)
