"""REST поверх WATCHLIST (авто-подключается main.py через auto-router).

ENDPOINT CONTRACT (точный):

GET  /api/watchlist
  → {"channels": [...]}        # ТОЛЬКО telegram-каналы; "entries" НЕ включён (strict == backward compat)

POST /api/watchlist
  body: {"channel": "...", "platform"?: "..."} ИЛИ {"target": "...", "platform": "..."}
  • platform отсутствует / "telegram" / unknown → telegram-путь:
      → {"channels": [...]}    # только telegram (backward compat)
  • platform = tiktok|youtube|twitch|kick|instagram|operator →
      → {"channels": [...], "entries": [...]}

DELETE /api/watchlist/{channel}
  ?platform=telegram (default)  → {"channels": [...]}
  ?platform=<non-telegram>      → {"channels": [...], "entries": [...]}

POST /api/watchlist/scan
  → service.scan_watchlist(conn) → {added, flagged, channels, entries, ...}

GET  /api/watchlist/stats
  → {"channels": [...]}        # ТОЛЬКО legacy telegram stats (get_stats()); strict == сохранён

GET  /api/watchlist/entries    ← НОВЫЙ МАРШРУТ
  → {"entries": [...], "watched": [...]}
     entries = get_entry_stats()["entries"]
     watched = store.list_entries()

Ошибки никогда не всплывают как 500 — ловим и возвращаем структурный ответ.
Русские пользовательские строки.
"""

from fastapi import APIRouter, Body, Request

from app.watchlist import service, stats, store

router = APIRouter()

# Платформы, отличные от telegram; при добавлении → расширенный ответ с entries
_NON_TELEGRAM = {"youtube", "tiktok", "twitch", "kick", "instagram", "operator"}


# --------------------------------------------------------------------------- #
# GET /api/watchlist — backward compat (только channels, без entries)          #
# --------------------------------------------------------------------------- #

@router.get("/api/watchlist")
async def api_watchlist_list():
    """Список telegram-каналов watchlist. Возвращает только channels (строгое равенство)."""
    try:
        return {"channels": store.list_channels()}
    except Exception as exc:
        return {"channels": [], "error": f"не удалось прочитать список: {exc}"}


# --------------------------------------------------------------------------- #
# GET /api/watchlist/stats — legacy telegram stats (strict == не нарушен)     #
# --------------------------------------------------------------------------- #

@router.get("/api/watchlist/stats")
async def api_watchlist_stats():
    """Per-channel telegram-статистика последнего скана.

    Возвращает ТОЛЬКО get_stats() (legacy channels), чтобы
    test_stats_endpoint_shape прошёл с точным равенством {"channels": []}.
    """
    try:
        return stats.get_stats()
    except Exception as exc:
        return {"channels": [], "error": f"не удалось прочитать статистику: {exc}"}


# --------------------------------------------------------------------------- #
# GET /api/watchlist/entries — НОВЫЙ маршрут (мультиплатформенные данные)     #
# --------------------------------------------------------------------------- #

@router.get("/api/watchlist/entries")
async def api_watchlist_entries():
    """Мультиплатформенный дашборд: per-entry stats + текущий watchlist.

    Форма ответа:
      {
        "entries": [{target, platform, last_scan, last_added, last_flagged,
                     total_collected, licensed}, ...],
        "watched": [{target, platform}, ...]
      }
    """
    try:
        entry_stats = stats.get_entry_stats()["entries"]
    except Exception:
        entry_stats = []
    try:
        watched = store.list_entries()
    except Exception:
        watched = []
    return {"entries": entry_stats, "watched": watched}


# --------------------------------------------------------------------------- #
# POST /api/watchlist — добавление записи (telegram или non-telegram)          #
# --------------------------------------------------------------------------- #

@router.post("/api/watchlist")
async def api_watchlist_add(payload: dict | None = Body(default=None)):
    """Добавляет канал/запись в watchlist.

    Принимает {"channel": "...", "platform"?: "..."} или {"target": "...", "platform": "..."}.
    Telegram-путь (по умолчанию и для unknown платформ): {"channels": [...]}.
    Non-telegram: {"channels": [...], "entries": [...]}.
    """
    payload = payload or {}
    # Поддерживаем "channel" (legacy) и "target" (новый ключ)
    target = (payload.get("target") or payload.get("channel") or
              payload.get("name") or "")
    target = str(target).strip()
    platform = str(payload.get("platform") or "telegram").strip().lower()

    if not target:
        try:
            channels = store.list_channels()
        except Exception:
            channels = []
        return {"channels": channels, "error": "укажите канал"}

    if platform not in _NON_TELEGRAM:
        # Telegram-путь (включая unknown platform → telegram через store.add_channel)
        try:
            return {"channels": store.add_channel(target)}
        except Exception as exc:
            return {"channels": [], "error": f"не удалось добавить канал: {exc}"}
    else:
        # Non-telegram-путь
        try:
            new_entries = store.add_entry(target, platform)
            return {
                "channels": store.list_channels(),
                "entries": new_entries,
            }
        except Exception as exc:
            return {"channels": [], "entries": [],
                    "error": f"не удалось добавить запись: {exc}"}


# --------------------------------------------------------------------------- #
# DELETE /api/watchlist/{channel}                                              #
# --------------------------------------------------------------------------- #

@router.delete("/api/watchlist/{channel}")
async def api_watchlist_remove(channel: str, platform: str = "telegram"):
    """Удаляет запись из watchlist.

    ?platform=telegram (default) → удаляет telegram-запись, {"channels": [...]}.
    ?platform=<non-telegram>     → удаляет мультиплатформенную запись,
                                   {"channels": [...], "entries": [...]}.
    """
    plat = (platform or "telegram").strip().lower()
    try:
        if plat not in _NON_TELEGRAM:
            return {"channels": store.remove_channel(channel)}
        else:
            remaining_entries = store.remove_entry(channel, plat)
            return {
                "channels": store.list_channels(),
                "entries": remaining_entries,
            }
    except Exception as exc:
        return {"channels": [], "error": f"не удалось удалить канал: {exc}"}


# --------------------------------------------------------------------------- #
# POST /api/watchlist/scan                                                     #
# --------------------------------------------------------------------------- #

@router.post("/api/watchlist/scan")
async def api_watchlist_scan(request: Request):
    """Запускает полный скан watchlist по всем платформам.

    Возвращает {added, flagged, channels (telegram per-channel rows), entries, ...}.
    Никогда не 500.
    """
    try:
        return service.scan_watchlist(conn=request.app.state.db)
    except Exception as exc:
        return {"added": 0, "flagged": 0, "channels": [], "entries": [],
                "error": f"скан не выполнен: {exc}"}
