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


# --- Флаг «разрешён в РК» (лицензированный букмекер) --------------------- #


# --- FEED FILTERS: risk-band (action / max_risk), date (since / sort=newest) --- #


@pytest.fixture
def band_client(tmp_path, monkeypatch):
    """По одному revealed-посту в каждой полосе риска.

      esc:   risk=85, action=escalate
      rev:   risk=55, action=review
      clr:   risk=20, action=auto_clear
    """
    dbfile = tmp_path / "band.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    _add_post(conn, "esc", 85, "gambling", "escalate", 1)
    _add_post(conn, "rev", 55, "gambling", "review", 1)
    _add_post(conn, "clr", 20, "clean", "auto_clear", 1)
    conn.close()
    with TestClient(app) as c:
        yield c


def test_feed_action_escalate_only(band_client):
    assert _ids(band_client, "?action=escalate") == ["esc"]


def test_feed_action_review_only(band_client):
    assert _ids(band_client, "?action=review") == ["rev"]


def test_feed_action_clean_maps_to_auto_clear(band_client):
    # публичное значение "clean" -> хранимое "auto_clear"
    assert _ids(band_client, "?action=clean") == ["clr"]


def test_feed_action_auto_clear_alias(band_client):
    # хранимое имя тоже принимается как полоса «очищено»
    assert _ids(band_client, "?action=auto_clear") == ["clr"]


def test_feed_action_unknown_ignored(band_client):
    # неизвестное/пустое action -> без фильтра (текущее поведение)
    assert _ids(band_client, "?action=bogus") == ["esc", "rev", "clr"]
    assert _ids(band_client, "?action=") == ["esc", "rev", "clr"]


def test_feed_max_risk_excludes_above(band_client):
    # max_risk=60 отсекает risk>60 (esc=85), остаются rev(55), clr(20)
    assert _ids(band_client, "?max_risk=60") == ["rev", "clr"]


def test_feed_risk_band_min_and_max(band_client):
    # полоса [40,70]: остаётся только rev(55)
    assert _ids(band_client, "?min_risk=40&max_risk=70") == ["rev"]


def test_feed_max_risk_default_no_upper_bound(band_client):
    # без max_risk верхней границы нет — все три (порядок по risk desc)
    assert _ids(band_client, "") == ["esc", "rev", "clr"]


def test_sort_newest_is_alias_for_novelty(sort_client):
    # newest == novelty (posted_at desc)
    assert _ids(sort_client, "?sort=newest") == ["b", "c", "a"]


def test_feed_since_excludes_older(sort_client):
    # since=11:00 отсекает a(10:00); остаются b(12:00), c(11:00) по risk desc -> c(70),b(50)
    ids = _ids(sort_client, "?since=2026-06-24T11:00:00")
    assert "a" not in ids
    assert set(ids) == {"b", "c"}


def test_feed_since_empty_is_no_filter(sort_client):
    assert _ids(sort_client, "?since=") == ["a", "c", "b"]


# --- FEED FILTER: licensed (licensed / unlicensed / all) ------------------- #


def test_feed_licensed_filter_keeps_only_licensed(licensed_feed_client):
    assert _ids(licensed_feed_client, "?licensed=licensed") == ["lic"]


def test_feed_licensed_filter_keeps_only_unlicensed(licensed_feed_client):
    assert _ids(licensed_feed_client, "?licensed=unlicensed") == ["unlic"]


def test_feed_licensed_filter_all_keeps_both(licensed_feed_client):
    # "all" и отсутствие параметра -> оба поста
    assert set(_ids(licensed_feed_client, "?licensed=all")) == {"lic", "unlic"}
    assert set(_ids(licensed_feed_client, "")) == {"lic", "unlic"}


def test_feed_licensed_filter_unknown_falls_back_to_all(licensed_feed_client):
    assert set(_ids(licensed_feed_client, "?licensed=bogus")) == {"lic", "unlic"}


@pytest.fixture
def licensed_feed_client(tmp_path, monkeypatch):
    """2 revealed-поста: licensed (Olimpbet) и unlicensed (mostbet casino)."""
    dbfile = tmp_path / "lic.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    _add_post(conn, "lic", 82, "gambling", "escalate", 1,
              caption="Ставки на Olimpbet — заходи и выигрывай")
    _add_post(conn, "unlic", 90, "gambling", "escalate", 1,
              caption="mostbet casino — заноси депозит сейчас")
    conn.close()
    with TestClient(app) as c:
        yield c


def _feed_row(client, pid):
    for row in client.get("/api/feed").json():
        if row["post"]["id"] == pid:
            return row
    raise AssertionError(f"post {pid} not in feed")


def test_feed_licensed_flag_for_licensed_operator(licensed_feed_client):
    row = _feed_row(licensed_feed_client, "lic")
    assert row["post"]["licensed"] is True
    assert row["post"]["licensed_operators"] == ["Olimpbet"]
    # риск не занижен флагом
    assert row["score"]["risk"] == 82
    assert row["recommended_action"] == "escalate"


def test_feed_no_licensed_flag_for_unlicensed_operator(licensed_feed_client):
    row = _feed_row(licensed_feed_client, "unlic")
    assert row["post"]["licensed"] is False
    assert row["post"]["licensed_operators"] == []


@pytest.fixture
def licensed_detail_client(tmp_path, monkeypatch):
    """drill-down пост с лицензированным оператором в combined_text."""
    dbfile = tmp_path / "licdetail.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed) VALUES("
        "'lic','tiktok','@a','http://x','Ставки',"
        "'2026-06-24T10:00:00',NULL,NULL,'seed',1)"
    )
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) VALUES("
        "'lic','Ставки','Olimpbet лучший букмекер','OCR',?,'Ставки Olimpbet лучший букмекер OCR',?)",
        (json.dumps([]), json.dumps([])),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES("
        "'lic',82,'gambling',?,?,'escalate','2026-06-24T10:00:00')",
        (json.dumps({"gambling": 0.9, "clean": 0.1}),
         json.dumps([{"feature": "casino_betting_brand", "weight": 0.7,
                      "evidence": "Olimpbet"}])),
    )
    # нелицензированный пост для негативного кейса
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed) VALUES("
        "'unlic','tiktok','@b','http://y','депозит',"
        "'2026-06-24T10:00:00',NULL,NULL,'seed',1)"
    )
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) VALUES("
        "'unlic','депозит','mostbet casino','OCR',?,'депозит mostbet casino OCR',?)",
        (json.dumps([]), json.dumps([])),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES("
        "'unlic',90,'gambling',?,?,'escalate','2026-06-24T10:00:00')",
        (json.dumps({"gambling": 0.95, "clean": 0.05}),
         json.dumps([{"feature": "casino_betting_brand", "weight": 0.7,
                      "evidence": "mostbet"}])),
    )
    conn.commit()
    conn.close()
    with TestClient(app) as c:
        yield c


def test_post_detail_licensed_flag_and_note(licensed_detail_client):
    from app.decision.licensed import COMPLIANCE_HINT, REGISTRY_DISCLAIMER

    data = licensed_detail_client.get("/api/post/lic").json()
    assert data["post"]["licensed"] is True
    assert data["post"]["licensed_operators"] == ["Olimpbet"]
    # подсказка и дисклеймер присутствуют для лицензированного
    assert data["licensed_note"] == COMPLIANCE_HINT
    assert data["licensed_disclaimer"] == REGISTRY_DISCLAIMER
    # риск не занижен
    assert data["score"]["risk"] == 82
    assert data["recommended_action"] == "escalate"
    # explain добавил буллет «✓ Разрешён в РК» первым
    assert data["explanation"][0].startswith("✓ Разрешён в РК")
    assert "Olimpbet" in data["explanation"][0]


def test_post_detail_no_licensed_flag_for_unlicensed(licensed_detail_client):
    data = licensed_detail_client.get("/api/post/unlic").json()
    assert data["post"]["licensed"] is False
    assert data["post"]["licensed_operators"] == []
    # подсказки пустые, когда не лицензирован
    assert data["licensed_note"] == ""
    assert data["licensed_disclaimer"] == ""
    assert not any(b.startswith("✓ Разрешён в РК") for b in data["explanation"])


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
        "post", "extracted", "score", "explanation", "recommended_action",
        "licensed_note", "licensed_disclaimer", "case_recommendations", "graph"}
    # граф-связи (эго-сеть) для мини-граф на странице анализа.
    assert set(data["graph"].keys()) == {"nodes", "edges"}
    assert isinstance(data["graph"]["nodes"], list) and isinstance(data["graph"]["edges"], list)
    # case_recommendations: список точечных рекомендаций под этот кейс (2-5).
    assert isinstance(data["case_recommendations"], list)
    assert 1 <= len(data["case_recommendations"]) <= 5
    for r in data["case_recommendations"]:
        assert {"title", "rationale", "action", "priority", "evidence"}.issubset(r)
        assert r["priority"] in {"high", "medium", "low"}
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


# --- QUEUE CONFIRM: POST /api/post/{id}/queue (аналитик подтверждает очередь) --- #


@pytest.fixture
def queue_client(tmp_path, monkeypatch):
    """Засевает 1 revealed-пост (qin) и 1 нераскрытый (qout) на одном файле БД.

    Роут пишет в request.app.state.db (тот же файл) — поэтому аудит можно
    перечитать свежим db.connect() ПОСЛЕ запроса.
    """
    dbfile = tmp_path / "queue.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    _add_post(conn, "qin", 85, "gambling", "escalate", 1)   # в очереди (revealed=1)
    _add_post(conn, "qout", 70, "gambling", "escalate", 0)  # НЕ в очереди (revealed=0)
    conn.close()
    with TestClient(app) as c:
        yield c


def test_queue_false_removes_post_from_feed(queue_client):
    # до запроса qin в ленте
    assert "qin" in _ids(queue_client)
    resp = queue_client.post("/api/post/qin/queue", json={"queued": False})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "post_id": "qin", "queued": False}
    # после — qin исчез из ленты (revealed=0)
    assert "qin" not in _ids(queue_client)


def test_queue_true_adds_post_to_feed(queue_client):
    # до запроса qout НЕ в ленте (revealed=0)
    assert "qout" not in _ids(queue_client)
    resp = queue_client.post("/api/post/qout/queue", json={"queued": True})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "post_id": "qout", "queued": True}
    # теперь появился в ленте
    assert "qout" in _ids(queue_client)


def test_queue_unknown_post_404(queue_client):
    resp = queue_client.post("/api/post/nope/queue", json={"queued": True})
    assert resp.status_code == 404


def test_queue_missing_body_field_400(queue_client):
    assert queue_client.post("/api/post/qin/queue", json={}).status_code == 400


def test_queue_invalid_body_type_400(queue_client):
    # "queued" не bool -> 400
    assert queue_client.post(
        "/api/post/qin/queue", json={"queued": "yes"}
    ).status_code == 400


def test_queue_non_dict_body_400(queue_client):
    # тело — не JSON-объект (список) -> 400
    assert queue_client.post(
        "/api/post/qin/queue", json=["queued"]
    ).status_code == 400


def test_queue_writes_audit_row(queue_client, tmp_path):
    queue_client.post("/api/post/qin/queue", json={"queued": False})
    # читаем тот же файл БД свежим соединением (роут писал в app.state.db)
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT action FROM audit WHERE post_id=? AND action='queue_confirm'",
            ("qin",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["action"] == "queue_confirm"


def test_feed_post_dict_includes_queued_true(queue_client):
    row = _feed_row(queue_client, "qin")
    assert row["post"]["queued"] is True


def test_post_detail_post_dict_includes_queued(queue_client):
    # detail делает SELECT * -> revealed виден -> queued соответствует revealed
    data = queue_client.get("/api/post/qin").json()
    assert data["post"]["queued"] is True
    data_out = queue_client.get("/api/post/qout").json()
    assert data_out["post"]["queued"] is False


# --- ГРАФ-СВЯЗИ: GET /api/post/{id} -> "graph" (эго-сеть для мини-граф) ------ #


@pytest.fixture
def graph_detail_client(tmp_path, monkeypatch):
    """Два поста, ДЕЛЯЩИХ одну сущность @casinox (telegram), -> build_ego_graph их связывает.

      g1: extracted.entities = [{telegram @casinox}]  + score 88
      g2: extracted.entities = [{telegram @casinox}]  + score 75 (co-post)
    Узел-сущность "entity:telegram:casinox" общий -> g1 и g2 в одной компоненте.
    Третий пост solo (без сущностей) для негативного кейса — отдельная фикстура ниже.
    """
    dbfile = tmp_path / "graphdetail.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    shared_entity = [{"type": "telegram", "value": "@casinox", "normalized": "casinox"}]
    for pid, risk in (("g1", 88), ("g2", 75)):
        conn.execute(
            "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
            "media_path, thumb_url, source, revealed) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, "tiktok", "@" + pid, "http://x/" + pid, "casino", "2026-06-24T10:00:00",
             None, None, "seed", 1),
        )
        conn.execute(
            "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
            "visual_concepts_json, combined_text, entities_json) VALUES(?,?,?,?,?,?,?)",
            (pid, "casino", "", "", json.dumps([]), "casino",
             json.dumps(shared_entity)),
        )
        conn.execute(
            "INSERT INTO scores(post_id, risk, category, class_probs_json, "
            "top_features_json, recommended_action, scored_at) VALUES(?,?,?,?,?,?,?)",
            (pid, risk, "gambling", json.dumps({"gambling": 0.9, "clean": 0.1}),
             json.dumps([{"feature": "casino_betting_brand", "weight": 0.7,
                          "evidence": "@casinox"}]),
             "escalate", "2026-06-24T10:00:00"),
        )
    conn.commit()
    conn.close()
    with TestClient(app) as c:
        yield c


def test_post_detail_graph_links_shared_entity_and_copost(graph_detail_client):
    data = graph_detail_client.get("/api/post/g1").json()
    graph = data["graph"]
    assert isinstance(graph["nodes"], list) and isinstance(graph["edges"], list)
    assert len(graph["nodes"]) > 0
    node_ids = {n["id"] for n in graph["nodes"]}
    # общая сущность @casinox (telegram) -> "entity:telegram:casinox"
    assert "entity:telegram:casinox" in node_ids
    # сам пост и со-пост (делит сущность) присутствуют как узлы-посты
    assert "post:g1" in node_ids
    assert "post:g2" in node_ids


@pytest.fixture
def graph_solo_client(tmp_path, monkeypatch):
    """Пост БЕЗ сущностей: эго-граф = только его собственный узел-пост (рёбер нет)."""
    dbfile = tmp_path / "graphsolo.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed) VALUES("
        "'solo','tiktok','@solo','http://x/solo','просто текст',"
        "'2026-06-24T10:00:00',NULL,NULL,'seed',1)"
    )
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) VALUES("
        "'solo','просто текст','','',?,'просто текст',?)",
        (json.dumps([]), json.dumps([])),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES("
        "'solo',20,'clean',?,?,'auto_clear','2026-06-24T10:00:00')",
        (json.dumps({"clean": 0.8, "gambling": 0.2}), json.dumps([])),
    )
    conn.commit()
    conn.close()
    with TestClient(app) as c:
        yield c


def test_post_detail_graph_no_entities_still_200(graph_solo_client):
    resp = graph_solo_client.get("/api/post/solo")
    assert resp.status_code == 200  # никогда не 500
    graph = resp.json()["graph"]
    assert set(graph.keys()) == {"nodes", "edges"}
    assert isinstance(graph["nodes"], list) and isinstance(graph["edges"], list)
    # без сущностей нет рёбер; но собственный узел-пост присутствует
    assert graph["edges"] == []
    assert "post:solo" in {n["id"] for n in graph["nodes"]}


# --- GET /api/monitor/entry — детальная статистика записи мониторинга ---

@pytest.fixture
def monitor_client(tmp_path, monkeypatch):
    """Посты, упоминающие бренд 1xbet (m1/m2), + посторонний (m3)."""
    dbfile = tmp_path / "mon.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    _add_post(conn, "m1", 90, "gambling", "escalate", 1, caption="промокод 1xbet занос")
    _add_post(conn, "m2", 80, "gambling", "escalate", 1, caption="1xBet бонус сегодня")
    _add_post(conn, "m3", 20, "clean", "auto_clear", 1, caption="погода в Алматы")
    conn.close()
    with TestClient(app) as c:
        yield c


def test_monitor_entry_brand_aggregates_and_posts(monitor_client):
    d = monitor_client.get("/api/monitor/entry",
                           params={"target": "1xbet", "platform": "all"}).json()
    assert d["target"] == "1xbet"
    # m1,m2 упоминают 1xbet; m3 — нет
    assert d["stats"]["total"] == 2
    assert d["stats"]["flagged"] == 2  # оба escalate
    assert d["stats"]["by_category"] == {"gambling": 2}
    assert {row["post"]["id"] for row in d["posts"]} == {"m1", "m2"}
    assert d["posts"][0]["score"]["risk"] == 90  # сортировка по риску убыв.
    assert d["licensed"] is True  # 1xBet.kz лицензирован в РК -> флаг «разрешён в РК»


def test_monitor_entry_empty_target_is_safe(monitor_client):
    d = monitor_client.get("/api/monitor/entry",
                           params={"target": "", "platform": "all"}).json()
    assert d["stats"]["total"] == 0 and d["posts"] == []


def test_monitor_entry_platform_filter(monitor_client):
    # все засеяны на tiktok -> фильтр youtube не вернёт ничего
    d = monitor_client.get("/api/monitor/entry",
                           params={"target": "1xbet", "platform": "youtube"}).json()
    assert d["stats"]["total"] == 0
