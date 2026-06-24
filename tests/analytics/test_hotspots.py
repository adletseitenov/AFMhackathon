"""Тесты сводки «самое опасное ПРЯМО СЕЙЧАС» (recommend.build_hotspots).

build_hotspots(conn=None, limit=5) -> dict — агрегат для верхнего баннера
дашборда: топ-проблемы (категории-угрозы), топ-операторы (бренды), топ-каналы
Telegram/YouTube и приоритезированные рекомендации по самому горячему срезу.

ЧИСТО rule-based (критерий №2): читает posts/scores/extracted read-only, никогда
не падает (на ошибке/пустой БД отдаёт безопасную пустую форму со всеми ключами).
БД изолируется через monkeypatch config.DB_PATH на временный файл (§0.2); данные
засеваются прямо в таблицы (без вызова модели).
"""

import json

import pytest

from app import config, db
from app.analytics.recommend import build_hotspots

_REC_KEYS = {"title", "rationale", "action", "priority", "evidence"}
_PRIORITIES = {"high", "medium", "low"}
_TOP_KEYS = {"top_problems", "top_operators", "top_telegram", "top_youtube"}
_ALL_KEYS = _TOP_KEYS | {"recommendations"}


# --- фикстуры/хелперы засева ---------------------------------------------------
@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Временная БД с инициализированной схемой; config.DB_PATH перенаправлен."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hotspots_test.db")
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


def _seed_post(conn, post_id, platform, author_handle="@a",
               posted_at="2026-06-20T10:00:00"):
    conn.execute(
        "INSERT INTO posts (id, platform, author_handle, url, caption, "
        "posted_at, media_path, thumb_url, source, revealed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (post_id, platform, author_handle, "http://x", "cap", posted_at,
         None, None, "seed"),
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


def _assert_shape(hs):
    """Контракт формы: dict со всеми 5 ключами; 4 топа — списки; recs — список."""
    assert isinstance(hs, dict)
    assert _ALL_KEYS.issubset(hs.keys())
    for k in _TOP_KEYS:
        assert isinstance(hs[k], list), f"{k} должен быть списком"
    assert isinstance(hs["recommendations"], list)


def _assert_recs_well_formed(recs):
    for r in recs:
        assert isinstance(r, dict)
        assert _REC_KEYS.issubset(r.keys())
        assert r["priority"] in _PRIORITIES
        assert isinstance(r["title"], str) and r["title"].strip()
        assert isinstance(r["action"], str) and r["action"].strip()


# --- 1: пустая БД -> форма со всеми ключами, без падения ----------------------
def test_empty_db_shape_no_crash(conn):
    hs = build_hotspots(conn)
    _assert_shape(hs)
    # на пустой БД 4 топа пусты, а рекомендации — непустой дефолт build_recommendations.
    for k in _TOP_KEYS:
        assert hs[k] == []
    assert len(hs["recommendations"]) >= 1
    _assert_recs_well_formed(hs["recommendations"])


def test_no_conn_arg_opens_own_connection(conn):
    # conn=None -> сам открывает соединение через config.DB_PATH, без падения.
    hs = build_hotspots()
    _assert_shape(hs)


# --- 2: непустые топ-проблемы при наличии скоров ------------------------------
def test_top_problems_non_empty_with_correct_types(conn):
    _seed_post(conn, "g1", "tiktok")
    _seed_post(conn, "g2", "tiktok")
    _seed_post(conn, "py1", "instagram")
    _seed_post(conn, "f1", "telegram")
    _seed_post(conn, "c1", "youtube")
    _seed_score(conn, "g1", 60, "gambling", "review")
    _seed_score(conn, "g2", 80, "gambling", "escalate")
    _seed_score(conn, "py1", 75, "pyramid", "escalate")
    _seed_score(conn, "f1", 90, "fraud", "escalate")
    _seed_score(conn, "c1", 5, "clean", "auto_clear")  # clean исключается
    conn.commit()

    hs = build_hotspots(conn)
    _assert_shape(hs)
    probs = hs["top_problems"]
    assert probs, "ожидались непустые топ-проблемы"
    cats = {p["category"] for p in probs}
    assert "clean" not in cats, "clean должен быть исключён"
    for p in probs:
        assert isinstance(p["category"], str) and p["category"]
        assert isinstance(p["label"], str) and p["label"]
        assert isinstance(p["count"], int)
        assert isinstance(p["avg_risk"], float)
        assert isinstance(p["escalate_count"], int)
    # escalate_count считает только escalate-строки.
    gambling = next(p for p in probs if p["category"] == "gambling")
    assert gambling["count"] == 2
    assert gambling["escalate_count"] == 1  # g2 escalate, g1 review


# --- 3: сортировка по опасности (avg_risk убыв.) ------------------------------
def test_top_problems_sorted_by_danger(conn):
    # fraud avg ~95, gambling avg ~50 -> fraud первый.
    for i in range(3):
        _seed_post(conn, f"fr{i}", "tiktok")
        _seed_score(conn, f"fr{i}", 95, "fraud", "escalate")
    for i in range(3):
        _seed_post(conn, f"gm{i}", "instagram")
        _seed_score(conn, f"gm{i}", 50, "gambling", "review")
    conn.commit()

    hs = build_hotspots(conn)
    probs = hs["top_problems"]
    assert probs[0]["category"] == "fraud"
    avgs = [p["avg_risk"] for p in probs]
    assert avgs == sorted(avgs, reverse=True), "avg_risk должен быть невозрастающим"


# --- 4: топ-операторы (лицензированный vs нелицензированный) -------------------
def test_top_operators_licensed_flags_and_sorting(conn):
    # mostbet (нелиценз., высокий риск) и Olimpbet (лиценз., ниже риск).
    for i in range(4):
        pid = f"mb{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    for i in range(3):
        pid = f"ob{i}"
        _seed_post(conn, pid, "telegram")
        _seed_score(conn, pid, 60, "gambling", "review")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Olimpbet", "normalized": "Olimpbet"},
        ])
    conn.commit()

    hs = build_hotspots(conn)
    ops = hs["top_operators"]
    assert ops, "ожидались топ-операторы"
    by_brand = {o["brand"].lower(): o for o in ops}
    assert "mostbet" in by_brand and "olimpbet" in by_brand
    assert by_brand["mostbet"]["licensed"] is False
    assert by_brand["olimpbet"]["licensed"] is True
    for o in ops:
        assert isinstance(o["count"], int) and o["count"] >= 1
        assert isinstance(o["avg_risk"], float)
        assert isinstance(o["licensed"], bool)
    # сортировка по avg_risk убыв.: mostbet (92) выше Olimpbet (60).
    avgs = [o["avg_risk"] for o in ops]
    assert avgs == sorted(avgs, reverse=True)
    assert ops[0]["brand"].lower() == "mostbet"


# --- 5: топ-каналы Telegram / YouTube -----------------------------------------
def test_top_telegram_and_youtube_channels(conn):
    # два telegram-канала с разным средним риском.
    for i in range(3):
        _seed_post(conn, f"tgA{i}", "telegram", author_handle="@chanA")
        _seed_score(conn, f"tgA{i}", 90, "gambling", "escalate")
    for i in range(2):
        _seed_post(conn, f"tgB{i}", "telegram", author_handle="@chanB")
        _seed_score(conn, f"tgB{i}", 45, "gambling", "review")
    # youtube-канал — не должен попадать в telegram.
    for i in range(2):
        _seed_post(conn, f"yt{i}", "youtube", author_handle="@ytChan")
        _seed_score(conn, f"yt{i}", 70, "pyramid", "escalate")
    conn.commit()

    hs = build_hotspots(conn)
    tg = hs["top_telegram"]
    yt = hs["top_youtube"]
    tg_chans = [t["channel"] for t in tg]
    assert "@chanA" in tg_chans and "@chanB" in tg_chans
    assert "@ytChan" not in tg_chans, "youtube-канал не должен утекать в telegram"
    # @chanA (avg 90) раньше @chanB (avg 45).
    assert tg[0]["channel"] == "@chanA"
    tg_avgs = [t["avg_risk"] for t in tg]
    assert tg_avgs == sorted(tg_avgs, reverse=True)
    for t in tg:
        assert isinstance(t["count"], int) and isinstance(t["avg_risk"], float)
    # youtube содержит свой канал, не telegram-каналы.
    yt_chans = [y["channel"] for y in yt]
    assert "@ytChan" in yt_chans
    assert "@chanA" not in yt_chans


# --- 6: рекомендации отражают самый горячий срез ------------------------------
def test_recommendations_reflect_hottest_slice(conn):
    # доминирующий нелицензированный бренд mostbet (>=4 постов, высокий риск).
    for i in range(5):
        pid = f"mb{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    conn.commit()

    hs = build_hotspots(conn, limit=5)
    recs = hs["recommendations"]
    assert recs, "ожидались непустые рекомендации"
    _assert_recs_well_formed(recs)
    assert len(recs) <= 5
    blob = " ".join(
        (r["title"] + r["rationale"] + r["action"] + str(r["evidence"])).lower()
        for r in recs
    )
    # хотя бы одна рекомендация упоминает горячий бренд ИЛИ топ-категорию.
    assert "mostbet" in blob or "гемблинг" in blob or "gambling" in blob


# --- 7: limit уважается во всех топах и рекомендациях -------------------------
def test_limit_param_respected(conn):
    # три категории-угрозы + три бренда + три telegram-канала.
    cats = ["gambling", "pyramid", "fraud"]
    for ci, cat in enumerate(cats):
        for i in range(2):
            pid = f"{cat}{i}"
            _seed_post(conn, pid, "telegram", author_handle=f"@ch{ci}{i}")
            _seed_score(conn, pid, 80 + ci, cat, "escalate")
    brands = ["mostbet", "1win", "1xbet"]
    for bi, b in enumerate(brands):
        for i in range(2):
            pid = f"br{bi}{i}"
            _seed_post(conn, pid, "tiktok", author_handle=f"@br{bi}{i}")
            _seed_score(conn, pid, 85, "gambling", "escalate")
            _seed_extracted(conn, pid, [
                {"type": "betting_brand", "value": b, "normalized": b},
            ])
    # несколько youtube-каналов.
    for i in range(3):
        _seed_post(conn, f"y{i}", "youtube", author_handle=f"@yt{i}")
        _seed_score(conn, f"y{i}", 70, "pyramid", "escalate")
    conn.commit()

    hs = build_hotspots(conn, limit=2)
    _assert_shape(hs)
    assert len(hs["top_problems"]) <= 2
    assert len(hs["top_operators"]) <= 2
    assert len(hs["top_telegram"]) <= 2
    assert len(hs["top_youtube"]) <= 2
    assert len(hs["recommendations"]) <= 2
