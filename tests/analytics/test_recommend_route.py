"""Тест HTTP-роута GET /api/recommendations (авто-роутер app/analytics/routes.py).

Поднимаем реальное app через TestClient на временной БД (как в tests/api):
данные пишутся ДО входа в контекст (lifespan откроет своё соединение к тому же
файлу; seed.load_seed идемпотентен -> demo-посты не подмешиваются).
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app


def _add(conn, pid, platform, risk, category, action, brand=None):
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed, view_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,1,0)",
        (pid, platform, "@" + pid, "https://x/" + pid, "cap",
         "2026-06-24T10:00:00", None, None, "seed"),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES(?,?,?,?,?,?,?)",
        (pid, risk, category, "{}", "[]", action, "2026-06-24T10:00:00"),
    )
    if brand is not None:
        conn.execute(
            "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
            "visual_concepts_json, combined_text, entities_json) "
            "VALUES(?, '', '', '', '[]', '', ?)",
            (pid, json.dumps(
                [{"type": "betting_brand", "value": brand,
                  "normalized": brand.lower()}], ensure_ascii=False)),
        )
    conn.commit()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "rec_route.db")
    conn = db.connect()
    db.init_db(conn)
    for i in range(6):
        _add(conn, f"p{i}", "tiktok", 92, "gambling", "escalate", brand="1xBet")
    conn.close()
    with TestClient(app) as c:
        yield c


def test_recommendations_endpoint_shape(client):
    resp = client.get("/api/recommendations")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) >= {"stats", "recommendations"}
    stats = data["stats"]
    assert set(stats.keys()) >= {"total_posts", "flagged", "top_platform", "top_brand"}
    assert stats["total_posts"] == 6
    recs = data["recommendations"]
    assert isinstance(recs, list) and recs
    for r in recs:
        assert {"title", "rationale", "action", "priority", "evidence"}.issubset(r)
        assert r["priority"] in {"high", "medium", "low"}
    # доминирующий бренд 1xbet -> есть high-рекомендация, упоминающая бренд
    assert any(
        r["priority"] == "high"
        and "1xbet" in (r["title"] + r["rationale"] + r["action"]).lower()
        for r in recs
    )


def test_recommendations_endpoint_empty_db_no_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "rec_empty.db")
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    with TestClient(app) as c:
        resp = c.get("/api/recommendations")
    assert resp.status_code == 200
    data = resp.json()
    assert data["recommendations"], "пустая БД -> хотя бы дефолтная рекомендация"
