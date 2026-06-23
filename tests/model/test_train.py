# F1 — тесты обучения: датасет генерится, модель достигает macro-F1 >= 0.7, артефакты пишутся.
import json
from collections import Counter

import pytest

from app.config import CATEGORIES, CLF_PATH, DATASET_PATH, METRICS_PATH
from scripts.gen_dataset import build_rows
from scripts.gen_dataset import main as gen_main


# --- датасет ---


def test_build_rows_volume_and_schema():
    rows = build_rows()
    assert 800 <= len(rows) <= 1100
    for r in rows:
        assert set(r.keys()) == {"text", "lang", "label"}
        assert r["lang"] in {"ru", "kk"}
        assert r["label"] in {"gambling", "pyramid", "fraud", "clean"}
        assert isinstance(r["text"], str) and len(r["text"].strip()) > 0


def test_all_classes_present_and_hard_negatives():
    rows = build_rows()
    labels = Counter(r["label"] for r in rows)
    for c in ("gambling", "pyramid", "fraud", "clean"):
        assert labels[c] >= 40, f"класс {c} недопредставлен: {labels[c]}"
    clean_texts = " ".join(r["text"] for r in rows if r["label"] == "clean").lower()
    assert "лиценз" in clean_texts or "финансов" in clean_texts


def test_both_languages_present():
    rows = build_rows()
    langs = Counter(r["lang"] for r in rows)
    assert langs["ru"] >= 100
    assert langs["kk"] >= 40


# --- обучение (генерит датасет на диск, обучает, проверяет метрики) ---


@pytest.fixture(scope="module")
def trained_metrics():
    # Гарантируем наличие dataset.jsonl, затем обучаем (импорт train ПОСЛЕ
    # генерации, чтобы не тащить sklearn при сборе датасет-тестов).
    if not DATASET_PATH.exists():
        gen_main()
    from app.model.train import train

    return train()


def test_train_reaches_sanity_macro_f1(trained_metrics):
    # Пол качества: модель должна уверенно различать классы.
    assert (
        trained_metrics["macro_f1"] >= 0.80
    ), f"macro-F1 слишком низкий: {trained_metrics['macro_f1']}"


def test_model_is_credible_not_perfect(trained_metrics):
    """Criterion #2 (свой, независимый): идеальная диагональ (macro_f1=1.0) неправдоподобна.

    Датасет содержит граничные коллизии (общий текст под двумя соседними ярлыками),
    поэтому на held-out ДОЛЖНЫ быть off-diagonal ошибки — это признак реалистичной,
    а не зазубренной модели. Держим разумный потолок и проверяем недиагональность.
    """
    cm = trained_metrics["confusion_matrix"]
    diag = sum(cm[i][i] for i in range(len(cm)))
    total = sum(sum(row) for row in cm)
    off_diagonal = total - diag
    assert off_diagonal > 0, "Матрица ошибок идеально диагональна — модель неправдоподобна"
    assert (
        trained_metrics["macro_f1"] <= 0.99
    ), f"macro-F1 подозрительно идеален: {trained_metrics['macro_f1']}"


def test_train_writes_artifacts_and_metrics_schema(trained_metrics):
    assert CLF_PATH.exists()
    assert METRICS_PATH.exists()
    saved = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    assert set(saved.keys()) >= {
        "per_class",
        "confusion_matrix",
        "macro_f1",
        "n_train",
        "n_test",
        "labels",
    }
    assert saved["labels"] == CATEGORIES
    for c in CATEGORIES:
        assert set(saved["per_class"][c].keys()) == {"precision", "recall", "f1"}
    assert len(saved["confusion_matrix"]) == len(CATEGORIES)
    assert all(len(row) == len(CATEGORIES) for row in saved["confusion_matrix"])
    assert isinstance(saved["n_train"], int) and saved["n_train"] > 0
    assert isinstance(saved["n_test"], int) and saved["n_test"] > 0
