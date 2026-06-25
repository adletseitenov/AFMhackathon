"""Тесты ФОКУСНОГО режима рекомендаций + available_sources (Задача 1).

build_recommendations(conn, focus=None) при заданном focus сужает выдачу на ОДНУ
проблему (категорию) или источник (бренд/площадку): уже, конкретнее, С ЦИФРАМИ по
срезу, ранжировано тем же _rank_and_dedup. focus=None/мусор -> прежняя глобальная
выдача (без регресса). available_sources(conn) отдаёт срезы для UI-селектора.

БД изолируется monkeypatch config.DB_PATH на временный файл (§0.2); данные сеются
прямо в posts/scores/extracted (без вызова модели) — это unit-тест правил среза.
"""

import json

import pytest

from app import config, db
from app.analytics.recommend import available_sources, build_recommendations

_REC_KEYS = {"title", "rationale", "action", "priority", "evidence"}
_PRIORITIES = {"high", "medium", "low"}


# --- фикстуры/хелперы засева (стиль test_recommend.py) -------------------------
@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "recommend_focus_test.db")
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


def _seed_post(conn, post_id, platform, posted_at="2026-06-20T10:00:00"):
    conn.execute(
        "INSERT INTO posts (id, platform, author_handle, url, caption, "
        "posted_at, media_path, thumb_url, source, revealed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (post_id, platform, "@a", "http://x", "cap", posted_at, None, None, "seed"),
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


def _assert_well_formed(recs):
    assert isinstance(recs, list)
    assert len(recs) >= 1
    for r in recs:
        assert isinstance(r, dict)
        assert _REC_KEYS.issubset(r.keys())
        assert r["priority"] in _PRIORITIES
        assert "score" in r and isinstance(r["score"], (int, float))
        assert isinstance(r["title"], str) and r["title"].strip()
        assert isinstance(r["rationale"], str) and r["rationale"].strip()
        assert isinstance(r["action"], str) and r["action"].strip()
        assert isinstance(r["evidence"], (str, list))


def _seed_mixed_pyramid_and_gambling(conn):
    """Несколько пирамид + немного gambling с нелицензированным брендом."""
    for i in range(4):
        pid = f"py{i}"
        _seed_post(conn, pid, "instagram")
        _seed_score(conn, pid, 85, "pyramid", "escalate")
    for i in range(3):
        pid = f"g{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 90, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "1xBet", "normalized": "1xbet"},
        ])
    conn.commit()


# --- 1. focus="category:pyramid" -> только про пирамиды -----------------------
def test_focus_category_pyramid_string_form(conn):
    _seed_mixed_pyramid_and_gambling(conn)
    recs = build_recommendations(conn, focus="category:pyramid")
    _assert_well_formed(recs)
    for r in recs:
        blob = (r["title"] + r["rationale"] + r["action"]).lower()
        assert "пирамид" in blob, f"в фокусе пирамид ожидался текст про пирамиды: {r['title']}"
    # не должно протекать gambling-рекомендации про блокировку бренда 1xbet.
    assert not any(
        "1xbet" in (r["title"] + r["rationale"] + r["action"]).lower()
        for r in recs
    ), "в срезе pyramid не должно быть рекомендаций про gambling-бренд"


# --- 2. dict-форма эквивалентна строковой -------------------------------------
def test_focus_category_pyramid_dict_form_equivalent(conn):
    _seed_mixed_pyramid_and_gambling(conn)
    str_recs = build_recommendations(conn, focus="category:pyramid")
    dict_recs = build_recommendations(conn, focus={"kind": "category", "value": "pyramid"})
    assert [r["title"] for r in str_recs] == [r["title"] for r in dict_recs]


# --- 3. focus="brand:mostbet" (нелицензированный) -> блок/takedown про mostbet ---
def test_focus_brand_unlicensed_block_takedown(conn):
    for i in range(4):
        pid = f"x{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    # шум: посты с другим брендом (vavada), которых НЕ должно быть в срезе mostbet.
    for i in range(3):
        pid = f"m{i}"
        _seed_post(conn, pid, "telegram")
        _seed_score(conn, pid, 88, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Vavada", "normalized": "vavada"},
        ])
    conn.commit()

    recs = build_recommendations(conn, focus="brand:mostbet")
    _assert_well_formed(recs)
    assert any(
        "mostbet" in (r["title"] + r["rationale"] + r["action"]).lower()
        for r in recs
    ), "ожидались рекомендации про бренд mostbet"
    # high блок/takedown присутствует.
    hard = [
        r for r in recs
        if r["priority"] == "high"
        and ("блокир" in r["action"].lower() or "takedown" in r["action"].lower())
    ]
    assert hard, "для нелицензированного бренда ожидался high блок/takedown"
    # vavada-посты не протекают в срез mostbet.
    assert not any(
        "vavada" in (r["title"] + r["rationale"] + r["action"]).lower()
        for r in recs
    ), "посты другого бренда не должны попадать в срез mostbet"


# --- 4. focus="brand:olimpbet" (лицензированный) -> ad-compliance, medium ------
def test_focus_brand_licensed_compliance_not_block(conn):
    for i in range(4):
        pid = f"ob{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 80, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Olimpbet", "normalized": "olimpbet"},
        ])
    conn.commit()

    recs = build_recommendations(conn, focus="brand:olimpbet")
    _assert_well_formed(recs)
    assert any(
        "olimpbet" in (r["title"] + r["rationale"] + r["action"]).lower()
        for r in recs
    ), "ожидались рекомендации про бренд olimpbet"
    for r in recs:
        a = r["action"].lower()
        assert "takedown" not in a, "лицензированный оператор — без takedown"
        assert "блокировку платёжных каналов" not in a, \
            "лицензированный оператор — без блокировки платёжных каналов"
        assert r["priority"] == "medium", "лицензированный бренд — medium"
    blob = " ".join(
        (r["title"] + r["rationale"] + r["action"]).lower() for r in recs
    )
    assert "рекламн" in blob or "реклам" in blob or "21+" in blob, \
        "ожидался контроль рекламных норм у лицензированного оператора"


# --- 5. focus="platform:tiktok" -> про tiktok с цифрами -----------------------
def test_focus_platform_tiktok_with_numbers(conn):
    for i in range(4):
        pid = f"tt{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 90, "gambling", "escalate")
    # telegram-пост (другая площадка) — его специфичные рекомендации не должны течь.
    _seed_post(conn, "tg1", "telegram")
    _seed_score(conn, "tg1", 95, "pyramid", "escalate")
    conn.commit()

    recs = build_recommendations(conn, focus="platform:tiktok")
    _assert_well_formed(recs)
    assert all(
        "tiktok" in (r["title"] + r["rationale"] + r["action"]).lower()
        for r in recs
    ), "каждая рекомендация среза должна упоминать tiktok"
    # цитирует числа (есть escalate-доля).
    assert any(
        "escalate" in r["evidence"].lower() or "%" in r["evidence"]
        for r in recs
    ), "ожидались цифры (escalate/проценты) по площадке"
    # специфика telegram не должна протекать.
    assert not any(
        "telegram" in (r["title"] + r["rationale"] + r["action"]).lower()
        for r in recs
    ), "рекомендации, специфичные для telegram, не должны попадать в срез tiktok"


# --- 6. focus="brand:nonexistent" (нет данных) -> >=1 честный rec --------------
def test_focus_brand_no_data_honest_rec(conn):
    _seed_post(conn, "p1", "tiktok")
    _seed_score(conn, "p1", 90, "gambling", "escalate")
    _seed_extracted(conn, "p1", [
        {"type": "betting_brand", "value": "1xBet", "normalized": "1xbet"},
    ])
    conn.commit()

    recs = build_recommendations(conn, focus="brand:nonexistent")
    _assert_well_formed(recs)
    blob = " ".join(
        (r["title"] + r["rationale"] + r["action"]).lower() for r in recs
    )
    assert "нет данных" in blob or "монитор" in blob or "расширить" in blob, \
        "по пустому срезу ожидался честный rec «нет данных»/расширить мониторинг"


# --- 7. focus=None идентичен глобальной выдаче --------------------------------
def test_focus_none_equals_global(conn):
    _seed_mixed_pyramid_and_gambling(conn)
    global_recs = build_recommendations(conn)
    none_recs = build_recommendations(conn, focus=None)
    assert [r["title"] for r in global_recs] == [r["title"] for r in none_recs]


# --- 8. мусорный focus -> откат к глобальной выдаче ---------------------------
@pytest.mark.parametrize("bad", ["garbage", "", "weirdkind:x", "  ", ":x", "category:"])
def test_garbage_focus_falls_back_to_global(conn, bad):
    _seed_mixed_pyramid_and_gambling(conn)
    recs = build_recommendations(conn, focus=bad)
    _assert_well_formed(recs)
    # совпадает с глобальной выдачей (мусор/неизвестный kind -> глобально).
    global_recs = build_recommendations(conn)
    assert [r["title"] for r in recs] == [r["title"] for r in global_recs]


# --- 9. available_sources(conn) на засеянных данных ---------------------------
def test_available_sources_shape_and_flags(conn):
    for i in range(4):
        pid = f"u{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    for i in range(2):
        pid = f"l{i}"
        _seed_post(conn, pid, "telegram")
        _seed_score(conn, pid, 80, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Olimpbet", "normalized": "olimpbet"},
        ])
    for i in range(3):
        pid = f"py{i}"
        _seed_post(conn, pid, "instagram")
        _seed_score(conn, pid, 85, "pyramid", "escalate")
    conn.commit()

    src = available_sources(conn)
    assert set(src.keys()) == {"categories", "brands", "platforms"}
    assert isinstance(src["categories"], list)
    assert isinstance(src["brands"], list)
    assert isinstance(src["platforms"], list)

    # бренды непусты, с count и булевым licensed.
    assert src["brands"], "ожидались бренды на засеянных данных"
    for b in src["brands"]:
        assert {"id", "label", "count", "licensed"}.issubset(b.keys())
        assert isinstance(b["licensed"], bool)
        assert isinstance(b["count"], int)
    by_id = {b["id"]: b for b in src["brands"]}
    assert by_id["olimpbet"]["licensed"] is True
    assert by_id["mostbet"]["licensed"] is False

    # категории — только угрозы (нет clean), с count.
    cat_ids = {c["id"] for c in src["categories"]}
    assert "clean" not in cat_ids
    assert cat_ids == {"gambling", "pyramid", "fraud"}
    for c in src["categories"]:
        assert {"id", "label", "count"}.issubset(c.keys())

    # площадки с count, отсортированы по count убыв.
    assert src["platforms"], "ожидались площадки"
    counts = [p["count"] for p in src["platforms"]]
    assert counts == sorted(counts, reverse=True)


# --- 10. available_sources() на пустой БД (conn=None путь) --------------------
def test_available_sources_empty_db_safe(conn):
    # conn=None -> своё соединение через config.DB_PATH (фикстура его перенаправила).
    src = available_sources()
    assert set(src.keys()) == {"categories", "brands", "platforms"}
    assert isinstance(src["categories"], list)
    assert isinstance(src["brands"], list)
    assert isinstance(src["platforms"], list)
    # пустая БД -> бренды/площадки пусты; категории присутствуют с нулевыми count.
    assert src["brands"] == []
    assert src["platforms"] == []
    for c in src["categories"]:
        assert c["count"] == 0
