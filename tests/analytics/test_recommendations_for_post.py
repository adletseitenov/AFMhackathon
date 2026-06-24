"""Тесты ТОЧЕЧНЫХ рекомендаций под конкретный кейс (Задача 2).

recommendations_for_post(conn, post, score) -> list[dict] выдаёт 2-5
рекомендаций ИМЕННО для этого поста по его категории/риску/бренду/площадке/
лицензии/реквизитам, отсортированных по важности. Форма каждой рекомендации —
{title, rationale, action, priority, evidence} (+ score). Никогда не падает.
"""

import json

import pytest

from app import config, db
from app.analytics.recommend import recommendations_for_post

_REC_KEYS = {"title", "rationale", "action", "priority", "evidence"}
_PRIORITIES = {"high", "medium", "low"}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "rec_post.db")
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


def _seed_post(conn, post_id, platform, caption="cap", author="@a"):
    conn.execute(
        "INSERT INTO posts (id, platform, author_handle, url, caption, "
        "posted_at, media_path, thumb_url, source, revealed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (post_id, platform, author, "http://x", caption,
         "2026-06-20T10:00:00", None, None, "discovered"),
    )


def _seed_score(conn, post_id, risk, category, recommended_action):
    conn.execute(
        "INSERT INTO scores (post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) "
        "VALUES (?, ?, ?, '{}', '[]', ?, ?)",
        (post_id, risk, category, recommended_action, "2026-06-20T10:00:00"),
    )


def _seed_extracted(conn, post_id, entities, combined=""):
    conn.execute(
        "INSERT INTO extracted (post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) "
        "VALUES (?, '', '', '', '[]', ?, ?)",
        (post_id, combined, json.dumps(entities, ensure_ascii=False)),
    )


def _assert_well_formed(recs):
    assert isinstance(recs, list)
    assert 2 <= len(recs) <= 5, "ожидалось 2-5 точечных рекомендаций"
    for r in recs:
        assert isinstance(r, dict)
        assert _REC_KEYS.issubset(r.keys())
        assert r["priority"] in _PRIORITIES
        assert isinstance(r["title"], str) and r["title"].strip()
        assert isinstance(r["action"], str) and r["action"].strip()
        assert isinstance(r["rationale"], str) and r["rationale"].strip()
    # отсортированы по важности (score убыв.)
    if all("score" in r for r in recs):
        scores = [r["score"] for r in recs]
        assert scores == sorted(scores, reverse=True)


def test_gambling_unlicensed_high_risk_recommends_takedown_and_payment_block(conn):
    _seed_post(conn, "p1", "tiktok", caption="заходи в mostbet, депозит x2")
    _seed_score(conn, "p1", 95, "gambling", "escalate")
    _seed_extracted(conn, "p1", [
        {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
    ], combined="заходи в mostbet депозит")
    conn.commit()

    recs = recommendations_for_post(conn, "p1", 95)
    _assert_well_formed(recs)
    blob = " ".join((r["title"] + " " + r["rationale"] + " " + r["action"]).lower()
                     for r in recs)
    assert "takedown" in blob or "удал" in blob, "ожидался takedown/удаление контента"
    assert "блокир" in blob and "платеж" in blob.replace("ё", "е"), \
        "ожидалась блокировка платёжных каналов для нелицензированного гемблинга"
    # высокий риск -> есть high-приоритет
    assert any(r["priority"] == "high" for r in recs)


def test_gambling_licensed_recommends_ad_compliance_not_block(conn):
    _seed_post(conn, "p2", "tiktok", caption="реклама olimpbet, ставки на спорт",
               author="@olimpbet")
    _seed_score(conn, "p2", 80, "gambling", "escalate")
    _seed_extracted(conn, "p2", [
        {"type": "betting_brand", "value": "Olimpbet", "normalized": "Olimpbet"},
    ], combined="реклама olimpbet ставки")
    conn.commit()

    recs = recommendations_for_post(conn, "p2", 80)
    _assert_well_formed(recs)
    blob = " ".join((r["title"] + " " + r["rationale"] + " " + r["action"]).lower()
                    for r in recs)
    # лицензированный оператор -> проверка рекламы, НЕ блокировка платёжных каналов
    assert "реклам" in blob, "для лицензированного оператора — проверка рекламных норм"
    # ни одна рекомендация не должна предписывать блокировку платёжных каналов
    for r in recs:
        assert "блокировку платёжных каналов" not in r["action"].lower(), \
            "лицензированный оператор — без блокировки платёжных каналов"
        assert "takedown" not in r["action"].lower()


def test_pyramid_recommends_public_warning(conn):
    _seed_post(conn, "p3", "instagram", caption="гарантированный доход 30% в месяц")
    _seed_score(conn, "p3", 85, "pyramid", "escalate")
    conn.commit()

    recs = recommendations_for_post(conn, "p3", 85)
    _assert_well_formed(recs)
    blob = " ".join((r["title"] + " " + r["rationale"] + " " + r["action"]).lower()
                    for r in recs)
    assert "предупрежд" in blob or "пирамид" in blob, \
        "для пирамиды — публичное предупреждение"


def test_promo_code_and_wallet_attached_to_case(conn):
    _seed_post(conn, "p4", "telegram", caption="промокод BONUS500")
    _seed_score(conn, "p4", 90, "gambling", "escalate")
    _seed_extracted(conn, "p4", [
        {"type": "promo_code", "value": "BONUS500", "normalized": "bonus500"},
        {"type": "crypto_wallet", "value": "TQ123abc", "normalized": "tq123abc"},
        {"type": "betting_brand", "value": "1xBet", "normalized": "1xbet"},
    ], combined="промокод BONUS500 кошелёк TQ123abc")
    conn.commit()

    recs = recommendations_for_post(conn, "p4", 90)
    _assert_well_formed(recs)
    blob = " ".join((r["title"] + " " + r["rationale"] + " " + r["action"]).lower()
                    for r in recs)
    # найденные реквизиты должны быть приобщены к делу / переданы провайдеру.
    assert "промокод" in blob or "кошел" in blob or "реквизит" in blob, \
        "найденные реквизиты должны попасть в точечную рекомендацию"
    assert "дел" in blob, "реквизиты приобщить к делу"


def test_low_risk_clean_post_gets_safe_default(conn):
    _seed_post(conn, "p5", "youtube", caption="обзор настольной игры")
    _seed_score(conn, "p5", 5, "clean", "auto_clear")
    conn.commit()

    recs = recommendations_for_post(conn, "p5", 5)
    _assert_well_formed(recs)
    # чистый низкорисковый пост -> без жёстких действий, есть низкий приоритет.
    assert any(r["priority"] == "low" for r in recs)
    for r in recs:
        assert "takedown" not in r["action"].lower(), \
            "чистый пост — никакого takedown"


def test_accepts_post_dict_without_db_row(conn):
    # post как dict (как из _post_dict) + score как int — без записи в БД.
    post = {
        "id": "x9", "platform": "tiktok", "author_handle": "@scam",
        "caption": "mostbet бонус", "category": "gambling",
        "licensed": False, "licensed_operators": [],
    }
    recs = recommendations_for_post(conn, post, 91)
    _assert_well_formed(recs)


def test_missing_post_does_not_crash(conn):
    # несуществующий post_id -> разумный дефолт, без исключения.
    recs = recommendations_for_post(conn, "nope-404", 0)
    assert isinstance(recs, list)
    assert len(recs) >= 1
    for r in recs:
        assert _REC_KEYS.issubset(r.keys())
        assert r["priority"] in _PRIORITIES


def test_none_score_does_not_crash(conn):
    _seed_post(conn, "p6", "tiktok", caption="что-то")
    conn.commit()
    recs = recommendations_for_post(conn, "p6", None)
    assert isinstance(recs, list) and len(recs) >= 1
