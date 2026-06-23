"""F7 — тесты analytics/trends.aggregate: сводные агрегации по постам/скорам/сущностям.

Засев идёт напрямую в таблицы (без вызова модели) — это unit-тест агрегации.
БД изолируется через monkeypatch config.DB_PATH на временный файл (§0.2), что
покрывает и путь aggregate(conn=None) — он сам открывает соединение.
"""

import json

import pytest

from app import config, db
from app.analytics.trends import aggregate


# --- фикстуры/хелперы засева ---------------------------------------------------
@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Временная БД с инициализированной схемой; config.DB_PATH перенаправлен."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "trends_test.db")
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


def _seed_post(conn, post_id, platform, posted_at):
    conn.execute(
        "INSERT INTO posts (id, platform, author_handle, url, caption, "
        "posted_at, media_path, thumb_url, source, revealed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (post_id, platform, "@a", "http://x", "cap", posted_at, None, None, "seed"),
    )


def _seed_score(conn, post_id, risk, category, recommended_action):
    conn.execute(
        "INSERT INTO scores (post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) "
        "VALUES (?, ?, ?, '{}', '[]', ?, ?)",
        (post_id, risk, category, recommended_action, "2026-06-20T10:00:00"),
    )


def _seed_extracted(conn, post_id, entities):
    conn.execute(
        "INSERT INTO extracted (post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) "
        "VALUES (?, '', '', '', '[]', '', ?)",
        (post_id, json.dumps(entities, ensure_ascii=False)),
    )


# --- Задача 1: пустая БД -> занулённая, но полная структура --------------------
def test_aggregate_empty_db_returns_zeroed_structure(conn):
    result = aggregate(conn)
    assert set(result.keys()) == {
        "total_posts",
        "by_category",
        "by_platform",
        "by_recommended_action",
        "top_brands",
        "risk_histogram",
        "time_series",
    }
    assert result["total_posts"] == 0
    assert result["by_category"] == {
        "gambling": 0,
        "pyramid": 0,
        "fraud": 0,
        "clean": 0,
    }
    assert result["by_recommended_action"] == {
        "auto_clear": 0,
        "review": 0,
        "escalate": 0,
    }
    # корзины риска — 3 уровня (§0.11): auto_clear / review / escalate
    assert result["risk_histogram"] == {
        "0-39": 0,
        "40-69": 0,
        "70-100": 0,
    }
    assert result["by_platform"] == {}
    assert result["top_brands"] == []
    assert result["time_series"] == []


def test_aggregate_empty_db_no_conn_arg_opens_own(conn):
    # conn=None -> aggregate сам открывает соединение через config.DB_PATH
    result = aggregate()
    assert result["total_posts"] == 0
    assert result["by_platform"] == {}
    assert result["top_brands"] == []


# --- Задача 2: счётчики по категориям и рекомендованным действиям --------------
def test_aggregate_counts_by_category_and_action(conn):
    _seed_post(conn, "p1", "tiktok", "2026-06-20T10:00:00")
    _seed_post(conn, "p2", "tiktok", "2026-06-20T11:00:00")
    _seed_post(conn, "p3", "instagram", "2026-06-21T09:00:00")
    _seed_score(conn, "p1", 85, "gambling", "escalate")
    _seed_score(conn, "p2", 50, "fraud", "review")
    _seed_score(conn, "p3", 10, "clean", "auto_clear")
    conn.commit()

    result = aggregate(conn)
    assert result["total_posts"] == 3
    assert result["by_category"] == {
        "gambling": 1,
        "pyramid": 0,
        "fraud": 1,
        "clean": 1,
    }
    assert result["by_recommended_action"] == {
        "auto_clear": 1,
        "review": 1,
        "escalate": 1,
    }


# --- Задача 3: разрез по платформам и гистограмма риска (3 корзины) ------------
def test_aggregate_by_platform_and_risk_histogram(conn):
    _seed_post(conn, "p1", "tiktok", "2026-06-20T10:00:00")
    _seed_post(conn, "p2", "tiktok", "2026-06-20T11:00:00")
    _seed_post(conn, "p3", "instagram", "2026-06-21T09:00:00")
    _seed_post(conn, "p4", "telegram", "2026-06-21T10:00:00")
    _seed_post(conn, "p5", "youtube", "2026-06-21T11:00:00")
    _seed_score(conn, "p1", 5, "clean", "auto_clear")     # 0-39
    _seed_score(conn, "p2", 39, "clean", "auto_clear")     # 0-39 (граница)
    _seed_score(conn, "p3", 40, "fraud", "review")         # 40-69 (граница)
    _seed_score(conn, "p4", 69, "gambling", "review")      # 40-69 (граница)
    _seed_score(conn, "p5", 95, "gambling", "escalate")    # 70-100
    conn.commit()

    result = aggregate(conn)
    assert result["by_platform"] == {
        "tiktok": 2,
        "instagram": 1,
        "telegram": 1,
        "youtube": 1,
    }
    assert result["risk_histogram"] == {
        "0-39": 2,
        "40-69": 2,
        "70-100": 1,
    }


# --- Задача 4: топ брендов казино/букмекеров и временной ряд по дням -----------
def test_aggregate_top_brands_ordered_and_time_series(conn):
    _seed_post(conn, "p1", "tiktok", "2026-06-20T10:00:00")
    _seed_post(conn, "p2", "tiktok", "2026-06-20T22:00:00")
    _seed_post(conn, "p3", "instagram", "2026-06-21T09:00:00")
    _seed_score(conn, "p1", 85, "gambling", "escalate")
    _seed_score(conn, "p2", 80, "gambling", "escalate")
    _seed_score(conn, "p3", 75, "gambling", "escalate")
    # "1xbet" встречается в 3 постах, "mostbet" в 1 — порядок по частоте убыв.
    _seed_extracted(conn, "p1", [
        {"type": "betting_brand", "value": "1XBET", "normalized": "1xbet"},
        {"type": "casino_brand", "value": "Mostbet", "normalized": "mostbet"},
        {"type": "telegram", "value": "@x", "normalized": "x"},
    ])
    _seed_extracted(conn, "p2", [
        {"type": "betting_brand", "value": "1xBet", "normalized": "1xbet"},
    ])
    _seed_extracted(conn, "p3", [
        {"type": "betting_brand", "value": "1xbet", "normalized": "1xbet"},
    ])
    conn.commit()

    result = aggregate(conn)
    # только casino_brand/betting_brand, упорядочены по частоте убыв.; telegram не в счёт
    assert result["top_brands"] == [
        {"brand": "1xbet", "count": 3},
        {"brand": "mostbet", "count": 1},
    ]
    # временной ряд по дню posted_at, упорядочен по дате
    assert result["time_series"] == [
        {"day": "2026-06-20", "count": 2},
        {"day": "2026-06-21", "count": 1},
    ]


def test_aggregate_top_brands_ignores_malformed_entities_json(conn):
    # битый entities_json не должен ронять агрегацию (R4-подобная устойчивость)
    _seed_post(conn, "p1", "tiktok", "2026-06-20T10:00:00")
    conn.execute(
        "INSERT INTO extracted (post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) "
        "VALUES ('p1', '', '', '', '[]', '', ?)",
        ("not-json",),
    )
    conn.commit()

    result = aggregate(conn)
    assert result["top_brands"] == []
    assert result["total_posts"] == 1
