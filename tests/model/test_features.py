# F1 — тесты инженерных признаков (build_features + реестр HANDCRAFTED_SIGNALS).
from app.model.features import HANDCRAFTED_SIGNALS, build_features
from app.models import Extracted, VisualConcept


def _ex(text: str, visual=None) -> Extracted:
    return Extracted(
        post_id="t1",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=visual or [],
        combined_text=text,
        entities=[],
    )


def test_payout_promise_signal_fires():
    feats = build_features(_ex("Гарантированный доход 30% в месяц без риска!"))
    assert feats["payout_promise"] == 1
    assert feats["urgency"] >= 0


def test_casino_brand_signal_fires():
    feats = build_features(_ex("Заходи на 1xBet и Pin-Up, бонус новичкам"))
    assert feats["casino_betting_brand"] == 1


def test_dm_cta_and_promo_signals():
    feats = build_features(_ex("Пиши в личку @manager, промокод BONUS500"))
    assert feats["dm_cta"] == 1
    assert feats["promo_code"] == 1


def test_clean_text_has_no_scam_signals():
    feats = build_features(_ex("Сегодня в Алматы открылась новая библиотека."))
    assert feats["payout_promise"] == 0
    assert feats["casino_betting_brand"] == 0
    assert feats["dm_cta"] == 0


def test_urgency_and_crypto_signals():
    feats = build_features(
        _ex("Срочно! Переведи USDT на кошелёк 0xA1b2C3d4E5f6, верну вдвое")
    )
    assert feats["urgency"] == 1
    assert feats["crypto_iban"] == 1


def test_money_emoji_signal():
    feats = build_features(_ex("Поднял на ставках 💰💰💰"))
    assert feats["money_emoji"] == 1


def test_visual_gambling_signal_from_concepts():
    feats = build_features(
        _ex("обычная подпись", visual=[VisualConcept(label="roulette", score=0.9)])
    )
    assert feats["visual_gambling"] == 1
    no_vis = build_features(
        _ex("обычная подпись", visual=[VisualConcept(label="dog", score=0.9)])
    )
    assert no_vis["visual_gambling"] == 0


def test_registry_shape():
    assert len(HANDCRAFTED_SIGNALS) >= 7
    for sig in HANDCRAFTED_SIGNALS:
        assert set(sig.keys()) == {"name", "pattern", "category_hint", "evidence_ru"}
        assert sig["category_hint"] in {"gambling", "pyramid", "fraud", "clean"}
        assert isinstance(sig["evidence_ru"], str) and sig["evidence_ru"]


def test_visual_gambling_is_registered_signal():
    names = {sig["name"] for sig in HANDCRAFTED_SIGNALS}
    assert "visual_gambling" in names


def test_build_features_returns_every_signal_key():
    feats = build_features(_ex("обычный текст"))
    for sig in HANDCRAFTED_SIGNALS:
        assert sig["name"] in feats
    assert set(feats.keys()) == {sig["name"] for sig in HANDCRAFTED_SIGNALS}
