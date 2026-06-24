"""F7 — HTTP-роут аналитики/трендов (авто-роутер, A2/§0.6).

Модуль экспортирует module-level `router`; app/main.py его автоподключает —
main.py НЕ правится. Соединение берётся из request.app.state.db (одно соединение
из lifespan), а не открывается заново.
"""

from fastapi import APIRouter, Request

from app.analytics.recommend import (
    available_sources,
    build_hotspots,
    build_recommendations,
)
from app.analytics.trends import aggregate

router = APIRouter()


@router.get("/api/trends")
def get_trends(request: Request) -> dict:
    """Сводные агрегации трендов из БД приложения."""
    conn = request.app.state.db
    return aggregate(conn)


@router.get("/api/recommendations")
def get_recommendations(request: Request, focus: "str | None" = None) -> dict:
    """Превентивные рекомендации для АФМ (rule-based, без побочных эффектов).

    Необязательный query-параметр `focus` сужает рекомендации до одной проблемы/
    источника (например "brand:1xbet"/"category:gambling"/"platform:tiktok"); его
    значение передаётся в build_recommendations как есть — парсинг и валидация
    (включая мусор -> глобальный режим, без падения) выполняются в recommend.py.
    focus=None -> прежнее (глобальное) поведение.

    Форма: {generated_at_note, focus, stats:{total_posts, flagged, top_platform,
            top_brand}, recommendations:[{title, rationale, action, priority,
            evidence}]}. Соединение берётся из request.app.state.db (одно
    соединение из lifespan).
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
        "focus": focus,
        "stats": {
            "total_posts": agg["total_posts"],
            "flagged": flagged,
            "top_platform": top_platform,
            "top_brand": top_brand,
        },
        "recommendations": build_recommendations(conn, focus=focus),
    }


@router.get("/api/recommendations/sources")
def get_recommendation_sources(request: Request) -> dict:
    """Доступные «проблемы и источники» для фронтового селектора фокуса.

    Возвращает available_sources(conn): {categories, brands, platforms} — каждый
    список элементов {id, label, count, ...}; brands дополнительно несут флаг
    licensed: bool. Соединение берётся из request.app.state.db (то же соединение
    из lifespan — нового не открываем). Статический путь без path-параметров,
    поэтому конфликта маршрутизации с /api/recommendations нет.
    """
    conn = request.app.state.db
    return available_sources(conn)


@router.get("/api/hotspots")
def get_hotspots(request: Request) -> dict:
    """«Актуальные проблемы» — верхняя плашка дашборда АФМ.

    Возвращает build_hotspots(conn): самые опасные ПРОБЛЕМЫ (категории-угрозы),
    самые опасные КОНТОРЫ (бренды/операторы, с флагом licensed), самые опасные
    TELEGRAM- и YOUTUBE-каналы, плюс приоритезированные РЕШЕНИЯ (рекомендации).

    Форма (5 ключей, всегда присутствуют; списки могут быть пустыми):
      {top_problems:  [{category, label, count, avg_risk, escalate_count}],
       top_operators: [{brand, count, avg_risk, licensed: bool}],
       top_telegram:  [{channel, count, avg_risk}],
       top_youtube:   [{channel, count, avg_risk}],
       recommendations: [{title, rationale, action, priority, evidence, score}]}

    Соединение берётся из request.app.state.db (то же соединение из lifespan — нового
    не открываем). Статический путь без path-параметров, конфликта маршрутизации нет.
    Движок rule-based и никогда не падает (R4) -> роут не отдаёт 500.
    """
    conn = request.app.state.db
    return build_hotspots(conn)
