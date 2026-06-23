# F1 — тесты RiskClassifier.predict() -> Score + объяснимость top_features.
import pytest

from app.config import CATEGORIES, CLF_PATH, DATASET_PATH
from app.models import Extracted, FeatureHit, Score
from app.model.classifier import RiskClassifier


def _ex(text: str) -> Extracted:
    return Extracted(
        post_id="p1",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text=text,
        entities=[],
    )


@pytest.fixture(scope="module")
def clf():
    # Артефакт нужен заранее — обучаем, если его ещё нет (изоляция от порядка тестов).
    if not CLF_PATH.exists():
        from scripts.gen_dataset import main as gen_main

        if not DATASET_PATH.exists():
            gen_main()
        from app.model.train import train

        train()
    return RiskClassifier.load()


def test_predict_returns_valid_score(clf):
    score = clf.predict(
        _ex("Гарантированный доход 30% в месяц, пиши в личку @money_pro")
    )
    assert isinstance(score, Score)
    assert score.post_id == "p1"
    assert 0 <= score.risk <= 100
    assert score.category in CATEGORIES
    assert set(score.class_probs.keys()) == set(CATEGORIES)
    assert abs(sum(score.class_probs.values()) - 1.0) < 1e-3


def test_scam_text_scores_high_and_clean_low(clf):
    scam = clf.predict(
        _ex("Гарантированный доход 50% в месяц! Вложи и забери вдвое, срочно")
    )
    clean = clf.predict(_ex("Сегодня в Алматы открылась новая городская библиотека"))
    assert scam.risk > clean.risk
    assert clean.category == "clean"


def test_top_features_are_explainable_in_russian(clf):
    score = clf.predict(_ex("Промокод BONUS500 на 1xBet, рулетка, заноси 💰"))
    assert len(score.top_features) >= 1
    for hit in score.top_features:
        assert isinstance(hit, FeatureHit)
        assert isinstance(hit.feature, str) and hit.feature
        assert isinstance(hit.weight, float)
        assert isinstance(hit.evidence, str) and hit.evidence
