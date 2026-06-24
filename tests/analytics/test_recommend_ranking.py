"""Тесты РАНЖИРОВАНИЯ глобальных рекомендаций и ДЕДУПА (Задача 1).

build_recommendations теперь добавляет каждой рекомендации числовой `score`
важности и сортирует по нему убыв. (high раньше low), дедуплицируя близкие
рекомендации по title/action. Форма {title,rationale,action,priority,evidence}
сохраняется (+ score).
"""

import json

import pytest

from app import config, db
from app.analytics.recommend import build_recommendations

_PRIORITIES = {"high", "medium", "low"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "rec_rank.db")
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


def _seed_post(conn, post_id, platform, posted_at="2026-06-20T10:00:00"):
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


def test_each_rec_has_numeric_score(conn):
    for i in range(6):
        pid = f"g{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    conn.commit()
    recs = build_recommendations(conn)
    assert recs
    for r in recs:
        assert "score" in r, "каждая рекомендация должна нести числовой score важности"
        assert isinstance(r["score"], (int, float))
        assert r["priority"] in _PRIORITIES


def test_recs_sorted_by_score_descending(conn):
    # high-бренд (важный) + стрим-правило (medium) + обзор -> должны ранжироваться.
    for i in range(6):
        pid = f"g{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 95, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    for i in range(3):
        pid = f"tw{i}"
        _seed_post(conn, pid, "twitch")
        _seed_score(conn, pid, 75, "gambling", "escalate")
    conn.commit()
    recs = build_recommendations(conn)
    scores = [r["score"] for r in recs]
    assert scores == sorted(scores, reverse=True), "рекомендации должны идти по score убыв."
    # high идёт раньше low (следствие ранжирования по важности).
    rank = {"high": 0, "medium": 1, "low": 2}
    ranks = [rank[r["priority"]] for r in recs]
    assert ranks == sorted(ranks), "high-приоритет не должен оказаться позади low"


def test_score_reflects_priority_and_evidence_scale(conn):
    # Доминирующий нелицензированный бренд в 8 постах должен иметь больший score,
    # чем обзорная low-рекомендация о преобладающей категории.
    for i in range(8):
        pid = f"g{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 95, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    conn.commit()
    recs = build_recommendations(conn)
    high = [r for r in recs if r["priority"] == "high"]
    low = [r for r in recs if r["priority"] == "low"]
    assert high, "ожидалась хотя бы одна high-рекомендация"
    if low:
        assert min(r["score"] for r in high) > max(r["score"] for r in low)


def test_dedup_near_duplicate_titles(conn):
    # Любые данные: в выдаче не должно быть двух рекомендаций с одинаковым title.
    for i in range(6):
        pid = f"g{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 95, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
            {"type": "promo_code", "value": "BONUS500", "normalized": "bonus500"},
        ])
    conn.commit()
    recs = build_recommendations(conn)
    titles = [r["title"].strip().lower() for r in recs]
    assert len(titles) == len(set(titles)), "не должно быть дублей по title"
    actions = [r["action"].strip().lower() for r in recs]
    assert len(actions) == len(set(actions)), "не должно быть дублей по action"


def test_empty_db_still_scored_and_safe(conn):
    recs = build_recommendations(conn)
    assert recs, "пустая БД -> хотя бы дефолтная рекомендация"
    for r in recs:
        assert "score" in r and isinstance(r["score"], (int, float))
