"""Тюнинг CLIP zero-shot визуальных концептов: снижение ложных срабатываний.

Эти тесты НЕ требуют тяжёлых библиотек (torch/open_clip/PIL). Они проверяют:
  (a) чистую решающую функцию select_concepts(label->prob) — нейтральное видео
      даёт [], gambling-видео даёт правильные метки из множества gambling_visual;
  (b) что visual_concepts([]) / visual_concepts(None) == [] и что при недоступной
      модели visual_concepts(...) == [] (мягкая деградация);
  (c) согласованность контракта: CONCEPT_PROMPTS содержит обязательные 5 меток,
      есть нейтральные (negative) промпты, метки эмитятся только из gambling-набора.
"""

import app.extractors.visual as visual
from app.models import VisualConcept

# Обязательные метки, которые модель F1 трактует как gambling_visual (§0.4).
REQUIRED_LABELS = {"casino", "roulette", "betting_slip", "cash_flaunt", "luxury_car"}


# --- (c) контракт промптов -------------------------------------------------
def test_concept_prompts_cover_required_gambling_labels():
    labels = {label for label, _prompt in visual.CONCEPT_PROMPTS}
    assert REQUIRED_LABELS <= labels


def test_has_neutral_negative_prompts():
    # Должны существовать дискриминативные нейтральные/контекстные промпты,
    # помеченные NEUTRAL_LABEL, иначе softmax-сравнение бессмысленно.
    neutral = [
        (label, prompt)
        for label, prompt in visual.CONCEPT_PROMPTS
        if label == visual.NEUTRAL_LABEL
    ]
    assert len(neutral) >= 3


def test_gambling_labels_subset_of_required():
    # Все НЕ-нейтральные метки промптов должны мапиться в gambling_visual набор,
    # иначе модель не подхватит их как визуальные маркеры гемблинга.
    gambling = {
        label for label, _ in visual.CONCEPT_PROMPTS if label != visual.NEUTRAL_LABEL
    }
    assert gambling == REQUIRED_LABELS


# --- (a) чистая решающая функция ------------------------------------------
def test_select_concepts_neutral_dominant_returns_empty():
    # Нейтральный кадр: neutral явно доминирует -> ничего не эмитим.
    probs = {
        "casino": 0.05,
        "roulette": 0.03,
        "betting_slip": 0.02,
        "cash_flaunt": 0.04,
        "luxury_car": 0.06,
        visual.NEUTRAL_LABEL: 0.80,
    }
    assert visual.select_concepts(probs) == []


def test_select_concepts_gambling_dominant_emits_label():
    # Казино явно бьёт нейтраль с запасом -> эмитим casino.
    probs = {
        "casino": 0.72,
        "roulette": 0.05,
        "betting_slip": 0.03,
        "cash_flaunt": 0.02,
        "luxury_car": 0.03,
        visual.NEUTRAL_LABEL: 0.15,
    }
    out = visual.select_concepts(probs)
    assert [vc.label for vc in out] == ["casino"]
    assert isinstance(out[0], VisualConcept)
    assert out[0].score == 0.72


def test_select_concepts_requires_margin_over_neutral():
    # Gambling немного выше нейтрали, но БЕЗ нужного запаса (margin) -> [].
    # casino=0.30, neutral=0.28 — слишком близко, чтобы доверять.
    probs = {
        "casino": 0.30,
        "roulette": 0.10,
        "betting_slip": 0.10,
        "cash_flaunt": 0.10,
        "luxury_car": 0.12,
        visual.NEUTRAL_LABEL: 0.28,
    }
    assert visual.select_concepts(probs) == []


def test_select_concepts_below_floor_returns_empty():
    # Даже если gambling > neutral, но абсолютный prob ниже пола -> не эмитим
    # (шумовое срабатывание на размазанном распределении).
    probs = {
        "casino": 0.18,
        "roulette": 0.16,
        "betting_slip": 0.15,
        "cash_flaunt": 0.15,
        "luxury_car": 0.16,
        visual.NEUTRAL_LABEL: 0.20,
    }
    assert visual.select_concepts(probs) == []


def test_select_concepts_emits_multiple_strong_labels():
    # Два gambling-концепта оба уверенно бьют нейтраль -> эмитим оба,
    # отсортированные по убыванию score.
    probs = {
        "casino": 0.40,
        "roulette": 0.38,
        "betting_slip": 0.02,
        "cash_flaunt": 0.02,
        "luxury_car": 0.03,
        visual.NEUTRAL_LABEL: 0.15,
    }
    out = visual.select_concepts(probs)
    labels = [vc.label for vc in out]
    assert labels == ["casino", "roulette"]
    # Отсортировано по убыванию score.
    assert out[0].score >= out[1].score


def test_select_concepts_ignores_neutral_label_in_output():
    # NEUTRAL_LABEL никогда не попадает в выдачу, даже если он не доминирует.
    probs = {
        "casino": 0.60,
        "roulette": 0.05,
        "betting_slip": 0.05,
        "cash_flaunt": 0.05,
        "luxury_car": 0.05,
        visual.NEUTRAL_LABEL: 0.20,
    }
    out = visual.select_concepts(probs)
    assert visual.NEUTRAL_LABEL not in {vc.label for vc in out}


def test_select_concepts_empty_or_missing_neutral_is_safe():
    # Пустой словарь -> []. Отсутствие нейтрали трактуется как neutral=0
    # (тогда сравнение идёт только с полом/margin против нуля).
    assert visual.select_concepts({}) == []
    out = visual.select_concepts({"casino": 0.9})
    assert [vc.label for vc in out] == ["casino"]


def test_select_concepts_scores_rounded():
    # prob выше пола и с запасом над нейтралью -> эмитится с округлённым score.
    probs = {"casino": 0.523456, visual.NEUTRAL_LABEL: 0.05}
    out = visual.select_concepts(probs)
    assert out[0].score == round(0.523456, 3)


# --- (b) графейсфул-фолбэк visual_concepts ---------------------------------
def test_visual_concepts_empty_input_returns_empty():
    assert visual.visual_concepts([]) == []
    assert visual.visual_concepts(None) == []


def test_visual_concepts_returns_empty_when_model_unavailable(monkeypatch):
    monkeypatch.setattr(visual, "_load_model", lambda: (None, None, None))
    assert visual.visual_concepts(["/nonexistent/frame0.png"]) == []


def test_visual_concepts_returns_list_of_visualconcept_on_success(monkeypatch):
    # Подменяем тяжёлый путь дешёвым фейком, который возвращает вероятности по
    # промптам, и проверяем, что итог проходит через select_concepts.
    monkeypatch.setattr(
        visual, "_load_model", lambda: ("fake_model", "fake_preprocess", "fake_tok")
    )

    # Gambling-доминантный кадр -> должен эмитнуть casino.
    fake_probs = {
        "casino": 0.7,
        "roulette": 0.05,
        "betting_slip": 0.05,
        "cash_flaunt": 0.05,
        "luxury_car": 0.05,
        visual.NEUTRAL_LABEL: 0.10,
    }
    monkeypatch.setattr(
        visual, "_frame_prompt_probs", lambda model, preprocess, tok, frame: fake_probs
    )
    out = visual.visual_concepts(["/fake/frame.png"])
    assert [vc.label for vc in out] == ["casino"]
    assert all(isinstance(vc, VisualConcept) for vc in out)


def test_visual_concepts_neutral_frame_yields_empty(monkeypatch):
    monkeypatch.setattr(
        visual, "_load_model", lambda: ("fake_model", "fake_preprocess", "fake_tok")
    )
    neutral_probs = {
        "casino": 0.03,
        "roulette": 0.02,
        "betting_slip": 0.02,
        "cash_flaunt": 0.03,
        "luxury_car": 0.05,
        visual.NEUTRAL_LABEL: 0.85,
    }
    monkeypatch.setattr(
        visual,
        "_frame_prompt_probs",
        lambda model, preprocess, tok, frame: neutral_probs,
    )
    assert visual.visual_concepts(["/fake/frame.png"]) == []


def test_visual_concepts_aggregates_max_over_frames(monkeypatch):
    monkeypatch.setattr(
        visual, "_load_model", lambda: ("fake_model", "fake_preprocess", "fake_tok")
    )
    # Кадр 1 нейтральный, кадр 2 — явное казино. max-агрегация -> casino эмитится.
    frames_probs = {
        "/f1.png": {
            "casino": 0.05,
            "roulette": 0.02,
            "betting_slip": 0.02,
            "cash_flaunt": 0.02,
            "luxury_car": 0.04,
            visual.NEUTRAL_LABEL: 0.85,
        },
        "/f2.png": {
            "casino": 0.75,
            "roulette": 0.05,
            "betting_slip": 0.05,
            "cash_flaunt": 0.05,
            "luxury_car": 0.05,
            visual.NEUTRAL_LABEL: 0.05,
        },
    }
    monkeypatch.setattr(
        visual,
        "_frame_prompt_probs",
        lambda model, preprocess, tok, frame: frames_probs[frame],
    )
    out = visual.visual_concepts(["/f1.png", "/f2.png"])
    assert "casino" in {vc.label for vc in out}


def test_visual_concepts_swallows_errors_from_frame(monkeypatch):
    # Если расчёт по кадру падает — visual_concepts не должен бросать, а вернуть [].
    monkeypatch.setattr(
        visual, "_load_model", lambda: ("fake_model", "fake_preprocess", "fake_tok")
    )

    def boom(*a, **k):
        raise RuntimeError("frame compute exploded")

    monkeypatch.setattr(visual, "_frame_prompt_probs", boom)
    assert visual.visual_concepts(["/fake/frame.png"]) == []
