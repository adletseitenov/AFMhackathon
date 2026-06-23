"""F7 — сводные агрегации (тренды) по постам, скорам и сущностям.

`aggregate(conn=None)` строит единый словарь трендов из таблиц posts/scores/extracted:
счётчики по категориям, платформам, рекомендованным действиям; топ брендов
казино/букмекеров (из extracted.entities_json по типам casino_brand/betting_brand);
гистограмма риска по 3 уровням (§0.11: 0-39 / 40-69 / 70-100); временной ряд по дням.

Пустая БД -> полностью занулённая структура (никаких KeyError, без падения).

Standalone-функция (§0.2): если conn не передан — открывает своё соединение через
db.connect() и закрывает его в finally (sqlite3 `with` не закрывает соединение).
"""

import json
import sqlite3
from collections import Counter

from app import config, db

# Канонические перечни (порядок ключей — детерминирован для фронта/тестов).
CATEGORIES = list(config.CATEGORIES)  # ["gambling", "pyramid", "fraud", "clean"]
# 3-уровневый маппинг действий (§0.11) — без полосы "monitor".
RECOMMENDED_ACTIONS = ["auto_clear", "review", "escalate"]
# Корзины риска зеркалят пороги config.REVIEW_THRESHOLD(40)/ESCALATE_THRESHOLD(70).
RISK_BUCKETS = ["0-39", "40-69", "70-100"]
# Типы сущностей, считающихся "брендом" для топа.
BRAND_ENTITY_TYPES = {"casino_brand", "betting_brand"}
TOP_BRANDS_LIMIT = 10


def _empty_result() -> dict:
    return {
        "total_posts": 0,
        "by_category": {c: 0 for c in CATEGORIES},
        "by_platform": {},
        "by_recommended_action": {a: 0 for a in RECOMMENDED_ACTIONS},
        "top_brands": [],
        "risk_histogram": {b: 0 for b in RISK_BUCKETS},
        "time_series": [],
    }


def aggregate(conn: "sqlite3.Connection | None" = None) -> dict:
    """Построить сводный словарь трендов из таблиц posts/scores/extracted.

    conn=None -> открыть своё соединение (config.DB_PATH) и закрыть в finally.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    try:
        return _aggregate(conn)
    finally:
        if own_conn:
            conn.close()


def _aggregate(conn: sqlite3.Connection) -> dict:
    result = _empty_result()

    # --- всего постов ---
    row = conn.execute("SELECT COUNT(*) AS n FROM posts").fetchone()
    result["total_posts"] = row["n"]

    # --- по категориям (только известные классы) ---
    for r in conn.execute(
        "SELECT category, COUNT(*) AS n FROM scores GROUP BY category"
    ):
        if r["category"] in result["by_category"]:
            result["by_category"][r["category"]] = r["n"]

    # --- по рекомендованным действиям (только известные действия) ---
    for r in conn.execute(
        "SELECT recommended_action, COUNT(*) AS n "
        "FROM scores GROUP BY recommended_action"
    ):
        if r["recommended_action"] in result["by_recommended_action"]:
            result["by_recommended_action"][r["recommended_action"]] = r["n"]

    # --- по платформам ---
    for r in conn.execute(
        "SELECT platform, COUNT(*) AS n FROM posts GROUP BY platform"
    ):
        result["by_platform"][r["platform"]] = r["n"]

    # --- гистограмма риска по 3 уровням (пороги 40/70) ---
    for r in conn.execute(
        """
        SELECT
            CASE
                WHEN risk < 40 THEN '0-39'
                WHEN risk < 70 THEN '40-69'
                ELSE '70-100'
            END AS bucket,
            COUNT(*) AS n
        FROM scores
        GROUP BY bucket
        """
    ):
        if r["bucket"] in result["risk_histogram"]:
            result["risk_histogram"][r["bucket"]] = r["n"]

    # --- топ брендов казино/букмекеров (парсинг JSON в Python) ---
    brand_counter: Counter = Counter()
    for r in conn.execute("SELECT entities_json FROM extracted"):
        try:
            entities = json.loads(r["entities_json"]) or []
        except (TypeError, ValueError):
            entities = []
        if not isinstance(entities, list):
            continue
        for e in entities:
            if not isinstance(e, dict):
                continue
            if e.get("type") in BRAND_ENTITY_TYPES:
                key = e.get("normalized") or e.get("value")
                if key:
                    brand_counter[key] += 1
    # упорядочить по частоте убыв.; при равенстве — по имени (детерминизм).
    result["top_brands"] = [
        {"brand": brand, "count": count}
        for brand, count in sorted(
            brand_counter.items(), key=lambda kv: (-kv[1], kv[0])
        )[:TOP_BRANDS_LIMIT]
    ]

    # --- временной ряд по дню posted_at ---
    for r in conn.execute(
        "SELECT substr(posted_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM posts WHERE posted_at IS NOT NULL AND posted_at != '' "
        "GROUP BY day ORDER BY day"
    ):
        result["time_series"].append({"day": r["day"], "count": r["n"]})

    return result
