"""F1 — RiskClassifier КӨЗ: предсказание + объяснимость (TORCH-FREE, поправка A1).

Загружает обученный пайплайн (config.CLF_PATH) и отдаёт объяснимый Score:

    risk        = round(100 * (1 - P(clean)))
    category    = argmax по class_probs
    class_probs = {category: prob} по всем CATEGORIES (нормированы ~1)
    top_features = FeatureHit'ы — активные handcrafted-сигналы поста, взвешенные
                   коэффициентами LogisticRegression для предсказанного класса,
                   с русскими evidence-строками из HANDCRAFTED_SIGNALS.

Не импортирует torch / sentence-transformers.
"""

import joblib
import numpy as np

from app.config import CATEGORIES, CLF_PATH
from app.model.features import HANDCRAFTED_SIGNALS, build_features
from app.model.normalize import normalize_obfuscated
from app.models import Extracted, FeatureHit, Score

# имя признака -> русская строка-доказательство (из реестра).
_EVIDENCE_RU = {sig["name"]: sig["evidence_ru"] for sig in HANDCRAFTED_SIGNALS}


def _combined_text(extracted: Extracted) -> str:
    if extracted.combined_text:
        return extracted.combined_text
    return " ".join(
        [
            extracted.caption or "",
            extracted.transcript or "",
            extracted.ocr_text or "",
        ]
    ).strip()


class RiskClassifier:
    """Обёртка над обученным sklearn-пайплайном с объяснимыми top_features."""

    def __init__(self, pipeline, labels):
        self.pipeline = pipeline
        self.labels = labels
        self.clf = pipeline.named_steps["clf"]
        self.features = pipeline.named_steps["features"]
        # Имена handcrafted-признаков в порядке, который выдал DictVectorizer.
        self._hand_names = self._handcrafted_feature_names()
        # Глобальный offset колонок handcrafted-блока внутри объединённой матрицы.
        self._hand_offset = self._handcrafted_column_offset()

    @classmethod
    def load(cls, path=None):
        # joblib.load десериализует НАШ локально обученный артефакт (config.CLF_PATH),
        # созданный train.py в этом же репозитории — не внешний/недоверенный источник.
        # Это стандартный путь персистентности sklearn-модели.
        bundle = joblib.load(path or CLF_PATH)
        return cls(bundle["pipeline"], bundle["labels"])

    def _handcrafted_feature_names(self):
        """Имена признаков из DictVectorizer внутри ветки 'hand' FeatureUnion."""
        hand_pipe = dict(self.features.transformer_list)["hand"]
        dictvec = hand_pipe.named_steps["dictvec"]
        return list(dictvec.get_feature_names_out())

    def _handcrafted_column_offset(self):
        """Сумма ширин предшествующих веток (word + char) — старт hand-колонок.

        FeatureUnion конкатенирует выходы трансформеров слева направо в порядке
        transformer_list; offset = размерность word-TF-IDF + char-TF-IDF.
        """
        offset = 0
        for name, trans in self.features.transformer_list:
            if name == "hand":
                break
            offset += len(trans.get_feature_names_out())
        return offset

    def _top_features(self, hand: dict, category: str) -> list:
        """Активные handcrafted-сигналы × коэффициент LogReg для предсказанного класса."""
        classes = list(self.clf.classes_)
        if category not in classes:
            return []
        cls_idx = classes.index(category)
        coef_row = self.clf.coef_[cls_idx]
        hits = []
        for j, name in enumerate(self._hand_names):
            if hand.get(name, 0):
                col = self._hand_offset + j
                weight = float(coef_row[col]) if col < coef_row.shape[0] else 0.0
                hits.append(
                    FeatureHit(
                        feature=name,
                        weight=round(weight, 4),
                        evidence=_EVIDENCE_RU.get(name, name),
                    )
                )
        hits.sort(key=lambda h: abs(h.weight), reverse=True)
        return hits[:6]

    def predict(self, extracted: Extracted) -> Score:
        # F3 (анти-обфускация): TF-IDF-ветки потребляют НОРМАЛИЗОВАННЫЙ текст — той
        # же функцией, что и train.py (см. train()._normalize_texts), чтобы train и
        # inference видели одинаковый вход ('1 x b e t' -> '1xbet', 'kаzино' -> 'казино').
        text = normalize_obfuscated(_combined_text(extracted))
        proba = self.pipeline.predict_proba([text])[0]
        classes = list(self.clf.classes_)
        class_probs = {c: float(proba[classes.index(c)]) for c in CATEGORIES}
        category = max(class_probs, key=class_probs.get)
        p_not_clean = 1.0 - class_probs.get("clean", 0.0)
        risk = int(round(100 * p_not_clean))
        risk = max(0, min(100, risk))

        hand = build_features(extracted)
        top = self._top_features(hand, category)
        return Score(
            post_id=extracted.post_id,
            risk=risk,
            category=category,
            class_probs=class_probs,
            top_features=top,
        )
