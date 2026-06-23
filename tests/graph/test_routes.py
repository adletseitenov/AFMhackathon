"""Тесты HTTP-роута GET /api/graph (F5, авто-роутер app/graph/routes.py).

БД изолируется через monkeypatch config.DB_PATH; данные пишутся в файл ДО
входа в контекст TestClient (lifespan откроет своё соединение к тому же файлу).
seed.load_seed идемпотентен (вернёт 0, т.к. posts уже не пуст), поэтому
demo-посты не подмешиваются.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)

    conn = db.connect()
    db.init_db(conn)

    def add(pid, risk, ents, revealed=1):
        conn.execute(
            "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
            "media_path, thumb_url, source, revealed) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, "telegram", "@" + pid, "https://t.me/x", "cap",
             "2026-06-24T10:00:00", None, None, "telegram", revealed),
        )
        conn.execute(
            "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
            "visual_concepts_json, combined_text, entities_json) VALUES(?,?,?,?,?,?,?)",
            (pid, "cap", "", "", "[]", "cap", json.dumps(ents)),
        )
        conn.execute(
            "INSERT INTO scores(post_id, risk, category, class_probs_json, "
            "top_features_json, recommended_action, scored_at) VALUES(?,?,?,?,?,?,?)",
            (pid, risk, "gambling", "{}", "[]", "review", "2026-06-24T10:00:00"),
        )
        conn.commit()

    add("p1", 80, [{"type": "telegram", "value": "@CasinoX", "normalized": "casinox"}])
    add("p2", 70, [{"type": "telegram", "value": "@casinox", "normalized": "casinox"}])
    add("p3", 10, [{"type": "casino_brand", "value": "Other", "normalized": "other"}])
    conn.close()

    with TestClient(app) as c:
        yield c


def test_graph_route_returns_200_with_nodes_edges_shape(client):
    resp = client.get("/api/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data and "edges" in data
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)
    assert data["nodes"], "ожидаются узлы для revealed-постов"


def test_graph_whole_filtered_by_min_risk(client):
    resp = client.get("/api/graph?min_risk=50")
    assert resp.status_code == 200
    data = resp.json()
    node_ids = {n["id"] for n in data["nodes"]}
    assert "post:p1" in node_ids
    assert "post:p2" in node_ids
    assert "post:p3" not in node_ids  # risk 10 < 50 отфильтрован
    assert isinstance(data["edges"], list)


def test_graph_ego_for_post(client):
    resp = client.get("/api/graph?post_id=p1")
    assert resp.status_code == 200
    node_ids = {n["id"] for n in resp.json()["nodes"]}
    assert "post:p1" in node_ids
    assert "post:p2" in node_ids       # co-post через общую сущность
    assert "post:p3" not in node_ids


def test_graph_default_no_filter_includes_all_revealed(client):
    resp = client.get("/api/graph")
    assert resp.status_code == 200
    node_ids = {n["id"] for n in resp.json()["nodes"]}
    # min_risk=0 по умолчанию -> все revealed-посты
    assert {"post:p1", "post:p2", "post:p3"}.issubset(node_ids)
