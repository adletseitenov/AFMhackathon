"""F1 — Обучение риск-классификатора КӨЗ (TORCH-FREE scikit-learn, поправка A1).

Пайплайн (ОДИН объект, сериализуется в config.CLF_PATH через joblib):

    FeatureUnion(
        word  = TfidfVectorizer(analyzer="word",    ngram_range=(1,2)),
        char  = TfidfVectorizer(analyzer="char_wb",  ngram_range=(3,5)),
        hand  = Pipeline(FunctionTransformer(build_features) -> DictVectorizer),
    ) -> LogisticRegression(max_iter=1000, class_weight="balanced")

Признаки строятся над combined_text поста; handcrafted-сигналы добавляются через
DictVectorizer (см. features.HANDCRAFTED_SIGNALS). Обучается за секунды, полностью
локально, без скачивания HF/torch-моделей.

`train()` загружает dataset.jsonl, делает стратифицированный split, обучает пайплайн,
считает per-class precision/recall/F1 + confusion matrix, сохраняет clf.joblib и
metrics.json. Возвращает dict метрик.

F3 (анти-обфускация) — ВЫБОР НОРМАЛИЗАЦИИ: тексты прогоняются через
`normalize_obfuscated` ОДИН РАЗ при подготовке обучающих данных (для TF-IDF-веток),
а handcrafted-ветка (`_texts_to_signal_dicts` -> build_features) нормализует
ВНУТРИ себя. На inference `RiskClassifier.predict` нормализует combined_text той же
функцией перед `predict_proba`. Так train и inference видят ОДИНАКОВЫЙ вход — это
сохраняет контракт «один сериализованный Pipeline», а ретрейн пересобирает словари
TF-IDF уже по нормализованной поверхности. (Двойная нормализация в hand-ветке
безвредна — normalize_obfuscated идемпотентна.)

ACTIVE-LEARNING HOOK (контракт с verdict-агентом): если существует
`data/analyst_labels.jsonl` (по одному JSON-объекту на строку:
{"text": <str>, "label": <одна из CATEGORIES>}), его строки ДОБАВЛЯЮТСЯ к
обучающим данным перед обучением. Файл отсутствует -> поведение как раньше.
Битые/неполные строки молча пропускаются.
"""

import json

import joblib
import numpy as np
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    confusion_matrix,
    f1_score,
    precision_recall_fscore_support,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import FunctionTransformer

from app.config import CATEGORIES, CLF_PATH, DATASET_PATH, METRICS_PATH
# _texts_to_signal_dicts ДОЛЖЕН импортироваться из features.py (стабильный модуль),
# иначе при запуске train.py как `python -m app.model.train` joblib запишет ссылку
# на __main__._texts_to_signal_dicts и артефакт не загрузится в uvicorn/pytest.
from app.model.features import _texts_to_signal_dicts
from app.model.normalize import normalize_obfuscated


def build_pipeline() -> Pipeline:
    """Собирает торч-free пайплайн (FeatureUnion TF-IDF + handcrafted -> LogReg)."""
    word_tfidf = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 2),
        min_df=1,
        sublinear_tf=True,
        lowercase=True,
    )
    char_tfidf = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=1,
        sublinear_tf=True,
        lowercase=True,
    )
    hand = Pipeline(
        steps=[
            ("to_dicts", FunctionTransformer(_texts_to_signal_dicts, validate=False)),
            ("dictvec", DictVectorizer(sparse=True)),
        ]
    )
    features = FeatureUnion(
        transformer_list=[
            ("word", word_tfidf),
            ("char", char_tfidf),
            ("hand", hand),
        ]
    )
    clf = LogisticRegression(
        max_iter=1000,
        class_weight="balanced",
        C=4.0,
    )
    return Pipeline(steps=[("features", features), ("clf", clf)])


# Контракт active-learning: аналитические метки рядом с dataset.jsonl.
ANALYST_LABELS_PATH = DATASET_PATH.parent / "analyst_labels.jsonl"


def _load_dataset():
    rows = []
    with open(DATASET_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _load_analyst_labels(path=ANALYST_LABELS_PATH):
    """Active-learning hook: дочитывает размеченные аналитиком строки, если файл есть.

    Формат: по одному JSON-объекту на строку {"text": <str>, "label": <CATEGORY>}.
    Файл отсутствует -> []. Битые/неполные/несоответствующие строки молча пропускаем
    (надёжность: один кривой JSON не должен валить обучение модели).
    """
    if not path.exists():
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                text = obj.get("text")
                label = obj.get("label")
                if not isinstance(text, str) or not text.strip():
                    continue
                if label not in CATEGORIES:
                    continue
                rows.append({"text": text, "label": label,
                             "lang": obj.get("lang", "ru")})
    except OSError:
        return []
    return rows


def train():
    """Обучает пайплайн, сохраняет артефакты, возвращает dict метрик."""
    rows = _load_dataset()
    # Active-learning: дочитываем аналитические метки (если файл есть) и дописываем
    # их к обучающим данным ПЕРЕД обучением (контракт с verdict-агентом).
    rows = rows + _load_analyst_labels()
    # F3 (анти-обфускация): нормализуем поверхность текста ОДИН раз для TF-IDF-веток.
    # build_features в hand-ветке тоже нормализует (идемпотентно), inference — тоже.
    texts = [normalize_obfuscated(r["text"]) for r in rows]
    labels = [r["label"] for r in rows]
    X = np.asarray(texts, dtype=object)
    y = np.asarray(labels)

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.25, stratify=y, random_state=42
    )

    pipe = build_pipeline()
    pipe.fit(list(X_tr), y_tr)

    y_pred = pipe.predict(list(X_te))
    macro = float(f1_score(y_te, y_pred, average="macro", labels=CATEGORIES))
    p, r, f1, _ = precision_recall_fscore_support(
        y_te, y_pred, labels=CATEGORIES, zero_division=0
    )
    cm = confusion_matrix(y_te, y_pred, labels=CATEGORIES).tolist()

    per_class = {
        CATEGORIES[i]: {
            "precision": round(float(p[i]), 4),
            "recall": round(float(r[i]), 4),
            "f1": round(float(f1[i]), 4),
        }
        for i in range(len(CATEGORIES))
    }
    metrics = {
        "labels": CATEGORIES,
        "per_class": per_class,
        "confusion_matrix": cm,
        "macro_f1": round(macro, 4),
        "n_train": int(len(y_tr)),
        "n_test": int(len(y_te)),
        "model": "FeatureUnion(tfidf-word(1,2)+tfidf-char_wb(3,5)+handcrafted) -> LogisticRegression",
    }

    CLF_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Сохраняем весь пайплайн + порядок классов (для classifier.py).
    joblib.dump({"pipeline": pipe, "labels": CATEGORIES}, CLF_PATH)
    METRICS_PATH.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


if __name__ == "__main__":
    m = train()
    print(f"macro-F1={m['macro_f1']} | n_train={m['n_train']} n_test={m['n_test']}")
    print("per-class:", json.dumps(m["per_class"], ensure_ascii=False))
