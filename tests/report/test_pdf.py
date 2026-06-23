"""Тесты F6 — PDF-досье КӨЗ (app/report/pdf.py + app/report/routes.py).

Изолируют БД через monkeypatch config.DB_PATH на временный файл, затем
сидируют пост/extracted/score КАНОНИЧЕСКИМ conn-first API (db.insert_post,
db.upsert_extracted, db.upsert_score). Не требуют torch/whisper/ocr/clip —
extracted берётся из сэмпл-данных, скоринг кладётся напрямую в таблицу scores.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.models import Entity, Extracted, FeatureHit, Post, Score, VisualConcept


def _seed(conn) -> None:
    """Положить один проскоренный пост 'p1' через канонический API БД."""
    post = Post(
        id="p1",
        platform="tiktok",
        author_handle="@lucky_casino",
        url="https://tiktok.com/@lucky/1",
        caption="Гарантированный доход 30% в месяц! Пиши в личку",
        posted_at="2026-06-20T10:00:00",
        media_path=None,
        thumb_url=None,
        source="seed",
    )
    ex = Extracted(
        post_id="p1",
        caption="Гарантированный доход 30% в месяц!",
        transcript="ставь и выигрывай каждый день в нашем казино",
        ocr_text="VAVADA BONUS 500%",
        visual_concepts=[
            VisualConcept(label="casino", score=0.91),
            VisualConcept(label="cash_flaunt", score=0.77),
        ],
        combined_text="гарантированный доход 30% vavada bonus казино",
        entities=[
            Entity(type="casino_brand", value="Vavada", normalized="vavada"),
            Entity(type="payout_promise", value="30% в месяц", normalized="30% в месяц"),
        ],
    )
    score = Score(
        post_id="p1",
        risk=88,
        category="gambling",
        class_probs={"gambling": 0.88, "pyramid": 0.07, "fraud": 0.03, "clean": 0.02},
        top_features=[
            FeatureHit(feature="casino_betting_brand", weight=0.6, evidence="Vavada"),
            FeatureHit(feature="payout_promise", weight=0.4, evidence="30% в месяц"),
        ],
    )
    db.insert_post(conn, post)
    db.upsert_extracted(conn, ex)
    db.reveal_post(conn, "p1")
    db.upsert_score(
        conn, score,
        recommended_action="escalate",
        scored_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """Временная БД (monkeypatch config.DB_PATH) с одним проскоренным постом."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "report_test.db")
    conn = db.connect()
    db.init_db(conn)
    _seed(conn)
    conn.close()
    return config.DB_PATH


def test_build_case_pdf_returns_pdf_bytes(seeded_db):
    from app.report.pdf import build_case_pdf

    data = build_case_pdf("p1")
    assert isinstance(data, bytes)
    assert len(data) > 0
    assert data.startswith(b"%PDF")


def test_build_case_pdf_accepts_explicit_conn(seeded_db):
    from app.report.pdf import build_case_pdf

    conn = db.connect()
    try:
        data = build_case_pdf("p1", conn=conn)
    finally:
        conn.close()
    assert data.startswith(b"%PDF")


def test_build_case_pdf_unknown_raises_keyerror(seeded_db):
    from app.report.pdf import build_case_pdf

    with pytest.raises(KeyError):
        build_case_pdf("does-not-exist")


def test_report_route_returns_pdf_for_existing_post(seeded_db):
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/report/p1.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert "p1" in resp.headers.get("content-disposition", "")


def test_report_route_404_for_unknown_id(seeded_db):
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/report/nope.pdf")
    assert resp.status_code == 404
