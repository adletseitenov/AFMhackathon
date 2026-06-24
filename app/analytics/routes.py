"""F7 — HTTP-роут аналитики/трендов (авто-роутер, A2/§0.6).

Модуль экспортирует module-level `router`; app/main.py его автоподключает —
main.py НЕ правится. Соединение берётся из request.app.state.db (одно соединение
из lifespan), а не открывается заново.
"""

from fastapi import APIRouter, Request

from app.analytics.recommend import build_recommendations
from app.analytics.trends import aggregate

router = APIRouter()


@router.get("/api/trends")
def get_trends(request: Request) -> dict:
    """Сводные агрегации трендов из БД приложения."""
    conn = request.app.state.db
    return aggregate(conn)


@router.get("/api/recommendations")
def get_recommendations(request: Request) -> dict:
    """Превентивные рекомендации для АФМ (rule-based, без побочных эффектов).

    Форма: {stats:{total_posts, flagged, top_platform, top_brand},
            recommendations:[{title, rationale, action, priority, evidence}]}.
    Соединение берётся из request.app.state.db (одно соединение из lifespan).
    """
    conn = request.app.state.db
    agg = aggregate(conn)
    by_category = agg["by_category"]
    by_platform = agg["by_platform"]
    top_brands = agg["top_brands"]

    flagged = sum(v for k, v in by_category.items() if k != "clean")
    top_platform = (
        max(by_platform.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if by_platform else None
    )
    top_brand = top_brands[0]["brand"] if top_brands else None

    return {
        "generated_at_note": (
            "Рекомендации сформированы детерминированными правилами поверх "
            "локальных агрегатов (без внешних API/LLM)."
        ),
        "stats": {
            "total_posts": agg["total_posts"],
            "flagged": flagged,
            "top_platform": top_platform,
            "top_brand": top_brand,
        },
        "recommendations": build_recommendations(conn),
    }
