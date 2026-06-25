"""Тесты движка рекомендаций (app/analytics/recommend.build_recommendations).

Движок — ЧИСТО rule-based (никаких внешних API/LLM, критерий №2): читает
агрегаты из БД (через app.analytics.trends.aggregate + собственные read-only SQL)
и выдаёт приоритезированные превентивные рекомендации для АФМ на русском.

БД изолируется через monkeypatch config.DB_PATH на временный файл (§0.2); данные
засеваются прямо в таблицы posts/scores/extracted (без вызова модели) — это
unit-тест правил агрегации, а не пайплайна.
"""

import json

import pytest

from app import config, db
from app.analytics.recommend import build_recommendations

# Канонический набор ключей каждой рекомендации (контракт фронта/роута).
_REC_KEYS = {"title", "rationale", "action", "priority", "evidence"}
_PRIORITIES = {"high", "medium", "low"}


# --- фикстуры/хелперы засева ---------------------------------------------------
@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Временная БД с инициализированной схемой; config.DB_PATH перенаправлен."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "recommend_test.db")
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
    """Любой результат: непустой список словарей канонической формы."""
    assert isinstance(recs, list)
    assert len(recs) >= 1
    for r in recs:
        assert isinstance(r, dict)
        assert _REC_KEYS.issubset(r.keys())
        assert r["priority"] in _PRIORITIES
        # строки на русском — как минимум непустые строки.
        assert isinstance(r["title"], str) and r["title"].strip()
        assert isinstance(r["rationale"], str) and r["rationale"].strip()
        assert isinstance(r["action"], str) and r["action"].strip()
        assert isinstance(r["evidence"], (str, list))


# --- Задача 3a: пустая БД -> дефолтная рекомендация без падения ----------------
def test_empty_db_returns_default_recommendation(conn):
    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    # на пустых данных движок не падает и возвращает хотя бы базовую рекомендацию
    # (например, про сбор данных/расширение мониторинга).
    assert any("монитор" in r["action"].lower() or "данны" in r["action"].lower()
               or "монитор" in r["title"].lower() or "данны" in r["title"].lower()
               for r in recs)


def test_no_conn_arg_opens_own_connection(conn):
    # conn=None -> build_recommendations сам открывает соединение через config.DB_PATH.
    recs = build_recommendations()
    _assert_well_formed(recs)


# --- Задача 3b: общая форма на «боевых» данных --------------------------------
def test_returns_prioritized_list_with_expected_structure(conn):
    # разнообразные данные, чтобы сработало несколько правил
    _seed_post(conn, "p1", "tiktok")
    _seed_post(conn, "p2", "tiktok")
    _seed_post(conn, "p3", "instagram")
    _seed_post(conn, "p4", "telegram")
    _seed_score(conn, "p1", 95, "gambling", "escalate")
    _seed_score(conn, "p2", 88, "gambling", "escalate")
    _seed_score(conn, "p3", 50, "pyramid", "review")
    _seed_score(conn, "p4", 75, "fraud", "escalate")
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    # высокий приоритет должен идти раньше низкого (сортировка по приоритету).
    rank = {"high": 0, "medium": 1, "low": 2}
    ranks = [rank[r["priority"]] for r in recs]
    assert ranks == sorted(ranks)


# --- Задача 3c: правило доминирующего бренда -> high про этот бренд ------------
def test_dominant_brand_rule_fires_high_with_brand_name(conn):
    # один бренд встречается в большом числе постов -> high про блокировку каналов
    brand = "mostbet"
    for i in range(6):
        pid = f"g{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 90, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": brand},
        ])
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    brand_recs = [
        r for r in recs
        if brand in (r["title"] + r["rationale"] + r["action"]).lower()
        and r["priority"] == "high"
    ]
    assert brand_recs, "ожидалась high-рекомендация по доминирующему бренду"
    rec = brand_recs[0]
    # action — из меню действий АФМ: блокировка платёжных каналов / takedown
    a = rec["action"].lower()
    assert "блокир" in a or "takedown" in a or "удал" in a


# --- Задача 3d: всплеск пирамид -> предупреждение + уведомление НБ РК ----------
def test_pyramid_surge_rule_fires(conn):
    for i in range(5):
        pid = f"py{i}"
        _seed_post(conn, pid, "instagram")
        _seed_score(conn, pid, 80, "pyramid", "escalate")
    # немного шума другой категории, чтобы доля пирамид была высокой, но не 100%.
    _seed_post(conn, "c1", "youtube")
    _seed_score(conn, "c1", 5, "clean", "auto_clear")
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    pyramid_recs = [
        r for r in recs
        if "пирамид" in (r["title"] + r["rationale"] + r["action"]).lower()
    ]
    assert pyramid_recs, "ожидалась рекомендация про всплеск финпирамид"


# --- Задача 3e: лидер по эскалациям среди площадок ----------------------------
def test_platform_escalation_leader_rule_fires(conn):
    # telegram целиком в эскалации -> рекомендация усилить мониторинг этой площадки
    for i in range(4):
        pid = f"t{i}"
        _seed_post(conn, pid, "telegram")
        _seed_score(conn, pid, 92, "gambling", "escalate")
    _seed_post(conn, "y1", "youtube")
    _seed_score(conn, "y1", 10, "clean", "auto_clear")
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    plat_recs = [
        r for r in recs
        if "telegram" in (r["title"] + r["rationale"] + r["action"]).lower()
    ]
    assert plat_recs, "ожидалась рекомендация про площадку-лидер эскалаций"


# --- Задача 3f: повторяющиеся реквизиты/промокоды -> единое дело ---------------
def test_repeated_promo_code_network_rule_fires(conn):
    code = "BONUS500"
    for i in range(3):
        pid = f"n{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 85, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "promo_code", "value": code, "normalized": code},
        ])
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    net_recs = [
        r for r in recs
        if "дело" in (r["title"] + r["action"]).lower()
        or "координир" in (r["rationale"] + r["title"] + r["action"]).lower()
    ]
    assert net_recs, "ожидалась рекомендация про координированную сеть (общие реквизиты)"


# --- Задача 3g: высокая доля high-risk -> приоритизация ручной проверки --------
def test_high_risk_share_rule_fires(conn):
    for i in range(8):
        pid = f"h{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 95, "gambling", "escalate")
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    hr_recs = [
        r for r in recs
        if "ручн" in (r["rationale"] + r["title"] + r["action"]).lower()
        or "эскалац" in (r["title"] + r["action"]).lower()
    ]
    assert hr_recs, "ожидалась рекомендация про высокую долю high-risk в очереди"


# --- Задача (лицензия): доминирующий бренд — ЛИЦЕНЗИРОВАННЫЙ оператор -----------
def test_dominant_brand_licensed_operator_recommends_compliance_not_block(conn):
    """Если топ-бренд — лицензированный в РК букмекер (Olimpbet), рекомендация
    НЕ про блокировку/takedown, а про проверку рекламных норм (priority=medium)."""
    brand = "Olimpbet"
    for i in range(6):
        pid = f"ob{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 88, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Olimpbet", "normalized": brand},
        ])
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)

    # Рекомендация про этот бренд должна существовать.
    brand_recs = [
        r for r in recs
        if "olimpbet" in (r["title"] + r["rationale"] + r["action"]).lower()
    ]
    assert brand_recs, "ожидалась рекомендация по доминирующему бренду Olimpbet"
    rec = brand_recs[0]

    # Лицензированный оператор: НЕ предписываем блокировку платёжных каналов /
    # takedown — рекомендуем проверку рекламных норм (medium).
    text = (rec["title"] + rec["rationale"] + rec["action"]).lower()
    a = rec["action"].lower()
    assert "блокировку платёжных каналов" not in a, \
        "лицензированный оператор — не предписываем блокировку платёжных каналов"
    assert "takedown" not in a, "лицензированный оператор — без takedown"
    assert rec["priority"] == "medium"
    assert "рекламн" in text or "21+" in text or "реклам" in text, \
        "ожидалась проверка рекламных норм у лицензированного оператора"


def test_dominant_brand_unlicensed_keeps_hard_block_high(conn):
    """Нелицензированный топ-бренд (mostbet) — прежняя жёсткая рекомендация:
    блокировка платёжных каналов / takedown, priority=high."""
    brand = "mostbet"
    for i in range(6):
        pid = f"mb{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": brand},
        ])
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)

    brand_recs = [
        r for r in recs
        if brand in (r["title"] + r["rationale"] + r["action"]).lower()
        and r["priority"] == "high"
    ]
    assert brand_recs, "ожидалась high-рекомендация по нелицензированному бренду"
    a = brand_recs[0]["action"].lower()
    assert "блокир" in a or "takedown" in a, \
        "для нелицензированного бренда — жёсткая блокировка/takedown"


def test_dominant_brand_unlicensed_1win_keeps_hard_block_high(conn):
    """1win — нелицензированный: остаётся жёсткая рекомендация про блокировку."""
    brand = "1win"
    for i in range(5):
        pid = f"w{i}"
        _seed_post(conn, pid, "instagram")
        _seed_score(conn, pid, 90, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "1Win", "normalized": brand},
        ])
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    brand_recs = [
        r for r in recs
        if brand in (r["title"] + r["rationale"] + r["action"]).lower()
        and r["priority"] == "high"
    ]
    assert brand_recs, "ожидалась high-рекомендация про блокировку 1win"
    a = brand_recs[0]["action"].lower()
    assert "блокир" in a or "takedown" in a


def test_mixed_licensed_and_unlicensed_overview_recommendation(conn):
    """Если среди top_brands есть и лицензированные, и нелицензированные —
    появляется обзорная рекомендация про разделение легальных и нелегальных."""
    # нелицензированные (доминируют), плюс лицензированный
    for i in range(6):
        pid = f"u{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    for i in range(3):
        pid = f"l{i}"
        _seed_post(conn, pid, "telegram")
        _seed_score(conn, pid, 80, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Olimpbet", "normalized": "Olimpbet"},
        ])
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    overview = [r for r in recs if "разделение" in r["title"].lower()]
    assert overview, "ожидалась обзорная рекомендация про разделение операторов"
    rat = overview[0]["rationale"].lower()
    assert "лиценз" in rat, "rationale должен ссылаться на лицензирование"


def test_only_unlicensed_no_split_overview(conn):
    """Если все top_brands нелицензированные — обзорной рекомендации про
    разделение легальных/нелегальных НЕТ (нечего разделять)."""
    for i in range(6):
        pid = f"o{i}"
        _seed_post(conn, pid, "tiktok")
        _seed_score(conn, pid, 92, "gambling", "escalate")
        _seed_extracted(conn, pid, [
            {"type": "betting_brand", "value": "Mostbet", "normalized": "mostbet"},
        ])
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
    overview = [r for r in recs if "разделение" in r["title"].lower()]
    assert not overview, "без лицензированных брендов разделять нечего"


# --- Устойчивость: битый entities_json не роняет движок ------------------------
def test_malformed_entities_do_not_crash(conn):
    _seed_post(conn, "p1", "tiktok")
    _seed_score(conn, "p1", 90, "gambling", "escalate")
    conn.execute(
        "INSERT INTO extracted (post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) "
        "VALUES ('p1', '', '', '', '[]', '', ?)",
        ("not-json",),
    )
    conn.commit()

    recs = build_recommendations(conn)
    _assert_well_formed(recs)
