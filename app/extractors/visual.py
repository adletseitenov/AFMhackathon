"""Визуальные концепты через open-clip zero-shot. Ленивая загрузка (A6).

torch / open_clip НЕ импортируются при импорте модуля — только лениво внутри
_load_model() / _frame_prompt_probs(). При отсутствии библиотек / ошибке
visual_concepts() возвращает [] (а НЕ "").

Тюнинг против ложных срабатываний (F2):
  * CONCEPT_PROMPTS расширен дискриминативными gambling-промптами (несколько
    формулировок на одну метку) И нейтральными/контекстными промптами
    (NEUTRAL_LABEL: «человек говорит в камеру», «природный пейзаж», «геймплей
    видеоигры», «новостной эфир», …).
  * По кадру считаем softmax над ВСЕМ набором промптов (gambling + neutral),
    сворачиваем prob к максимуму на метку, затем select_concepts() эмитит
    gambling-метку только если её prob уверенно бьёт лучший нейтральный prob
    с запасом (margin) и выше абсолютного пола. Нейтральное видео -> [].

Обязательные метки (ТЗ, согласованы с GAMBLING_VISUAL модели F1, §0.4):
casino / roulette / betting_slip / cash_flaunt / luxury_car.
"""

from app.models import VisualConcept

# Метка-«корзина» для всех нейтральных/контекстных (negative) промптов.
NEUTRAL_LABEL = "neutral"

# (label, текстовый промпт). Несколько промптов на одну gambling-метку повышают
# дискриминативность zero-shot; они сворачиваются к max-prob на метку. Нейтральные
# промпты делят общую метку NEUTRAL_LABEL и служат «отрицательным» классом softmax.
CONCEPT_PROMPTS = [
    # --- casino ---
    ("casino", "a photo of a casino interior with rows of slot machines"),
    ("casino", "a close-up of a glowing slot machine screen with reels and jackpots"),
    ("casino", "an online casino game interface on a phone screen"),
    # --- roulette ---
    ("roulette", "a spinning roulette wheel on a casino table"),
    ("roulette", "an online roulette wheel with a betting board and chips"),
    # --- betting_slip ---
    ("betting_slip", "a sports betting slip showing odds and stake"),
    ("betting_slip", "a bookmaker mobile app screen with live odds and bets"),
    ("betting_slip", "a football match betting coupon with numeric odds"),
    # --- cash_flaunt ---
    ("cash_flaunt", "a person flaunting and fanning out thick stacks of cash money"),
    ("cash_flaunt", "hands holding a large bundle of banknotes on camera"),
    # --- luxury_car ---
    ("luxury_car", "an expensive luxury sports car flex shown off to the camera"),
    ("luxury_car", "a person posing next to a brand new supercar"),
    # --- NEUTRAL / negative / context (НЕ gambling) ---
    ("neutral", "a person talking to the camera in a plain room"),
    ("neutral", "a calm nature landscape with mountains or trees"),
    ("neutral", "gameplay footage of a regular video game"),
    ("neutral", "a television news broadcast with an anchor"),
    ("neutral", "a selfie of a person in everyday clothes"),
    ("neutral", "a plate of food on a table"),
    ("neutral", "an ordinary city street with cars and buildings"),
    ("neutral", "a screenshot of a text chat or social media feed"),
]

# Множество gambling-меток (всё, что НЕ нейтраль). Совпадает с GAMBLING_VISUAL F1.
_GAMBLING_LABELS = {label for label, _ in CONCEPT_PROMPTS if label != NEUTRAL_LABEL}

# Порог: исторический минимальный prob, ниже которого метку не эмитим вовсе.
_SCORE_THRESHOLD = 0.20
# Запас, на который gambling-prob должен бить лучший нейтральный prob.
_NEUTRAL_MARGIN = 0.12
# Абсолютный пол: уверенный gambling-кадр обычно даёт заметную массу на метке.
_MIN_PROB = 0.25

_MODEL = None
_PREPROCESS = None
_TOKENIZER = None
_LOAD_FAILED = False


def _load_model():
    """Лениво грузит open-clip. При ошибке -> (None, None, None) навсегда."""
    global _MODEL, _PREPROCESS, _TOKENIZER, _LOAD_FAILED
    if _MODEL is not None:
        return _MODEL, _PREPROCESS, _TOKENIZER
    if _LOAD_FAILED:
        return None, None, None
    try:
        import open_clip

        from app import config

        model, _, preprocess = open_clip.create_model_and_transforms(
            config.CLIP_MODEL, pretrained=config.CLIP_PRETRAINED
        )
        model.eval()
        _MODEL, _PREPROCESS, _TOKENIZER = (
            model,
            preprocess,
            open_clip.get_tokenizer(config.CLIP_MODEL),
        )
        return _MODEL, _PREPROCESS, _TOKENIZER
    except Exception:
        _LOAD_FAILED = True
        return None, None, None


def select_concepts(prob_by_label: "dict") -> list:
    """Чистая решающая функция: dict {label -> prob} -> list[VisualConcept].

    `prob_by_label` — агрегированные (max по кадрам и по дублирующим промптам)
    softmax-вероятности на КАЖДУЮ метку, включая NEUTRAL_LABEL.

    Gambling-метку эмитим ТОЛЬКО если выполнены все условия:
      1. её prob >= _MIN_PROB (абсолютный пол против размазанного шума);
      2. её prob >= prob[NEUTRAL_LABEL] + _NEUTRAL_MARGIN (уверенно бьёт нейтраль).
    Нейтрально-доминантное распределение -> []. Метки сортируются по убыванию prob.
    """
    if not prob_by_label:
        return []
    neutral = float(prob_by_label.get(NEUTRAL_LABEL, 0.0))
    out = []
    for label, prob in prob_by_label.items():
        if label == NEUTRAL_LABEL or label not in _GAMBLING_LABELS:
            continue
        p = float(prob)
        if p < _MIN_PROB:
            continue
        if p < neutral + _NEUTRAL_MARGIN:
            continue
        out.append(VisualConcept(label=label, score=round(p, 3)))
    out.sort(key=lambda vc: vc.score, reverse=True)
    return out


def _frame_prompt_probs(model, preprocess, tokenizer, frame) -> "dict":
    """Softmax-вероятности по ВСЕМ промптам для одного кадра, свёрнутые к max на
    метку. Тяжёлый путь (torch/PIL) — это seam, который тесты monkeypatch'ат.
    """
    import torch
    from PIL import Image

    text_tokens = tokenizer([p for _label, p in CONCEPT_PROMPTS])
    with torch.no_grad():
        text_features = model.encode_text(text_tokens)
        text_features /= text_features.norm(dim=-1, keepdim=True)
        image = preprocess(Image.open(frame).convert("RGB")).unsqueeze(0)
        img_features = model.encode_image(image)
        img_features /= img_features.norm(dim=-1, keepdim=True)
        probs = (100.0 * img_features @ text_features.T).softmax(dim=-1)[0]
    per_label: dict = {}
    for (label, _prompt), p in zip(CONCEPT_PROMPTS, probs.tolist()):
        per_label[label] = max(per_label.get(label, 0.0), float(p))
    return per_label


def visual_concepts(frames: "list | None") -> list:
    """Zero-shot концепты по кадрам. Возвращает [] при недоступности модели либо
    если кадры нейтральны (нет уверенных gambling-маркеров)."""
    if not frames:
        return []
    model, preprocess, tokenizer = _load_model()
    if model is None:
        return []
    try:
        # Решение принимается ПОКАДРОВО (чтобы один нейтральный кадр не «раздувал»
        # нейтральный prob и не глушил уверенный gambling-кадр), затем эмитированные
        # концепты объединяются по максимальному score на метку.
        best: dict = {}
        for frame in frames:
            per_label = _frame_prompt_probs(model, preprocess, tokenizer, frame)
            for vc in select_concepts(per_label):
                best[vc.label] = max(best.get(vc.label, 0.0), vc.score)
        out = [VisualConcept(label=label, score=score) for label, score in best.items()]
        out.sort(key=lambda vc: vc.score, reverse=True)
        return out
    except Exception:
        return []
