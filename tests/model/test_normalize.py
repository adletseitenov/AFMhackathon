# F3 — тесты анти-обфускации (normalize_obfuscated) + срабатывание сигналов на
# обфусцированном тексте. Покрывает разрядку, гомоглифы, литспик, crash-safety,
# идемпотентность, и интеграцию с build_features.
from app.model.features import build_features
from app.model.normalize import normalize_obfuscated, preview
from app.models import Extracted


def _ex(text: str) -> Extracted:
    return Extracted(
        post_id="n1",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text=text,
        entities=[],
    )


# --- базовая нормализация ---


def test_spaced_letters_collapse_to_casino():
    assert normalize_obfuscated("к а з и н о") == "казино"


def test_dotted_letters_collapse():
    assert normalize_obfuscated("к.а.з.и.н.о") == "казино"


def test_spaced_bookmaker_collapses():
    # '1 x b e t' -> '1xbet' (чисто латинский токен НЕ фолдится в кириллицу)
    assert normalize_obfuscated("1 x b e t") == "1xbet"


def test_homoglyph_mixed_script_folds_to_cyrillic():
    # 'kаzино' (латинские k,z + кириллица) -> 'казино'
    assert normalize_obfuscated("kаzино") == "казино"


def test_pure_latin_brand_not_folded():
    # Чисто латинские бренды НЕ калечатся в кириллицу — их ловят Latin-литералы.
    assert normalize_obfuscated("casino") == "casino"
    assert normalize_obfuscated("Melbet") == "melbet"
    assert normalize_obfuscated("1xBet") == "1xbet"


def test_leetspeak_inside_word():
    # '1xб3т' -> делит 3->е внутри кириллического слова, x в смеш.токене -> х
    assert normalize_obfuscated("1xб3т") == "1хбет"


def test_zero_width_chars_stripped():
    # ZWSP между буквами не должен мешать матчингу.
    assert normalize_obfuscated("каз​ино") == "казино"


def test_lowercases():
    assert normalize_obfuscated("КАЗИНО") == "казино"


# --- crash-safety ---


def test_empty_and_none_safe():
    assert normalize_obfuscated("") == ""
    assert normalize_obfuscated(None) == ""
    assert normalize_obfuscated(123) == ""  # нестрока -> ""


def test_whitespace_only_safe():
    assert normalize_obfuscated("   ") == ""


# --- идемпотентность ---


def test_idempotent_on_attack_strings():
    for s in ["к а з и н о", "kаzино", "1xб3т", "к.а.з.и.н.о", "1 x b e t"]:
        once = normalize_obfuscated(s)
        twice = normalize_obfuscated(once)
        assert once == twice, f"не идемпотентно на {s!r}: {once!r} != {twice!r}"


# --- preview helper ---


def test_preview_truncates_and_safe():
    assert preview(None) == ""
    long = "казино " * 100
    p = preview(long, limit=20)
    assert len(p) <= 20


# --- интеграция с build_features: сигналы фаерятся на обфускации ---


def test_casino_signal_fires_on_spaced_letters():
    feats = build_features(_ex("заходи в к а з и н о сегодня"))
    assert feats["casino_betting_brand"] == 1


def test_bookmaker_signal_fires_on_spaced_1xbet():
    # '1 x b e t' -> '1xbet' матчится casino_betting_brand (бренд букмекера).
    feats = build_features(_ex("реклама 1 x b e t для всех"))
    assert feats["casino_betting_brand"] == 1


def test_bookmaker_signal_fires_on_leetspeak():
    # '1xб3т' -> '1хбет' матчит kz_local_bookmaker (кириллический букмекер).
    feats = build_features(_ex("переходи на 1xб3т бонус"))
    assert feats["kz_local_bookmaker"] == 1


def test_casino_signal_fires_on_homoglyph():
    feats = build_features(_ex("новое kаzино с бонусами"))
    assert feats["casino_betting_brand"] == 1


def test_clean_text_no_false_positive_after_normalize():
    feats = build_features(_ex("Сегодня в Алматы открылась городская библиотека"))
    assert feats["casino_betting_brand"] == 0
    assert feats["kz_local_bookmaker"] == 0
    assert feats["payout_promise"] == 0
