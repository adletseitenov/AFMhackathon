"""F3 — Decision/scoring КӨЗ: вызов собственной обученной модели, персист скора, аудит.

Превращает выход локального классификатора (`RiskClassifier.predict`) в финальный
`Score`, выводит `recommended_action` из порогов риска (§0.11, 3 ветки, без
полосы 'monitor'), сохраняет скор в таблицу `scores` и пишет строку аудита.

ВАЖНО: риск-скоринг выполняет ТОЛЬКО наша локально обученная sklearn-модель
(критерий №2), никаких внешних LLM-сервисов. `RiskClassifier` импортируется на
верхнем уровне модуля, чтобы тесты могли монки-патчить `scoring.RiskClassifier`.
"""

from datetime import datetime, timezone

from app import config, db
from app.config import ESCALATE_THRESHOLD, REVIEW_THRESHOLD
from app.model.classifier import RiskClassifier
from app.models import Extracted, Post, Score

# Модуль-кэш классификатора: тяжёлый артефакт грузится один раз на процесс.
_clf_cache: "RiskClassifier | None" = None


def recommend_action(risk: int) -> str:
    """Единый 3-уровневый маппинг риска в рекомендацию (§0.11).

    risk < REVIEW_THRESHOLD            -> "auto_clear"
    REVIEW_THRESHOLD <= risk < ESCALATE_THRESHOLD -> "review"
    risk >= ESCALATE_THRESHOLD         -> "escalate"

    Идентичен `config.action_for_risk` — двух разных маппингов в коде нет.
    """
    if risk >= ESCALATE_THRESHOLD:
        return "escalate"
    if risk >= REVIEW_THRESHOLD:
        return "review"
    return "auto_clear"


def _get_classifier() -> RiskClassifier:
    """Лениво загрузить и закэшировать собственный обученный классификатор."""
    global _clf_cache
    if _clf_cache is None:
        _clf_cache = RiskClassifier.load()
    return _clf_cache


def score_post(post: Post, extracted: Extracted,
               conn=None) -> Score:
    """Скорит пост собственной моделью, персистит скор + рекомендацию + аудит.

    Если `conn` не передан — открывает своё соединение по `config.DB_PATH`
    и закрывает его в `finally` (sqlite3 `with` НЕ закрывает соединение).
    Возвращает вычисленный `Score`.
    """
    clf = _get_classifier()
    score = clf.predict(extracted)
    action = recommend_action(score.risk)
    scored_at = datetime.now(timezone.utc).isoformat()

    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    try:
        db.upsert_score(conn, score, action, scored_at)
        db.add_audit(
            conn,
            ts=scored_at,
            post_id=post.id,
            action="scored",
            actor="system",
            detail=f"risk={score.risk} category={score.category} action={action}",
        )
    finally:
        if own_conn:
            conn.close()
    return score
