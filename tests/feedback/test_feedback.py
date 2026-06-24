"""Тесты F-обратной-связи КӨЗ (app/feedback/routes.py, авто-роутер).

Активное обучение: аналитик подтверждает/отклоняет/переклассифицирует вердикт
модели; вердикт превращается в строку обучающей выборки data/analyst_labels.jsonl
({"text": <combined_text>, "label": <resolved category>}) — это КОНТРАКТ, который
потребляет агент модели/обучения.

Изоляция (как в tests/api):
  - config.DB_PATH монки-патчится на временный файл; данные пишутся ДО входа в
    контекст TestClient (lifespan откроет своё соединение к тому же файлу);
  - путь к файлу меток монки-патчится через routes.LABELS_PATH на tmp-файл,
    чтобы тесты НЕ засоряли реальный data/analyst_labels.jsonl;
  - реальное переобучение в тесте retrain монки-патчится (subprocess не запускаем).
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import config, db
import app.feedback.routes as fb
from app.main import app


def _add_post(conn, pid, risk, category, action, revealed=1,
              posted_at="2026-06-24T10:00:00", caption="cap",
              combined_text="combined text", platform="tiktok"):
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pid, platform, "@" + pid, "https://x/" + pid, caption, posted_at,
         None, None, "seed", revealed),
    )
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) VALUES(?,?,?,?,?,?,?)",
        (pid, caption, "", "", "[]", combined_text, "[]"),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES(?,?,?,?,?,?,?)",
        (pid, risk, category, json.dumps({category: 0.9, "clean": 0.1}),
         json.dumps([]), action, posted_at),
    )
    conn.commit()


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    # Файл меток — во временную папку, чтобы не трогать реальные данные.
    labels_file = tmp_path / "analyst_labels.jsonl"
    monkeypatch.setattr(fb, "LABELS_PATH", labels_file)

    conn = db.connect()
    db.init_db(conn)
    # p1: модель сказала gambling risk=85 (escalate)
    _add_post(conn, "p1", 85, "gambling", "escalate",
              combined_text="заносы 1xbet промокод x500")
    # p2: модель сказала pyramid risk=45 (review, около REVIEW=40)
    _add_post(conn, "p2", 45, "pyramid", "review",
              combined_text="пассивный доход гарантирую 30% реферал")
    # p3: модель сказала clean risk=72 (escalate, около ESCALATE=70)
    _add_post(conn, "p3", 72, "fraud", "escalate",
              combined_text="переведи на кошелёк верну x2")
    # p4: clean risk=5 — далеко от любой границы
    _add_post(conn, "p4", 5, "clean", "auto_clear",
              combined_text="сегодня хорошая погода в Алматы")
    conn.close()

    with TestClient(app) as c:
        c._labels_file = labels_file  # удобный доступ в тестах
        yield c


def _read_labels(path):
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


# --------------------------------------------------------------------------- #
# POST /api/feedback/verdict
# --------------------------------------------------------------------------- #

def test_verdict_confirm_keeps_model_category(client):
    resp = client.post("/api/feedback/verdict",
                       json={"post_id": "p1", "verdict": "confirm"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["total_labels"] == 1

    rows = _read_labels(client._labels_file)
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"text", "label"}
    assert rows[0]["label"] == "gambling"  # подтверждена категория модели
    assert rows[0]["text"] == "заносы 1xbet промокод x500"


def test_verdict_reject_marks_clean(client):
    resp = client.post("/api/feedback/verdict",
                       json={"post_id": "p1", "verdict": "reject"})
    assert resp.status_code == 200
    rows = _read_labels(client._labels_file)
    assert rows[-1]["label"] == "clean"  # отклонение => на самом деле clean
    assert rows[-1]["text"] == "заносы 1xbet промокод x500"


def test_verdict_reclassify_uses_provided_category(client):
    resp = client.post("/api/feedback/verdict",
                       json={"post_id": "p1", "verdict": "reclassify",
                             "category": "fraud"})
    assert resp.status_code == 200
    rows = _read_labels(client._labels_file)
    assert rows[-1]["label"] == "fraud"


def test_verdict_reclassify_requires_valid_category(client):
    # reclassify без category или с мусором => 400, файл не растёт
    r1 = client.post("/api/feedback/verdict",
                     json={"post_id": "p1", "verdict": "reclassify"})
    assert r1.status_code == 400
    r2 = client.post("/api/feedback/verdict",
                     json={"post_id": "p1", "verdict": "reclassify",
                           "category": "banana"})
    assert r2.status_code == 400
    assert _read_labels(client._labels_file) == []


def test_verdict_unknown_post_404(client):
    resp = client.post("/api/feedback/verdict",
                       json={"post_id": "nope", "verdict": "confirm"})
    assert resp.status_code == 404


def test_verdict_invalid_verdict_400(client):
    resp = client.post("/api/feedback/verdict",
                       json={"post_id": "p1", "verdict": "maybe"})
    assert resp.status_code == 400


def test_verdict_appends_not_overwrites(client):
    client.post("/api/feedback/verdict", json={"post_id": "p1", "verdict": "confirm"})
    client.post("/api/feedback/verdict", json={"post_id": "p2", "verdict": "reject"})
    rows = _read_labels(client._labels_file)
    assert len(rows) == 2
    assert [r["label"] for r in rows] == ["gambling", "clean"]


def test_verdict_writes_audit_row(client):
    client.post("/api/feedback/verdict", json={"post_id": "p1", "verdict": "confirm"})
    conn = db.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit WHERE post_id='p1' AND action='analyst_verdict'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


# --------------------------------------------------------------------------- #
# GET /api/feedback/stats
# --------------------------------------------------------------------------- #

def test_stats_empty(client):
    body = client.get("/api/feedback/stats").json()
    assert body["total_labels"] == 0
    assert body["by_label"] == {}
    assert body["labels_until_retrain"] == fb.RETRAIN_BATCH


def test_stats_counts_after_verdicts(client):
    client.post("/api/feedback/verdict", json={"post_id": "p1", "verdict": "confirm"})
    client.post("/api/feedback/verdict", json={"post_id": "p2", "verdict": "confirm"})
    client.post("/api/feedback/verdict", json={"post_id": "p3", "verdict": "reject"})
    body = client.get("/api/feedback/stats").json()
    assert body["total_labels"] == 3
    assert body["by_label"]["gambling"] == 1
    assert body["by_label"]["pyramid"] == 1
    assert body["by_label"]["clean"] == 1
    assert body["labels_until_retrain"] == fb.RETRAIN_BATCH - 3


# --------------------------------------------------------------------------- #
# GET /api/feedback/uncertain
# --------------------------------------------------------------------------- #

def test_uncertain_returns_boundary_posts(client):
    body = client.get("/api/feedback/uncertain").json()
    ids = {row["post"]["id"] for row in body}
    # p2 (45 ~ REVIEW 40) и p3 (72 ~ ESCALATE 70) около границ; p1 (85) и p4 (5) — нет
    assert "p2" in ids
    assert "p3" in ids
    assert "p1" not in ids
    assert "p4" not in ids


def test_uncertain_row_shape_matches_feed(client):
    body = client.get("/api/feedback/uncertain").json()
    assert body, "ожидались посты у границы"
    row = body[0]
    assert set(row.keys()) == {"post", "score", "recommended_action"}
    assert "risk" in row["score"] and "category" in row["score"]


def test_uncertain_limit(client):
    body = client.get("/api/feedback/uncertain?limit=1").json()
    assert len(body) == 1


# --------------------------------------------------------------------------- #
# POST /api/feedback/retrain
# --------------------------------------------------------------------------- #

def test_retrain_returns_job_id(client, monkeypatch):
    # Не запускаем настоящее обучение: подменяем функцию переобучения.
    def _fake_retrain(report):
        report("переобучение", 50)
        return {"before": 0.9, "after": 0.95, "delta": 0.05, "total_labels": 0}

    monkeypatch.setattr(fb, "_retrain_job", _fake_retrain)

    resp = client.post("/api/feedback/retrain")
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert isinstance(job_id, str) and job_id

    # дожидаемся завершения фоновой задачи
    import time
    deadline = time.monotonic() + 5.0
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert job is not None and job["status"] == "done", job
    assert job["result"]["delta"] == 0.05
