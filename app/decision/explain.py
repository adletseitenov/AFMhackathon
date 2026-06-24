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

from app.model.normalize import preview as _normalized_preview
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
    "streaming_casino_brand": "Англоязычный казино-бренд/стрим-гемблинг (Stake, Roobet, bonus hunt, slots)",
    "public_figure_impersonation": "Эксплуатация имени публичной фигуры РК (ложный «эндорсмент»)",
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


def explain(score: Score, entities: list[Entity], extracted=None) -> list[str]:
    """Список русских буллетов-причин (богатый, ранжированный, кратко ≤ MAX_BULLETS).

    Перечисляет ВСЕ сработавшие handcrafted-сигналы по убыванию |weight| с уликами,
    затем добавляет заметные сущности как фактические подтверждения. Для clean-постов
    без сигналов и сущностей — пустой список. Никогда не выбрасывает исключение.

    F3 (анти-обфускация): если передан `extracted` (или строка) и в нём обнаружена
    обфускация (разрядка/гомоглифы/литспик отличают нормализованный текст от исходного),
    добавляем короткий буллет с НОРМАЛИЗОВАННЫМ предпросмотром — чтобы аналитик/UI
    видели, что детектор «развернул» «1 x b e t»/«kаzино» обратно в канон. Crash-safe.
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

    # 3) F3: нормализованный предпросмотр (только если ввод был обфусцирован).
    norm_bullet = _normalized_evidence_bullet(extracted)
    if norm_bullet and norm_bullet not in seen:
        bullets.append(norm_bullet)

    return bullets


def _raw_text(extracted) -> str:
    """Достаёт исходный текст из Extracted/строки/None (crash-safe)."""
    if extracted is None:
        return ""
    if isinstance(extracted, str):
        return extracted
    # Extracted dataclass: combined_text или склейка полей.
    try:
        combined = getattr(extracted, "combined_text", None)
        if combined:
            return combined
        parts = [
            getattr(extracted, "caption", "") or "",
            getattr(extracted, "transcript", "") or "",
            getattr(extracted, "ocr_text", "") or "",
        ]
        return " ".join(parts).strip()
    except Exception:
        return ""


def _normalized_evidence_bullet(extracted) -> str:
    """Буллет с нормализованным предпросмотром, ТОЛЬКО если текст был обфусцирован.

    «Обфусцирован» = нормализованная форма заметно отличается от просто-lowercase
    исходника (разрядка/гомоглифы/литспик «развернулись»). Если разницы нет — буллет
    не добавляем, чтобы не шуметь на чистых постах. Никогда не падает.
    """
    try:
        raw = _raw_text(extracted)
        if not raw or not raw.strip():
            return ""
        norm = _normalized_preview(raw)
        if not norm:
            return ""
        # Сравниваем с «наивным» lowercase+схлопывание пробелов исходника: если
        # нормализатор реально что-то изменил (свернул разрядку/гомоглиф/литспик) —
        # это сигнал маскировки, который стоит показать аналитику.
        naive = " ".join(raw.lower().split())
        if norm.replace(" ", "") == naive.replace(" ", ""):
            return ""
        return f"Обнаружена маскировка текста, нормализовано: «{norm}»"
    except Exception:
        return ""
