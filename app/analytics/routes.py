"""F7 — HTTP-роут аналитики/трендов (авто-роутер, A2/§0.6).

Модуль экспортирует module-level `router`; app/main.py его автоподключает —
main.py НЕ правится. Соединение берётся из request.app.state.db (одно соединение
из lifespan), а не открывается заново.
"""

from fastapi import APIRouter, Request

from app.analytics.trends import aggregate

router = APIRouter()


@router.get("/api/trends")
def get_trends(request: Request) -> dict:
    """Сводные агрегации трендов из БД приложения."""
    conn = request.app.state.db
    return aggregate(conn)
