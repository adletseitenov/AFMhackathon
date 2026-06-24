"""F3 — тесты decision/explain: русские причины из top_features + сущностей.

_FEATURE_LABELS кейзится РОВНО на реальные имена сигналов F1 (§0.4):
payout_promise, casino_betting_brand, dm_cta, referral, promo_code,
crypto_iban, urgency, money_emoji, visual_gambling.
"""

from app.decision.explain import explain
from app.models import Entity, FeatureHit, Score


def test_high_risk_yields_at_least_two_russian_bullets():
    score = Score(
        post_id="p1", risk=85, category="gambling",
        class_probs={"gambling": 0.9, "pyramid": 0.04, "fraud": 0.03, "clean": 0.03},
        top_features=[
            FeatureHit(feature="payout_promise", weight=0.6, evidence="30% в месяц"),
            FeatureHit(feature="casino_betting_brand", weight=0.5, evidence="1xBet"),
        ],
    )
    entities = [
        Entity(type="betting_brand", value="1xBet", normalized="1xbet"),
        Entity(type="telegram", value="t.me/scamchat", normalized="scamchat"),
    ]
    bullets = explain(score, entities)
    assert isinstance(bullets, list)
    assert len(bullets) >= 2
    assert all(isinstance(b, str) and b.strip() for b in bullets)
    joined = " ".join(bullets)
    assert "30% в месяц" in joined
    assert "1xBet" in joined
    assert "t.me/scamchat" in joined


def test_clean_yields_no_bullets():
    score = Score(
        post_id="p2", risk=10, category="clean",
        class_probs={"gambling": 0.02, "pyramid": 0.02, "fraud": 0.04, "clean": 0.92},
        top_features=[],
    )
    bullets = explain(score, [])
    assert bullets == []


def test_feature_labels_cover_all_handcrafted_signal_names():
    # Каждый реальный сигнал F1 даёт русский буллет с использованием evidence.
    names = [
        "payout_promise", "casino_betting_brand", "dm_cta", "referral",
        "promo_code", "crypto_iban", "urgency", "money_emoji", "visual_gambling",
    ]
    score = Score(
        post_id="p3", risk=90, category="gambling",
        class_probs={"gambling": 0.9, "pyramid": 0.04, "fraud": 0.03, "clean": 0.03},
        top_features=[
            FeatureHit(feature=n, weight=0.5, evidence=f"улика_{n}") for n in names
        ],
    )
    bullets = explain(score, [])
    assert len(bullets) == len(names)
    joined = " ".join(bullets)
    for n in names:
        assert f"улика_{n}" in joined
    # русские метки присутствуют
    assert "казино" in joined.lower()
    assert "доход" in joined.lower()


def test_visual_gambling_without_evidence_does_not_crash():
    # R4: visual_gambling pattern=None — буллет строится даже без evidence-текста.
    score = Score(
        post_id="p4", risk=80, category="gambling",
        class_probs={"gambling": 0.8, "pyramid": 0.1, "fraud": 0.05, "clean": 0.05},
        top_features=[FeatureHit(feature="visual_gambling", weight=0.5, evidence="")],
    )
    bullets = explain(score, [])
    assert isinstance(bullets, list)
    assert len(bullets) == 1
    assert "азарт" in bullets[0].lower()


def test_notable_entities_telegram_and_payout():
    score = Score(
        post_id="p5", risk=70, category="fraud",
        class_probs={"gambling": 0.1, "pyramid": 0.1, "fraud": 0.7, "clean": 0.1},
        top_features=[],
    )
    entities = [
        Entity(type="telegram", value="https://t.me/win_bot", normalized="win_bot"),
        Entity(type="casino_brand", value="vavada", normalized="vavada"),
        Entity(type="payout_promise", value="доход 40% в месяц", normalized="доход 40% в месяц"),
        Entity(type="phone", value="+7 700 000 00 00", normalized="77000000000"),
    ]
    bullets = explain(score, entities)
    joined = " ".join(bullets)
    assert "win_bot" in joined or "t.me/win_bot" in joined
    assert "vavada" in joined
    assert "доход 40% в месяц" in joined


def test_dedup_preserves_order():
    score = Score(
        post_id="p6", risk=80, category="gambling",
        class_probs={"gambling": 0.8, "pyramid": 0.1, "fraud": 0.05, "clean": 0.05},
        top_features=[
            FeatureHit(feature="casino_betting_brand", weight=0.5, evidence="1xBet"),
        ],
    )
    # сущность betting_brand с тем же значением -> не дублируется
    entities = [Entity(type="betting_brand", value="1xBet", normalized="1xbet")]
    bullets = explain(score, entities)
    assert len(bullets) == len(set(bullets))


# --- F3 enrichment: ранжирование, перечисление ВСЕХ сигналов, концизность, надёжность ---


def test_lists_all_triggered_signals_ranked_by_weight():
    """Перечисляются ВСЕ сработавшие сигналы, отранжированные по убыванию |weight|."""
    score = Score(
        post_id="r1", risk=95, category="fraud",
        class_probs={"gambling": 0.1, "pyramid": 0.1, "fraud": 0.75, "clean": 0.05},
        top_features=[
            FeatureHit(feature="money_emoji", weight=0.3, evidence="💰"),
            FeatureHit(feature="crypto_iban", weight=1.9, evidence="0xDeAdBeEf"),
            FeatureHit(feature="urgency", weight=1.1, evidence="только сегодня"),
        ],
    )
    bullets = explain(score, [])
    # все три сигнала присутствуют
    assert len(bullets) == 3
    # порядок: crypto_iban (1.9) -> urgency (1.1) -> money_emoji (0.3)
    assert bullets[0].startswith("Крипто-кошелёк")
    assert "0xDeAdBeEf" in bullets[0]
    assert bullets[1].startswith("Срочность")
    assert "💰" in bullets[2]


def test_negative_weight_ranks_by_absolute_value():
    """Отрицательный коэффициент LogReg ранжируется по модулю (|weight|)."""
    score = Score(
        post_id="r2", risk=88, category="gambling",
        class_probs={"gambling": 0.8, "pyramid": 0.1, "fraud": 0.05, "clean": 0.05},
        top_features=[
            FeatureHit(feature="promo_code", weight=0.2, evidence="BONUS500"),
            FeatureHit(feature="casino_betting_brand", weight=-2.4, evidence="1xBet"),
        ],
    )
    bullets = explain(score, [])
    # |-2.4| > 0.2 -> бренд казино/букмекера первым
    assert "1xBet" in bullets[0]
    assert "BONUS500" in bullets[1]


def test_signals_plus_entities_combined():
    """Сигналы и заметные сущности объединяются в одном объяснении."""
    score = Score(
        post_id="r3", risk=92, category="fraud",
        class_probs={"gambling": 0.1, "pyramid": 0.1, "fraud": 0.7, "clean": 0.1},
        top_features=[
            FeatureHit(feature="crypto_iban", weight=1.5, evidence="кошелёк"),
            FeatureHit(feature="urgency", weight=0.9, evidence="срочно"),
        ],
    )
    entities = [
        Entity(type="crypto_wallet", value="0xA1b2C3d4E5f6", normalized="0xA1b2C3d4E5f6"),
        Entity(type="telegram", value="https://t.me/win_bot", normalized="win_bot"),
    ]
    bullets = explain(score, entities)
    joined = " ".join(bullets)
    # сигналы идут раньше сущностей
    assert bullets[0].startswith("Крипто-кошелёк")
    assert "0xA1b2C3d4E5f6" in joined  # конкретный кошелёк-сущность
    assert "t.me/win_bot" in joined  # telegram-сущность
    assert len(bullets) == 4


def test_concise_cap_at_eight_bullets():
    """Общий список усечён до MAX_BULLETS=8: много сущностей не раздувают вывод."""
    score = Score(
        post_id="r4", risk=99, category="gambling",
        class_probs={"gambling": 0.9, "pyramid": 0.04, "fraud": 0.03, "clean": 0.03},
        top_features=[
            FeatureHit(feature="casino_betting_brand", weight=2.0, evidence="1xBet"),
            FeatureHit(feature="promo_code", weight=1.0, evidence="BONUS500"),
            FeatureHit(feature="money_emoji", weight=0.5, evidence="🎰"),
        ],
    )
    entities = [
        Entity(type="casino_brand", value=f"brand{i}", normalized=f"brand{i}")
        for i in range(10)
    ]
    bullets = explain(score, entities)
    assert len(bullets) <= 8
    # все 3 сигнала сохранены (сигналы приоритетнее сущностей)
    joined = " ".join(bullets)
    assert "1xBet" in joined and "BONUS500" in joined and "🎰" in joined


def test_does_not_crash_on_none_fields():
    """Надёжность: None в evidence/value/weight не приводит к падению."""
    score = Score(
        post_id="r5", risk=80, category="gambling",
        class_probs={"gambling": 0.8, "pyramid": 0.1, "fraud": 0.05, "clean": 0.05},
        top_features=[
            FeatureHit(feature="visual_gambling", weight=None, evidence=None),
            FeatureHit(feature="money_emoji", weight=0.4, evidence=None),
            FeatureHit(feature="unknown_signal", weight=9.9, evidence="ignored"),
        ],
    )
    entities = [
        Entity(type="telegram", value=None, normalized=None),
        Entity(type="phone", value=None, normalized=None),  # не заметная -> пропуск
    ]
    bullets = explain(score, entities)
    assert isinstance(bullets, list)
    # неизвестный сигнал и незаметная сущность отброшены; известные -> буллеты
    joined = " ".join(bullets).lower()
    assert "ignored" not in joined
    assert "азарт" in joined  # visual_gambling метка без улики не падает
    assert all(isinstance(b, str) and b.strip() for b in bullets)


def test_entity_only_explanation_when_no_features():
    """Если сигналов нет, но есть заметные сущности — объяснение всё равно богатое."""
    score = Score(
        post_id="r6", risk=70, category="fraud",
        class_probs={"gambling": 0.1, "pyramid": 0.1, "fraud": 0.7, "clean": 0.1},
        top_features=[],
    )
    entities = [
        Entity(type="betting_brand", value="mostbet", normalized="mostbet"),
        Entity(type="promo_code", value="промокод WIN777", normalized="WIN777"),
    ]
    bullets = explain(score, entities)
    joined = " ".join(bullets)
    assert "mostbet" in joined
    assert "WIN777" in joined
    assert len(bullets) == 2
