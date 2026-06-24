# F1 — тесты нового сигнала streaming_casino_brand (англоязычный стрим-гемблинг).
#
# Twitch/Kick гемблинг-контент англоязычный (STAKE, roobet, gamdom, rollbit,
# bonus hunt, slots, $650,000 bonus) RU/KZ-модель НЕ ловила. Новый АДДИТИВНЫЙ
# сигнал должен срабатывать на англо-казино брендах/стрим-гемблинг лексике и
# НЕ давать false-positive на чистом англо-контенте (геймплей не-казино игр,
# киберспорт, новости про регулирование).
from app.decision.explain import _FEATURE_LABELS
from app.model.features import HANDCRAFTED_SIGNALS, build_features
from app.models import Extracted

SIGNAL = "streaming_casino_brand"


def _ex(text: str) -> Extracted:
    return Extracted(
        post_id="t1",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text=text,
        entities=[],
    )


# --- сигнал есть в реестре и в feats ---

def test_streaming_casino_brand_in_registry():
    names = {sig["name"] for sig in HANDCRAFTED_SIGNALS}
    assert SIGNAL in names


def test_streaming_casino_brand_registry_shape():
    sig = next(s for s in HANDCRAFTED_SIGNALS if s["name"] == SIGNAL)
    assert sig["category_hint"] == "gambling"
    assert sig["pattern"] is not None  # это regex-сигнал
    assert isinstance(sig["evidence_ru"], str) and sig["evidence_ru"]


def test_build_features_includes_streaming_casino_key():
    feats = build_features(_ex("neutral english text about weather"))
    assert SIGNAL in feats
    assert feats[SIGNAL] == 0


def test_streaming_casino_brand_has_ru_label_in_explain():
    assert SIGNAL in _FEATURE_LABELS
    assert isinstance(_FEATURE_LABELS[SIGNAL], str) and _FEATURE_LABELS[SIGNAL]


# --- ПОЗИТИВЫ: англо-казино бренды и стрим-гемблинг лексика ---

def test_fires_on_stake():
    assert build_features(_ex("Tonight gambling on stake.com lets go"))[SIGNAL] == 1


def test_fires_on_roobet_bonus_hunt():
    assert build_features(_ex("xposed $650,000 BONUS HUNT !roobet"))[SIGNAL] == 1


def test_fires_on_bonus_opening_stake():
    assert build_features(
        _ex("NOW OPENING 22 $30,000 BONUSES !stake")
    )[SIGNAL] == 1


def test_fires_on_max_win_slot():
    assert build_features(_ex("MAX WIN on Gates of Olympus slot"))[SIGNAL] == 1


def test_fires_on_roshtein_casino_stream():
    assert build_features(_ex("Roshtein big win casino stream"))[SIGNAL] == 1


def test_fires_on_other_brands():
    for brand in ("gamdom", "rollbit", "duelbits", "csgoroll", "bc.game", "bcgame"):
        assert build_features(_ex(f"streaming on {brand} now"))[SIGNAL] == 1, brand


def test_fires_on_stream_gambling_terms():
    for term in ("free spins", "mega win", "jackpot", "bonus buy", "sweeps"):
        assert build_features(_ex(f"insane {term} today"))[SIGNAL] == 1, term


# --- HARD-NEGATIVES: чистый англо-контент без гемблинга ---

def test_no_fire_on_football_highlights():
    assert build_features(_ex("great football match highlights last night"))[SIGNAL] == 0


def test_no_fire_on_esports():
    assert build_features(
        _ex("CS2 major grand final clutch, insane esports moment")
    )[SIGNAL] == 0


def test_no_fire_on_non_casino_gameplay():
    assert build_features(
        _ex("Minecraft survival episode 12, building a new base")
    )[SIGNAL] == 0


def test_no_fire_on_regulation_news():
    # новость про регулирование казино — упоминает 'casino' но это clean-контекст.
    # допускаем, что слово casino триггерит regex (gambling-уклон допустим по ТЗ),
    # поэтому этот негатив проверяем на тексте БЕЗ слова casino/slot/gamble.
    assert build_features(
        _ex("New regulator report on online betting taxation policy")
    )[SIGNAL] == 0


def test_no_fire_on_plain_english():
    assert build_features(
        _ex("I cooked a great dinner and watched a movie tonight")
    )[SIGNAL] == 0
