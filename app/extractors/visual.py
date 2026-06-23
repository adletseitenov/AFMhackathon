"""Визуальные концепты через open-clip zero-shot. Ленивая загрузка (A6).

torch / open_clip НЕ импортируются при импорте модуля — только лениво внутри
_load_model() / visual_concepts(). При отсутствии библиотек / ошибке
visual_concepts() возвращает [] (а НЕ "").

CONCEPT_PROMPTS фиксирован и покрывает обязательные метки (ТЗ):
casino / roulette / betting_slip / cash_flaunt / luxury_car. Они согласованы с
множеством gambling_visual модели F1 (§0.4).
"""

from app.models import VisualConcept

# (label, текстовый промпт) — фиксированный набор концептов
CONCEPT_PROMPTS = [
    ("casino", "a casino interior with slot machines"),
    ("roulette", "a roulette wheel and casino table"),
    ("betting_slip", "a sports betting slip or bookmaker odds screen"),
    ("cash_flaunt", "a person flaunting stacks of cash money"),
    ("luxury_car", "an expensive luxury sports car"),
]
_SCORE_THRESHOLD = 0.30

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


def visual_concepts(frames: "list | None") -> list:
    """Zero-shot концепты по кадрам. Возвращает [] при недоступности модели."""
    if not frames:
        return []
    model, preprocess, tokenizer = _load_model()
    if model is None:
        return []
    try:
        import torch
        from PIL import Image

        text_tokens = tokenizer([p for _label, p in CONCEPT_PROMPTS])
        with torch.no_grad():
            text_features = model.encode_text(text_tokens)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            best: dict = {}
            for frame in frames:
                image = preprocess(Image.open(frame).convert("RGB")).unsqueeze(0)
                img_features = model.encode_image(image)
                img_features /= img_features.norm(dim=-1, keepdim=True)
                probs = (100.0 * img_features @ text_features.T).softmax(dim=-1)[0]
                for (label, _prompt), p in zip(CONCEPT_PROMPTS, probs.tolist()):
                    best[label] = max(best.get(label, 0.0), float(p))
        return [
            VisualConcept(label=label, score=round(score, 3))
            for label, score in best.items()
            if score >= _SCORE_THRESHOLD
        ]
    except Exception:
        return []
