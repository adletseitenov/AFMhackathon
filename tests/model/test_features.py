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


# --- KZ-specific signals ---

def test_kz_bank_transfer_signal_fires():
    feats = build_features(_ex("Переведи на каспи 50 000, реквизиты скину в личку"))
    assert feats["kz_bank_transfer"] == 1

def test_kz_bank_transfer_signal_kk():
    feats = build_features(_ex("Карточкаға аудар, Kaspi Gold-қа аудар 10 000 тг"))
    assert feats["kz_bank_transfer"] == 1

def test_kz_bank_transfer_clean_negative():
    feats = build_features(_ex("Kaspi Bank предлагает вклады с лицензией АФМ — безопасно"))
    assert feats["kz_bank_transfer"] == 0

def test_kz_phone_number_signal_fires():
    feats = build_features(_ex("Скинь на +7 700 123 45 67 через каспи, жду"))
    assert feats["kz_phone_number"] == 1

def test_kz_phone_number_compact_form():
    feats = build_features(_ex("Пишите на 87012345678, отвечу в вотсап"))
    assert feats["kz_phone_number"] == 1

def test_kz_gambling_kz_lang_fires():
    feats = build_features(_ex("Қазино ойынында ұтыс үлкен! Тегін бонус ал, тіркел қазір"))
    assert feats["kz_gambling_kz_lang"] == 1

def test_kz_gambling_kz_lang_betting():
    feats = build_features(_ex("Ставка жаса, ұтып ал — бонус беріледі әр депозитке"))
    assert feats["kz_gambling_kz_lang"] == 1

def test_kz_gambling_kz_lang_clean_negative():
    feats = build_features(_ex("Спорт жеңісі: Қазақстан командасы чемпионатта жеңіске жетті"))
    assert feats["kz_gambling_kz_lang"] == 0

def test_kz_pyramid_tenge_fires():
    feats = build_features(_ex("Пассивный доход в тенге — вложи 100 000 тг и получай 30% в месяц"))
    assert feats["kz_pyramid_tenge"] == 1

def test_kz_pyramid_tenge_kk():
    feats = build_features(_ex("Айына 200 000 тг табыс! Кепілдік бар"))
    assert feats["kz_pyramid_tenge"] == 1

def test_kz_pyramid_tenge_clean_negative():
    feats = build_features(_ex("Депозит в тенге от 6% годовых — Народный банк, лицензия АФМ"))
    assert feats["kz_pyramid_tenge"] == 0

def test_kz_local_bookmaker_cyrillic_fires():
    feats = build_features(_ex("Регистрируйся на 1хбет — бонус 100% на первый депозит"))
    assert feats["kz_local_bookmaker"] == 1

def test_kz_local_bookmaker_melbet_mosbet():
    feats = build_features(_ex("Мелбет ставка на футбол — коэффициент 3.5, мостбет тоже работает"))
    assert feats["kz_local_bookmaker"] == 1

def test_kz_local_bookmaker_finiko():
    feats = build_features(_ex("Финико платит 30% в месяц — отправь заявку через 1 вин"))
    assert feats["kz_local_bookmaker"] == 1

def test_kz_local_bookmaker_clean_negative():
    feats = build_features(_ex("Олимп 2024: итоги Олимпийских игр в Казахстане"))
    assert feats["kz_local_bookmaker"] == 0

def test_new_kz_signals_in_registry():
    """Все 5 новых KZ-сигналов присутствуют в реестре HANDCRAFTED_SIGNALS."""
    names = {sig["name"] for sig in HANDCRAFTED_SIGNALS}
    for expected in ("kz_bank_transfer", "kz_phone_number", "kz_gambling_kz_lang",
                     "kz_pyramid_tenge", "kz_local_bookmaker"):
        assert expected in names, f"Сигнал {expected} отсутствует в реестре"

def test_build_features_returns_all_kz_signal_keys():
    """build_features возвращает ключи для всех новых KZ-сигналов."""
    feats = build_features(_ex("обычный текст без сигналов"))
    for key in ("kz_bank_transfer", "kz_phone_number", "kz_gambling_kz_lang",
                "kz_pyramid_tenge", "kz_local_bookmaker"):
        assert key in feats, f"Ключ {key} отсутствует в feats"
        assert feats[key] == 0  # на чистом тексте — 0
