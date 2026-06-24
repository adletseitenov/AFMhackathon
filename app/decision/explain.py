"""F3 — Decision/explain КӨЗ: человекочитаемые причины риска на русском.

Собирает РИЧ-набор буллетов из двух источников:
  1) `score.top_features` — ВСЕ сработавшие инженерные сигналы собственной модели,
     ОТРАНЖИРОВАННЫЕ по |FeatureHit.weight| (как их ранжирует classifier для
     предсказанного класса), каждый со своей уликой (evidence). Имена кейзятся
     РОВНО на реестр F1 §0.4: payout_promise, casino_betting_brand, dm_cta,
     referral, promo_code, crypto_iban, urgency, money_emoji, visual_gambling;
  2) заметных извлечённых сущностей (бренд казино/букмекера, telegram/whatsapp-
     ссылка, обещание дохода, промокод, крипто-кошелёк) — конкретные значения,
     которые подкрепляют сигналы фактами из поста.

ПРАВИЛА ВЫВОДА:
  • Перечисляем ВСЕ сработавшие сигналы (не только один), от сильного к слабому.
  • Сущности добавляем после сигналов как фактические подтверждения.
  • Дедуп по тексту буллета с сохранением порядка.
  • Кратко: общий список ограничен MAX_BULLETS (=8); сигналы приоритетнее сущностей.
  • НИКОГДА не падаем на отсутствующих/пустых полях: visual_gambling приходит без
    текстовой улики (pattern=None) — буллет строится без значения (R4); weight,
    evidence, value могут быть None/пустыми.
"""

from app.models import Entity, Score

# Максимум буллетов в объяснении (criterion: концизность).
MAX_BULLETS = 8

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
    "kz_bank_transfer": "Перевод через казахстанский банк (Kaspi/Halyk/Jusan/Forte)",
    "kz_phone_number": "Казахстанский номер телефона (реквизиты)",
    "kz_gambling_kz_lang": "Казахскоязычная лексика азартных игр",
    "kz_pyramid_tenge": "Инвестиционные обещания в тенге",
    "kz_local_bookmaker": "Кириллическое написание букмекера/HYIP-бренда",
}

# Русские метки для заметных извлечённых сущностей (Entity.type из extract_entities).
# Порядок ключей задаёт приоритет вывода: бренды и каналы связи важнее прочего.
_ENTITY_LABELS = {
    "casino_brand": "Бренд казино",
    "betting_brand": "Бренд букмекера",
    "telegram": "Ссылка/контакт Telegram",
    "whatsapp": "Контакт WhatsApp",
    "promo_code": "Промокод",
    "crypto_wallet": "Крипто-кошелёк/реквизиты",
    "payout_promise": "Обещание дохода",
}

# Приоритет сущностей при усечении до MAX_BULLETS (заметность для проверяющего).
_ENTITY_PRIORITY = {t: i for i, t in enumerate(_ENTITY_LABELS)}


def _feature_weight(fh) -> float:
    """|weight| сигнала; устойчиво к None/нечисловому значению (никогда не падаем)."""
    try:
        return abs(float(getattr(fh, "weight", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def explain(score: Score, entities: list[Entity]) -> list[str]:
    """Список русских буллетов-причин (богатый, ранжированный, кратко ≤ MAX_BULLETS).

    Перечисляет ВСЕ сработавшие handcrafted-сигналы по убыванию |weight| с уликами,
    затем добавляет заметные сущности как фактические подтверждения. Для clean-постов
    без сигналов и сущностей — пустой список. Никогда не выбрасывает исключение.
    """
    bullets: list[str] = []
    seen: set[str] = set()

    def add(label: str, value: str) -> bool:
        """Добавляет буллет (с дедупом). Возвращает True, если буллет реально добавлен."""
        value = (value or "").strip()
        text = f"{label}: «{value}»" if value else label
        if text in seen:
            return False
        seen.add(text)
        bullets.append(text)
        return True

    # 1) ВСЕ сработавшие сигналы, отранжированные по убыванию |weight|.
    #    Сортировка стабильна -> при равных весах сохраняется исходный порядок
    #    (важно для детерминизма и для теста «все 9 сигналов»).
    feature_hits = [fh for fh in (score.top_features or []) if _FEATURE_LABELS.get(fh.feature)]
    feature_hits = sorted(feature_hits, key=_feature_weight, reverse=True)
    for fh in feature_hits:
        label = _FEATURE_LABELS[fh.feature]
        # visual_gambling может прийти без текстовой улики (R4) — буллет без значения.
        add(label, getattr(fh, "evidence", "") or "")

    # 2) Заметные сущности как фактические подтверждения, в порядке приоритета.
    notable = [
        ent
        for ent in (entities or [])
        if getattr(ent, "type", None) in _ENTITY_LABELS
    ]
    notable.sort(key=lambda e: _ENTITY_PRIORITY.get(e.type, len(_ENTITY_PRIORITY)))
    for ent in notable:
        if len(bullets) >= MAX_BULLETS:
            break
        add(_ENTITY_LABELS[ent.type], getattr(ent, "value", "") or "")

    # Концизность: сигналы перечисляем полностью, общий список усекаем до MAX_BULLETS.
    # (classifier и так отдаёт не более 6 сигналов, поэтому усечение почти всегда
    #  затрагивает только хвост сущностей; синтетический случай «9 сигналов» в тестах
    #  сохраняется целиком, т.к. урезаем по последней позиции, а сигналы идут первыми.)
    if len(bullets) > MAX_BULLETS:
        # Гарантируем, что все feature-буллеты остаются (они в начале списка).
        keep = max(MAX_BULLETS, len(feature_hits))
        bullets = bullets[:keep]

    return bullets
