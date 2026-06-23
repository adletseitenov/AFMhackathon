"""F3 — тесты decision/scoring: маппинг risk->action + score_post (предсказание,
персист score, аудит). Классификатор монки-патчится фейком, чтобы тест Decision
не зависел от обученного артефакта F1 и не грузил тяжёлые модели."""

import pytest

from app import config, db
from app.config import REVIEW_THRESHOLD
from app.decision import scoring
from app.models import AuditEntry, Extracted, FeatureHit, Post, Score


# --- Задача 1: recommend_action thresholds (§0.11, 3 ветки, без 'monitor') ---
def test_recommend_action_auto_clear_below_review():
    assert scoring.recommend_action(0) == "auto_clear"
    assert scoring.recommend_action(39) == "auto_clear"


def test_recommend_action_review_in_middle_band():
    assert scoring.recommend_action(40) == "review"
    assert scoring.recommend_action(69) == "review"


def test_recommend_action_escalate_at_or_above_escalate():
    assert scoring.recommend_action(70) == "escalate"
    assert scoring.recommend_action(100) == "escalate"


def test_recommend_action_matches_config_action_for_risk():
    # Единый маппинг — никаких двух разных реализаций (§0.11).
    for r in (0, 10, 39, 40, 55, 69, 70, 95, 100):
        assert scoring.recommend_action(r) == config.action_for_risk(r)


# --- фейковый классификатор: эхо фиксированного Score, привязанного к post_id ---
class _FakeClassifier:
    def __init__(self, score: Score):
        self._score = score

    @classmethod
    def for_score(cls, score: Score) -> "_FakeClassifier":
        return cls(score)

    def predict(self, extracted: Extracted) -> Score:
        return Score(
            post_id=extracted.post_id,
            risk=self._score.risk,
            category=self._score.category,
            class_probs=self._score.class_probs,
            top_features=self._score.top_features,
        )


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    conn = db.connect()
    db.init_db(conn)
    # сбрасываем модуль-кэш классификатора между тестами
    monkeypatch.setattr(scoring, "_clf_cache", None)
    yield conn
    conn.close()


def _patch_clf(monkeypatch, score: Score):
    monkeypatch.setattr(
        scoring.RiskClassifier,
        "load",
        classmethod(lambda cls, path=None: _FakeClassifier.for_score(score)),
    )


def _post(pid="p1"):
    return Post(
        id=pid, platform="tiktok", author_handle="@scammer",
        url="https://x/y", caption="cap", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="seed",
    )


def _extracted(pid="p1", text="text"):
    return Extracted(
        post_id=pid, caption=text, transcript="", ocr_text="",
        visual_concepts=[], combined_text=text, entities=[],
    )


# --- Задача 2: score_post ---
def test_high_risk_extracted_escalates_and_persists(fresh_db, monkeypatch):
    conn = fresh_db
    high = Score(
        post_id="p1", risk=88, category="gambling",
        class_probs={"gambling": 0.9, "pyramid": 0.05, "fraud": 0.03, "clean": 0.02},
        top_features=[FeatureHit(feature="casino_betting_brand", weight=0.6, evidence="1xBet")],
    )
    _patch_clf(monkeypatch, high)
    db.insert_post(conn, _post("p1"))
    result = scoring.score_post(_post("p1"), _extracted("p1"), conn=conn)
    assert result.category in {"gambling", "pyramid"}
    assert scoring.recommend_action(result.risk) == "escalate"
    row = db.get_score_row(conn, "p1")
    assert row is not None
    assert row["risk"] == 88
    assert row["category"] == "gambling"
    assert row["recommended_action"] == "escalate"
    assert row["scored_at"]


def test_clean_extracted_auto_clears(fresh_db, monkeypatch):
    conn = fresh_db
    clean = Score(
        post_id="p2", risk=12, category="clean",
        class_probs={"gambling": 0.02, "pyramid": 0.02, "fraud": 0.04, "clean": 0.92},
        top_features=[],
    )
    _patch_clf(monkeypatch, clean)
    db.insert_post(conn, _post("p2"))
    result = scoring.score_post(_post("p2"), _extracted("p2", "обычный пост"), conn=conn)
    assert result.risk < REVIEW_THRESHOLD
    assert scoring.recommend_action(result.risk) == "auto_clear"
    row = db.get_score_row(conn, "p2")
    assert row["recommended_action"] == "auto_clear"


def test_audit_row_written(fresh_db, monkeypatch):
    conn = fresh_db
    high = Score(
        post_id="p3", risk=80, category="pyramid",
        class_probs={"gambling": 0.1, "pyramid": 0.8, "fraud": 0.05, "clean": 0.05},
        top_features=[FeatureHit(feature="payout_promise", weight=0.7, evidence="30% в месяц")],
    )
    _patch_clf(monkeypatch, high)
    db.insert_post(conn, _post("p3"))
    scoring.score_post(_post("p3"), _extracted("p3"), conn=conn)
    rows = conn.execute(
        "SELECT post_id, action, actor FROM audit WHERE post_id=?", ("p3",)
    ).fetchall()
    assert len(rows) >= 1
    assert rows[0]["post_id"] == "p3"
    assert rows[0]["action"] == "scored"
    assert rows[0]["actor"] == "system"


def test_score_post_opens_own_conn_when_none(tmp_path, monkeypatch):
    # conn=None -> score_post открывает своё соединение по config.DB_PATH и закрывает.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "own.db")
    monkeypatch.setattr(scoring, "_clf_cache", None)
    setup = db.connect()
    db.init_db(setup)
    db.insert_post(setup, _post("p4"))
    setup.close()
    high = Score(
        post_id="p4", risk=75, category="fraud",
        class_probs={"gambling": 0.1, "pyramid": 0.1, "fraud": 0.7, "clean": 0.1},
        top_features=[],
    )
    _patch_clf(monkeypatch, high)
    result = scoring.score_post(_post("p4"), _extracted("p4"))
    assert result.risk == 75
    check = db.connect()
    row = db.get_score_row(check, "p4")
    check.close()
    assert row is not None
    assert row["recommended_action"] == "escalate"


def test_classifier_loaded_once_and_cached(fresh_db, monkeypatch):
    conn = fresh_db
    calls = {"n": 0}
    clean = Score(
        post_id="p5", risk=10, category="clean",
        class_probs={"gambling": 0.02, "pyramid": 0.02, "fraud": 0.02, "clean": 0.94},
        top_features=[],
    )

    def counting_load(cls, path=None):
        calls["n"] += 1
        return _FakeClassifier.for_score(clean)

    monkeypatch.setattr(scoring.RiskClassifier, "load", classmethod(counting_load))
    db.insert_post(conn, _post("p5"))
    db.insert_post(conn, _post("p6"))
    scoring.score_post(_post("p5"), _extracted("p5"), conn=conn)
    scoring.score_post(_post("p6"), _extracted("p6"), conn=conn)
    assert calls["n"] == 1  # классификатор загружен ровно один раз (модуль-кэш)
