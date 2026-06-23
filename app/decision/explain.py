"""F3 — Decision/explain КӨЗ: человекочитаемые причины риска на русском.

Собирает буллеты из:
  1) `score.top_features` — активные инженерные сигналы собственной модели
     (имена кейзятся РОВНО на реестр F1 §0.4: payout_promise, casino_betting_brand,
     dm_cta, referral, promo_code, crypto_iban, urgency, money_emoji, visual_gambling);
  2) заметных извлечённых сущностей (бренд казино/букмекера, telegram-ссылка,
     обещание дохода, промокод, крипто-кошелёк).

Дедуп по тексту буллета с сохранением порядка. Не падает на visual_gambling
(pattern=None, evidence может быть пустым — R4).
"""

from app.models import Entity, Score

# Русские метки для инженерных признаков модели (FeatureHit.feature) — §0.4.
_FEATURE_LABELS = {
    "payout_promise": "Обещание гарантированного дохода",
    "casino_betting_brand": "Упоминание казино/букмекера",
    "dm_cta": "Призыв писать в личку (Telegram/WhatsApp)",
    "referral": "Реферальная схема",
    "promo_code": "Промокод",
    "crypto_iban": "Крипто-кошелёк/реквизиты",
    "urgency": "Срочность/давление",
    "money_emoji": "Демонстрация денег",
    "visual_gambling": "Визуальные маркеры азартных игр",
}

# Русские метки для заметных извлечённых сущностей (Entity.type из extract_entities).
_ENTITY_LABELS = {
    "casino_brand": "Упоминание казино",
    "betting_brand": "Упоминание букмекера",
    "telegram": "Призыв в Telegram",
    "whatsapp": "Призыв в WhatsApp",
    "promo_code": "Промокод",
    "crypto_wallet": "Крипто-кошелёк/реквизиты",
    "payout_promise": "Обещание дохода",
}


def explain(score: Score, entities: list[Entity]) -> list[str]:
    """Список русских буллетов-причин. Для clean-постов без сигналов — пустой список."""
    bullets: list[str] = []
    seen: set[str] = set()

    def add(label: str, value: str) -> None:
        text = f"{label}: «{value}»" if value else label
        if text not in seen:
            seen.add(text)
            bullets.append(text)

    for fh in score.top_features:
        label = _FEATURE_LABELS.get(fh.feature)
        if label:
            # visual_gambling может прийти без текстовой улики (R4) — буллет без значения.
            add(label, (fh.evidence or "").strip())

    for ent in entities or []:
        label = _ENTITY_LABELS.get(ent.type)
        if label:
            add(label, ent.value)

    return bullets
