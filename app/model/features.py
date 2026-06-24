"""F1 — Инженерные признаки риск-классификатора КӨЗ (TORCH-FREE, поправка A1).

Реестр `HANDCRAFTED_SIGNALS` — единственный источник истины для ключей признаков.
`build_features(extracted)` возвращает dict {signal_name: int}, который скармливается
в DictVectorizer (см. train.py) рядом с TF-IDF (word + char_wb) над combined_text.

НЕ импортирует sentence-transformers / torch и НЕ ссылается на EMBEDDING_MODEL —
ядро модели полностью на scikit-learn (см. §0 + binding amendment A1).
"""

import re

from app.models import Extracted

# Каждый сигнал: name (ключ признака), pattern (скомпилированный regex, re.I|re.U),
# category_hint (одна из CATEGORIES), evidence_ru (русская строка-доказательство).
HANDCRAFTED_SIGNALS = [
    {
        "name": "payout_promise",
        "pattern": re.compile(
            r"(гарантирован\w*\s+доход|доход\w*\s+гарантирован\w*|"
            r"\d{2,3}\s*%\s*(в\s*(месяц|день|неделю)|годовых|ай(ына|да))|"
            r"кепілдік\s+табыс|табыс\s+кепілд|айына\s+\d{2,3}\s*%|"
            r"забери\s+(вдвое|втрое|x\d)|вернёт?\s+вдвое|удво(им|ить)\s+(депозит|вклад)|"
            r"доход\s+гарантирован|без\s+риска|пассивн\w*\s+доход)",
            re.I | re.U,
        ),
        "category_hint": "pyramid",
        "evidence_ru": "Обещание гарантированного/фиксированного дохода",
    },
    {
        "name": "casino_betting_brand",
        "pattern": re.compile(
            r"(1xbet|melbet|pin[-\s]?up|mostbet|parimatch|betcity|"
            r"vulkan|joycasino|1win|olimp|казино|казино\w*|рулетк\w*|"
            r"букмекер\w*|ставк[аи]\w*|ставках|депозит\w*\s+удво|слот\w*|"
            r"казиносы\w*|бетон\w*)",
            re.I | re.U,
        ),
        "category_hint": "gambling",
        "evidence_ru": "Упоминание бренда казино/букмекера или ставок",
    },
    {
        "name": "dm_cta",
        "pattern": re.compile(
            r"(пиши\w*\s+в\s+(личк\w*|директ|лс|телеграм|telegram|whatsapp|ватсап)|"
            r"пиши\s+в\s+личку|в\s+лс\b|жми\s+(на\s+)?ссылк\w*|"
            r"переходи\s+по\s+ссылк\w*|ссылк\w*\s+в\s+проф\w*|"
            r"жеке\s+хабарлама|сілтемеге\s+бас|регистрируйся\s+по\s+ссылк\w*)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Призыв написать в личку / перейти по ссылке (CTA в DM)",
    },
    {
        "name": "referral",
        "pattern": re.compile(
            r"(реферал\w*|реф[-\s]?ссылк\w*|реф[-\s]?код\w*|пригласи\s+друг\w*|"
            r"приведи\s+друг\w*|тво[яё]\s+команда\s+зараб\w*|сетевой\s+маркетинг|"
            r"досы\w*\s+шақыр|команда\w*\s+табыс|рефералдық\s+бағдарлама)",
            re.I | re.U,
        ),
        "category_hint": "pyramid",
        "evidence_ru": "Реферальная / сетевая схема вовлечения",
    },
    {
        "name": "promo_code",
        "pattern": re.compile(
            r"(промокод\w*|промо[-\s]?код\w*|бонус[-\s]?код\w*|"
            r"промокод\s+[A-Z0-9]{3,12}\b|кодом?\s+[A-Z0-9]{3,12}\b)",
            re.I | re.U,
        ),
        "category_hint": "gambling",
        "evidence_ru": "Промокод / бонус-код",
    },
    {
        "name": "crypto_iban",
        "pattern": re.compile(
            r"(\b0x[a-fA-F0-9]{6,}\b|\b(bc1|trc20|trx)[a-zA-HJ-NP-Z0-9]{6,}\b|"
            r"\bKZ\d{2}[A-Z0-9]{10,}\b|usdt|tether|trc20|btc[-\s]?кошел\w*|"
            r"крипто[-\s]?кошел\w*|кошел[её]к\w*|әмиян\w*|кошельк\w*)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Крипто-кошелёк / IBAN / реквизиты для перевода",
    },
    {
        "name": "urgency",
        "pattern": re.compile(
            r"(срочно|только\s+сегодня|успей\w*|последн\w*\s+(мест\w*|орынд\w*)|"
            r"количество\s+ограничен\w*|не\s+упусти|пока\s+не\s+поздно|"
            r"тез\s+арада|бүгін\s+ғана|үлгер|за\s+(час|вечер|день))",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Лексика срочности / искусственный дефицит",
    },
    {
        "name": "money_emoji",
        "pattern": re.compile(
            r"[\U0001F4B0\U0001F4B5\U0001F4B4\U0001F4B6\U0001F911\U0001F4B8\U0001F3B0]"
        ),
        "category_hint": "gambling",
        "evidence_ru": "Эмодзи денег / демонстрация богатства",
    },
    {
        "name": "visual_gambling",
        "pattern": None,  # визуальный сигнал — берётся из extracted.visual_concepts, не из текста
        "category_hint": "gambling",
        "evidence_ru": "Визуальные маркеры азартных игр (CLIP)",
    },
    {
        "name": "kz_bank_transfer",
        "pattern": re.compile(
            r"(каспи\s*(gold|голд|перевод|карт\w*)|на\s+карт\w*\s+каспи|переведи\s+на\s+каспи|кинь\s+на\s+каспи|"
            r"kaspi\s*(gold|перевод)|халык\s*(банк)?\s*перевод|на\s+карт\w*\s+халык|"
            r"жусан\s*(перевод|банк|карт\w*)|jusan\s*(перевод|банк)|форте\s*(банк)?\s*(перевод|реквизит|карт\w*)|"
            r"forte\s*банк|карточкаға\s+аудар|kaspi\s*gold[-\s]?қа\s+аудар|каспиге\s+аудар)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Реквизиты перевода через казахстанский банк (Kaspi/Halyk/Jusan/Forte)",
    },
    {
        "name": "kz_phone_number",
        "pattern": re.compile(
            r"(\+7[\s\-]?7\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}|"
            r"87\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}|"
            r"8\s7\d{2}\s\d{3}\s\d{2}\s\d{2})",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Казахстанский номер телефона (+7 7XX / 8 7XX) — вероятные реквизиты",
    },
    {
        "name": "kz_gambling_kz_lang",
        "pattern": re.compile(
            r"(ұтыс\w*|ұтып\s+ал\w*|ұтқан\w*|қазино\w*|"
            r"ставка\s+жаса\w*|ставка\s+қой\w*|"
            r"тегін\s+бонус|бонус\s+ал\w*|бонус\s+беріл\w*|"
            r"ақша\s+таб\w*|жеңіс\s+ал\w*|"
            r"тіркел\w*\s+(бонус|ойын|ставка)|"
            r"депозит\s+жаса\w*|фрибет\w*|тегін\s+ойна)",
            re.I | re.U,
        ),
        "category_hint": "gambling",
        "evidence_ru": "Казахскоязычная лексика азартных игр (ұтыс, қазино, тегін бонус)",
    },
    {
        "name": "kz_pyramid_tenge",
        "pattern": re.compile(
            r"(пассивн\w*\s+доход\s+в\s+тенге|доход\s+в\s+тг|"
            r"айына\s+[\d\s]+тг|в\s+месяц\s+[\d\s]+тг|"
            r"₸\s*[\d\s]{2,10}\s*(доход|табыс)|[\d\s]{2,10}\s*₸\s*(доход|табыс|пассивн\w*)|"
            r"\d[\d\s]*\s*000\s*тг\s*(в\s+месяц|айына|доход|пассивн\w*)|"
            r"тг\s+пассивн\w*|инвестиц\w*\s+(в\s+)?тенге|"
            r"[\d]+\s*%\s*(в\s+месяц|айына)\s*(в\s+тенге|тг|₸))",
            re.I | re.U,
        ),
        "category_hint": "pyramid",
        "evidence_ru": "Пирамидные обещания дохода в тенге (тг / ₸ / тенге)",
    },
    {
        "name": "kz_local_bookmaker",
        "pattern": re.compile(
            r"(1\s*х\s*бет|1хбет|1\s*x\s*ставка|1хставка|"
            r"мел\s*бет|мелбет|мост\s*бет|мостбет|"
            r"пин\s*[-\s]?\s*ап|пинап|"
            r"1\s*вин\b|"
            r"олимп\s*(бет|ставк\w*|казино)|olimpbet|"
            r"бет\s*сити|бетсити|леон\s*бет|леонбет|leon\s*bet|"
            r"финико|finiko|bestway\s*(invest|инвест|проект))",
            re.I | re.U,
        ),
        "category_hint": "gambling",
        "evidence_ru": "Кириллическое написание нелегального букмекера/казино или HYIP-бренда (1хбет, мелбет, Финико)",
    },
]

# Метки визуальных концептов, трактуемых как маркеры гемблинга (§0.4).
GAMBLING_VISUAL = {"casino", "roulette", "betting_slip", "cash_flaunt", "luxury_car"}


def _combined(extracted: Extracted) -> str:
    """Весь доступный текст поста: combined_text или склейка caption/transcript/ocr."""
    if extracted.combined_text:
        return extracted.combined_text
    return " ".join(
        [
            extracted.caption or "",
            extracted.transcript or "",
            extracted.ocr_text or "",
        ]
    ).strip()


def build_features(extracted: Extracted) -> dict:
    """Бинарные инженерные признаки по всему доступному тексту/визуалу поста.

    Возвращает dict {signal_name: int} c РОВНО одним ключом на каждую запись
    HANDCRAFTED_SIGNALS (включая `visual_gambling`). Этот dict скармливается
    DictVectorizer в составе FeatureUnion (см. train.py).
    """
    text = _combined(extracted)
    feats: dict = {}
    for sig in HANDCRAFTED_SIGNALS:
        if sig["name"] == "visual_gambling":
            feats["visual_gambling"] = (
                1
                if any(
                    vc.label in GAMBLING_VISUAL for vc in (extracted.visual_concepts or [])
                )
                else 0
            )
            continue
        feats[sig["name"]] = 1 if sig["pattern"].search(text) else 0
    return feats


def _texts_to_signal_dicts(texts):
    """Каждую строку combined_text -> dict {signal_name: int} через build_features.

    Используется как callable внутри FunctionTransformer в обучающем пайплайне
    (см. train.py). ВАЖНО: функция живёт здесь, в стабильном модуле
    `app.model.features` (а НЕ в train.py-энтрипоинте), иначе joblib запишет ссылку
    на `__main__._texts_to_signal_dicts` и артефакт не загрузится в другом процессе
    (uvicorn/pytest). Оборачиваем строку в минимальный Extracted (визуальных
    концептов в датасете нет — visual_gambling=0; на проде он приходит из
    extracted.visual_concepts).
    """
    dicts = []
    for t in texts:
        ex = Extracted(
            post_id="train",
            caption=t or "",
            transcript="",
            ocr_text="",
            visual_concepts=[],
            combined_text=t or "",
            entities=[],
        )
        dicts.append(build_features(ex))
    return dicts
