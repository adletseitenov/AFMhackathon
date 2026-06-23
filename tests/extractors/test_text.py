"""F2 core tests: normalize + extract_entities (regex + brands). No heavy libs."""

import sys

from app.extractors.text import extract_entities, normalize
from app.models import Entity


def _types(ents):
    return {e.type for e in ents}


# --- normalize ---
def test_normalize_collapses_whitespace_and_strips():
    assert normalize("  Привет\n\n  мир\t! ") == "Привет мир !"


def test_normalize_keeps_case_cyrillic_digits_percent():
    assert normalize("Доход 30% в МЕСЯЦ") == "Доход 30% в МЕСЯЦ"


def test_normalize_handles_none_and_empty():
    assert normalize(None) == ""
    assert normalize("") == ""


# --- required combo: telegram + payout_promise + casino brand in a RU string ---
def test_finds_telegram_payout_and_casino_brand_in_ru_string():
    text = (
        "Гарантированный доход 30% в месяц в казино 1win! "
        "Пиши в личку https://t.me/casino_win_bot"
    )
    ents = extract_entities(text)
    assert ents and isinstance(ents[0], Entity)
    t = _types(ents)
    assert "telegram" in t
    assert "payout_promise" in t
    assert "casino_brand" in t
    casino = {e.normalized for e in ents if e.type == "casino_brand"}
    assert "1win" in casino


def test_finds_url_promo_phone_crypto_whatsapp_handle():
    text = (
        "Промокод BONUS500 на сайте http://1xstavka.ru "
        "звони +7 701 234 56 78, wa.me/77012345678 "
        "кошелёк bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq пиши @big_money"
    )
    t = _types(extract_entities(text))
    assert {"url", "promo_code", "phone", "whatsapp", "crypto_wallet", "handle"} <= t


def test_telegram_normalized_to_handle_lowercase():
    ents = extract_entities("заходи https://t.me/Casino_Win_Bot")
    tg = [e for e in ents if e.type == "telegram"][0]
    assert tg.normalized == "casino_win_bot"


def test_payout_promise_kazakh_and_percent_variants():
    assert "payout_promise" in _types(extract_entities("кепілдік табыс әр айда"))
    assert "payout_promise" in _types(extract_entities("прибыль 50% в день"))
    assert "payout_promise" in _types(extract_entities("гарантированная прибыль"))


# --- brands (required: 1xBet/Mostbet/Melbet/Pin-Up/1win) ---
def test_detects_casino_brands_including_required():
    ents = extract_entities("Заносим в 1WIN, Pin-Up и Vavada сегодня!")
    brands = {e.normalized for e in ents if e.type == "casino_brand"}
    assert "1win" in brands
    assert "vavada" in brands
    assert "pin-up" in brands


def test_detects_betting_brands_including_required():
    ents = extract_entities("Ставки на 1xBet, Mostbet и Melbet")
    brands = {e.normalized for e in ents if e.type == "betting_brand"}
    assert {"1xbet", "mostbet", "melbet"} <= brands


def test_no_false_brand_on_clean_text():
    ents = extract_entities("Сегодня хорошая погода в Алматы")
    assert not [e for e in ents if e.type in ("casino_brand", "betting_brand")]


def test_url_does_not_double_count_telegram_whatsapp():
    ents = extract_entities("https://t.me/foo_bar и wa.me/77001112233")
    urls = [e for e in ents if e.type == "url"]
    assert urls == []  # t.me / wa.me исключаются из url-ветки


def test_text_module_does_not_import_heavy_libs():
    for lib in ("torch", "faster_whisper", "easyocr", "open_clip"):
        assert lib not in sys.modules
