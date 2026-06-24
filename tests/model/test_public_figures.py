# F10 — тесты газеттира публичных фигур + сигнала public_figure_impersonation.
from app.model import public_figures as pf
from app.model.features import HANDCRAFTED_SIGNALS, build_features
from app.models import Extracted


def _ex(text: str) -> Extracted:
    return Extracted(
        post_id="f1",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text=text,
        entities=[],
    )


# --- газеттир: структура данных ---


def test_gazetteer_is_nonempty_reasonable_size():
    assert 12 <= len(pf.PUBLIC_FIGURES) <= 30
    for fig in pf.PUBLIC_FIGURES:
        assert fig["canonical"] and isinstance(fig["canonical"], str)
        assert fig["aliases"] and all(isinstance(a, str) for a in fig["aliases"])
        assert "role" in fig


# --- match / matches ---


def test_match_finds_figure_surname():
    found = pf.match("Сулейменов запустил новый инвестпроект для всех")
    assert "Тимур Сулейменов" in found


def test_matches_true_on_endorsement():
    assert pf.matches("Токаев рекомендует вложиться в платформу")


def test_matches_false_on_clean_text():
    assert not pf.matches("Сегодня в Алматы солнечная погода и ясное небо")


def test_match_dedup_by_canonical():
    # имя + должность одной фигуры не должны дублироваться
    found = pf.match("Глава Нацбанка Сулейменов Тимур Сулейменов обещает доход")
    assert found.count("Тимур Сулейменов") == 1


def test_match_on_normalized_obfuscated_text():
    # имя с разрядкой/гомоглифом должно ловиться через нормализацию
    assert pf.matches("т о к а е в советует этот проект")


def test_match_empty_and_none_safe():
    assert pf.match("") == []
    assert pf.match(None) == []
    assert pf.matches(None) is False


# --- сигнал public_figure_impersonation в build_features ---


def test_public_figure_signal_in_registry():
    names = {sig["name"] for sig in HANDCRAFTED_SIGNALS}
    assert "public_figure_impersonation" in names


def test_public_figure_signal_category_hint():
    sig = next(s for s in HANDCRAFTED_SIGNALS if s["name"] == "public_figure_impersonation")
    assert sig["category_hint"] in {"fraud", "pyramid"}
    assert sig["pattern"] is None  # вычисляемый сигнал, как visual_gambling


def test_signal_fires_on_endorsement_post():
    feats = build_features(
        _ex("Токаев рекомендует инвестплатформу — гарантированный доход 50% в месяц!")
    )
    assert feats["public_figure_impersonation"] == 1


def test_signal_silent_on_clean_news_without_figure():
    feats = build_features(_ex("В Алматы открылась новая библиотека для студентов"))
    assert feats["public_figure_impersonation"] == 0


def test_signal_present_as_key_for_every_post():
    feats = build_features(_ex("обычный текст"))
    assert "public_figure_impersonation" in feats
