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
