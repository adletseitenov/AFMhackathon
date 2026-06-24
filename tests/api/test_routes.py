"""Тесты F4-роутов консоли КӨЗ (app/api/routes.py, авто-роутер).

БД изолируется через monkeypatch config.DB_PATH на временный файл; данные
пишутся ДО входа в контекст TestClient (lifespan откроет своё соединение к
тому же файлу). seed.load_seed идемпотентен (posts уже не пуст -> вернёт 0),
поэтому demo-посты не подмешиваются в фикстуру.

Покрываем (по заданию F4):
  - GET /api/feed: только revealed, сортировка по risk desc, форма
    {post, score, recommended_action}; фильтры min_risk/category;
  - GET /api/post/{id}: форма {post, extracted, score, explanation,
    recommended_action}; explanation — непустой список; 404 на неизвестный id;
  - POST /api/tick: увеличивает число revealed;
  - POST /api/analyze: с монки-патченным fetch_post возвращает scored-результат.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app
from app.models import Extracted, Post, Score, FeatureHit


def _add_post(conn, pid, risk, category, action, revealed,
              posted_at="2026-06-24T10:00:00", caption="cap", view_count=0):
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed, view_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, "tiktok", "@" + pid, "https://x/" + pid, caption, posted_at,
         None, None, "seed", revealed, view_count),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES(?,?,?,?,?,?,?)",
        (pid, risk, category, json.dumps({category: 0.9, "clean": 0.1}),
         json.dumps([{"feature": "payout_promise", "weight": 0.7,
                      "evidence": "30% в месяц"}]),
         action, posted_at),
    )
    conn.commit()


@pytest.fixture
def feed_client(tmp_path, monkeypatch):
    """Засевает 2 revealed (risk 85/30) + 1 нераскрытый (risk 95)."""
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    _add_post(conn, "p1", 85, "gambling", "escalate", 1)
    _add_post(conn, "p2", 30, "clean", "auto_clear", 1)
    _add_post(conn, "p3", 95, "pyramid", "escalate", 0)  # не раскрыт
    conn.close()
    with TestClient(app) as c:
        yield c


def test_feed_returns_only_revealed_sorted_by_risk_desc(feed_client):
    resp = feed_client.get("/api/feed")
    assert resp.status_code == 200
    data = resp.json()
    ids = [row["post"]["id"] for row in data]
    assert ids == ["p1", "p2"]  # p3 нераскрыт, p1 раньше p2 (85>30)
    assert data[0]["score"]["risk"] == 85
    # форма строки
    row = data[0]
    assert set(row.keys()) == {"post", "score", "recommended_action"}
    assert row["recommended_action"] == "escalate"
    assert row["post"]["platform"] == "tiktok"
    assert "category" in row["score"] and "class_probs" in row["score"]


def test_feed_min_risk_filter(feed_client):
    ids = [r["post"]["id"] for r in feed_client.get("/api/feed?min_risk=50").json()]
    assert ids == ["p1"]


def test_feed_category_filter(feed_client):
    ids = [r["post"]["id"] for r in feed_client.get("/api/feed?category=clean").json()]
    assert ids == ["p2"]


def test_feed_limit(feed_client):
    data = feed_client.get("/api/feed?limit=1").json()
    assert len(data) == 1
    assert data[0]["post"]["id"] == "p1"


def test_feed_post_dict_includes_view_count(feed_client):
    row = feed_client.get("/api/feed").json()[0]
    assert "view_count" in row["post"]


# --- SORTING (приоритетная очередь) -------------------------------------- #

@pytest.fixture
def sort_client(tmp_path, monkeypatch):
    """3 revealed-поста, у которых risk / posted_at / view_count дают РАЗНЫЙ порядок.

      a: risk=90, posted 10:00 (старейший), view_count=10
      b: risk=50, posted 12:00 (новейший), view_count=999
      c: risk=70, posted 11:00,            view_count=500
    relevance(risk)  -> a,c,b ; novelty(posted_at) -> b,c,a ; popularity(views) -> b,c,a.
    """
    dbfile = tmp_path / "sort.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    _add_post(conn, "a", 90, "gambling", "escalate", 1,
              posted_at="2026-06-24T10:00:00", view_count=10)
    _add_post(conn, "b", 50, "gambling", "review", 1,
              posted_at="2026-06-24T12:00:00", view_count=999)
    _add_post(conn, "c", 70, "gambling", "escalate", 1,
              posted_at="2026-06-24T11:00:00", view_count=500)
    conn.close()
    with TestClient(app) as c:
        yield c


def _ids(client, qs=""):
    return [r["post"]["id"] for r in client.get("/api/feed" + qs).json()]


def test_sort_relevance_orders_by_risk_desc(sort_client):
    assert _ids(sort_client, "?sort=relevance") == ["a", "c", "b"]


def test_sort_default_is_relevance(sort_client):
    assert _ids(sort_client) == ["a", "c", "b"]


def test_sort_novelty_orders_by_posted_at_desc(sort_client):
    assert _ids(sort_client, "?sort=novelty") == ["b", "c", "a"]


def test_sort_popularity_orders_by_view_count_desc(sort_client):
    assert _ids(sort_client, "?sort=popularity") == ["b", "c", "a"]


def test_sort_unknown_falls_back_to_relevance(sort_client):
    assert _ids(sort_client, "?sort=bogus") == ["a", "c", "b"]
    assert _ids(sort_client, "?sort=") == ["a", "c", "b"]


def test_sort_combines_with_filters(sort_client):
    # фильтр min_risk совместно с novelty: остаются a(90),c(70), порядок по дате desc
    assert _ids(sort_client, "?sort=novelty&min_risk=70") == ["c", "a"]
    # popularity + limit
    assert _ids(sort_client, "?sort=popularity&limit=1") == ["b"]


@pytest.fixture
def detail_client(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed) VALUES("
        "'p1','tiktok','@a','http://x','Гарантированный доход 30% в месяц',"
        "'2026-06-24T10:00:00',NULL,NULL,'seed',1)"
    )
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) VALUES("
        "'p1','Гарантированный доход','транскрипт казино','OCR казино',?,'combined',?)",
        (json.dumps([{"label": "casino", "score": 0.9}]),
         json.dumps([{"type": "casino_brand", "value": "1xBet", "normalized": "1xbet"}])),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES("
        "'p1',88,'pyramid',?,?,'escalate','2026-06-24T10:00:00')",
        (json.dumps({"pyramid": 0.88, "clean": 0.12}),
         json.dumps([{"feature": "payout_promise", "weight": 0.7,
                      "evidence": "30% в месяц"}])),
    )
    conn.commit()
    conn.close()
    with TestClient(app) as c:
        yield c


def test_post_detail_shape(detail_client):
    resp = detail_client.get("/api/post/p1")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {
        "post", "extracted", "score", "explanation", "recommended_action"}
    assert data["post"]["id"] == "p1"
    assert data["extracted"]["ocr_text"] == "OCR казино"
    assert data["extracted"]["visual_concepts"][0]["label"] == "casino"
    assert data["score"]["risk"] == 88
    assert isinstance(data["explanation"], list) and len(data["explanation"]) >= 1
    # explanation на русском — приходит из app.decision.explain
    assert any("доход" in line.lower() for line in data["explanation"])
    assert data["recommended_action"] == "escalate"


def test_post_detail_404(detail_client):
    assert detail_client.get("/api/post/nope").status_code == 404


@pytest.fixture
def tick_client(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    for i in range(10):
        conn.execute(
            "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
            "media_path, thumb_url, source, revealed) VALUES(?,?,?,?,?,?,?,?,?,0)",
            (f"p{i}", "tiktok", "@a", "http://x", "cap",
             f"2026-06-24T10:0{i}:00", None, None, "seed"),
        )
    conn.commit()
    conn.close()
    with TestClient(app) as c:
        yield c


def test_tick_increases_revealed_count(tick_client):
    r1 = tick_client.post("/api/tick")
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["revealed"] == config.TICK_REVEAL_N
    assert body1["total_revealed"] == config.TICK_REVEAL_N
    r2 = tick_client.post("/api/tick")
    body2 = r2.json()
    assert body2["total_revealed"] == 2 * config.TICK_REVEAL_N


@pytest.fixture
def analyze_client(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    conn.close()

    fake_post = Post(
        id="live1", platform="tiktok", author_handle="@scam", url="http://x",
        caption="100% доход", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="live",
    )
    fake_extracted = Extracted(
        post_id="live1", caption="100% доход", transcript="", ocr_text="",
        visual_concepts=[], combined_text="100% доход", entities=[],
    )
    fake_score = Score(
        post_id="live1", risk=91, category="pyramid",
        class_probs={"pyramid": 0.91, "clean": 0.09},
        top_features=[FeatureHit(feature="payout_promise", weight=0.8,
                                 evidence="100% доход")],
    )

    # Монки-патчим один seam (§0.5): module-атрибут fetch_post, extract, score_post.
    import app.ingestion.fetch as fetch_mod
    import app.extractors.pipeline as pipeline_mod
    import app.decision.scoring as scoring_mod

    monkeypatch.setattr(fetch_mod, "fetch_post",
                        lambda url=None, upload=None: fake_post)
    monkeypatch.setattr(pipeline_mod, "extract",
                        lambda post, use_cache=True, progress=None: fake_extracted)

    def _fake_score_post(post, extracted, conn=None):
        # имитируем персист реального score_post (upsert + audit)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        own = conn is None
        c = db.connect() if own else conn
        try:
            db.upsert_score(c, fake_score, "escalate", ts)
            db.add_audit(c, ts, post.id, "scored", "system", "test")
        finally:
            if own:
                c.close()
        return fake_score

    monkeypatch.setattr(scoring_mod, "score_post", _fake_score_post)

    with TestClient(app) as c:
        yield c


def _wait_job(client, job_id, timeout=10.0):
    """Опрашивает /api/jobs/{id} до done/error (анализ идёт в фоновом потоке)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.02)
    return client.get(f"/api/jobs/{job_id}").json()


def test_analyze_url_returns_scored_result(analyze_client):
    # /api/analyze теперь ставит фоновую задачу и возвращает job_id (async-контракт).
    resp = analyze_client.post("/api/analyze", json={"url": "http://x"})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    job = _wait_job(analyze_client, job_id)
    assert job["status"] == "done", job
    data = job["result"]
    assert data["post"]["id"] == "live1"
    assert data["score"]["risk"] == 91
    assert data["score"]["category"] == "pyramid"
    assert data["extracted"]["combined_text"] == "100% доход"
    assert data["recommended_action"] == "escalate"
    assert isinstance(data["explanation"], list)


def test_analyze_persists_and_reveals(analyze_client):
    """После анализа (задача завершилась) пост попадает в ленту (revealed=1 + score)."""
    resp = analyze_client.post("/api/analyze", json={"url": "http://x"})
    job = _wait_job(analyze_client, resp.json()["job_id"])
    assert job["status"] == "done", job
    feed = analyze_client.get("/api/feed").json()
    ids = [r["post"]["id"] for r in feed]
    assert "live1" in ids
