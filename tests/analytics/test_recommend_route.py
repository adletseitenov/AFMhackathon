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
        _add(conn, f"p{i}", "tiktok", 92, "gambling", "escalate", brand="Mostbet")
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
    # доминирующий бренд mostbet -> есть high-рекомендация, упоминающая бренд
    assert any(
        r["priority"] == "high"
        and "mostbet" in (r["title"] + r["rationale"] + r["action"]).lower()
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


def test_recommendations_focus_param(client):
    resp = client.get("/api/recommendations", params={"focus": "brand:mostbet"})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) >= {"stats", "recommendations"}
    recs = data["recommendations"]
    assert isinstance(recs, list) and recs, "фокус по бренду -> непустые рекомендации"
    top = recs[0]
    assert "mostbet" in (top["title"] + top["rationale"] + top["action"]).lower(), (
        "топ-рекомендация при focus=brand:mostbet должна упоминать mostbet"
    )


def test_recommendations_focus_category(client):
    resp = client.get("/api/recommendations", params={"focus": "category:gambling"})
    assert resp.status_code == 200
    data = resp.json()
    recs = data["recommendations"]
    assert isinstance(recs, list) and recs, "фокус по категории -> непустые рекомендации"
    blob = " ".join(
        (r["title"] + r["rationale"] + r["action"]) for r in recs
    ).lower()
    # gambling-ориентированность: упоминание гемблинга/казино/букмекера/бренда mostbet.
    assert any(
        kw in blob for kw in ("гемблинг", "казино", "букмекер", "mostbet", "ставк")
    ), "рекомендации при focus=category:gambling должны быть про гемблинг"


def test_recommendations_focus_garbage_falls_back(client):
    resp = client.get("/api/recommendations", params={"focus": "garbage"})
    assert resp.status_code == 200, "мусорный focus не должен ронять роут (нет 500)"
    data = resp.json()
    assert data["recommendations"], "мусорный focus -> глобальный фолбэк, непустой список"


def test_recommendations_sources_shape(client):
    resp = client.get("/api/recommendations/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert {"categories", "brands", "platforms"}.issubset(data)
    for key in ("categories", "brands", "platforms"):
        assert isinstance(data[key], list), f"{key} должно быть списком"
    brands = data["brands"]
    assert brands, "на засеянной фикстуре brands непуст (доминирует Mostbet)"
    for b in brands:
        assert {"id", "label", "count"}.issubset(b)
        assert isinstance(b["licensed"], bool), "каждый brand несёт булев licensed"
    platforms = data["platforms"]
    platform_ids = {p.get("id") for p in platforms} | {
        p.get("label") for p in platforms
    }
    assert "tiktok" in platform_ids, "tiktok среди платформ засеянной фикстуры"


def test_recommendations_sources_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "rec_sources_empty.db")
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    with TestClient(app) as c:
        resp = c.get("/api/recommendations/sources")
    assert resp.status_code == 200, "пустая БД -> /sources не падает"
    data = resp.json()
    assert {"categories", "brands", "platforms"}.issubset(data)
    for key in ("categories", "brands", "platforms"):
        assert isinstance(data[key], list), f"{key} список (может быть пустым)"


_HOTSPOTS_KEYS = {
    "top_problems", "top_operators", "top_telegram", "top_youtube",
    "recommendations",
}


def test_hotspots_shape(client):
    resp = client.get("/api/hotspots")
    assert resp.status_code == 200
    data = resp.json()
    assert _HOTSPOTS_KEYS.issubset(data), "все 5 ключей плашки присутствуют"
    for key in ("top_problems", "top_operators", "top_telegram", "top_youtube"):
        assert isinstance(data[key], list), f"{key} должно быть списком"
    assert isinstance(data["recommendations"], list), "recommendations — список"
    # на засеянных gambling/Mostbet данных топ-проблемы непусты (есть gambling).
    problems = data["top_problems"]
    assert problems, "top_problems непуст (gambling в засеянной фикстуре)"
    assert any(
        (p.get("category") or "").lower() == "gambling" for p in problems
    ), "gambling среди топ-проблем"
    # топ-конторы непусты и содержат нелицензированный mostbet.
    operators = data["top_operators"]
    assert operators, "top_operators непуст (доминирует Mostbet)"
    assert any(
        "mostbet" in (op.get("brand") or "").lower() and op.get("licensed") is False
        for op in operators
    ), "mostbet среди контор с licensed=False"
    assert data["recommendations"], "решения (recommendations) непусты"


def test_hotspots_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hotspots_empty.db")
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    with TestClient(app) as c:
        resp = c.get("/api/hotspots")
    assert resp.status_code == 200, "пустая БД -> /hotspots не падает (нет 500)"
    data = resp.json()
    assert _HOTSPOTS_KEYS.issubset(data), "все 5 ключей присутствуют и на пустой БД"
    for key in ("top_problems", "top_operators", "top_telegram", "top_youtube"):
        assert isinstance(data[key], list), f"{key} список (пустой на пустой БД)"
    assert isinstance(data["recommendations"], list), (
        "recommendations — список (может быть непустым: дефолтная рекомендация)"
    )
