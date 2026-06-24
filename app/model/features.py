"""F1 — Инженерные признаки риск-классификатора КӨЗ (TORCH-FREE, поправка A1).

Реестр `HANDCRAFTED_SIGNALS` — единственный источник истины для ключей признаков.
`build_features(extracted)` возвращает dict {signal_name: int}, который скармливается
в DictVectorizer (см. train.py) рядом с TF-IDF (word + char_wb) над combined_text.

НЕ импортирует sentence-transformers / torch и НЕ ссылается на EMBEDDING_MODEL —
ядро модели полностью на scikit-learn (см. §0 + binding amendment A1).
"""

import re

from app.model import public_figures
from app.model.normalize import normalize_obfuscated
from app.models import Extracted

# Каждый сигнал: name (ключ признака), pattern (скомпилированный regex, re.I|re.U),
# category_hint (одна из CATEGORIES), evidence_ru (русская строка-доказательство).
HANDCRAFTED_SIGNALS = [
    {
        "name": "payout_promise",
        "pattern": re.compile(
            r"(гарантирован\w*\s+(доход\w*|заработ\w*|прибыл\w*|профит)|доход\w*\s+гарантирован\w*|"
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
        "name": "scam_payment_demand",
        "pattern": re.compile(
            r"(оплат(и|ите)\s+(доставк\w*|комисси\w*|налог\w*|задолженност\w*|штраф\w*|пошлин\w*|страховк\w*)|"
            r"внес(и|ите)\s+(страхов\w*\s+)?(депозит|взнос|залог|платёж|платеж)|"
            r"(назов(и|ите)|сообщ(и|ите)|введите?|продиктуй\w*)\s+(код(\s+из\s+смс)?|данные\s+карт\w*|cvv|пароль|пин[\s-]?код)|"
            r"код\s+из\s+смс|данны\w*\s+(вашей\s+)?карт\w*|"
            r"разблокир\w*\s+(карт\w*|сч[её]т|аккаунт)|"
            r"карт\w*\s+заблокирован\w*|сч[её]т\s+заблокир\w*|"
            r"предоплат\w*|"
            r"подтвердит\w*\s+(данные\s+)?карт\w*)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Требование оплаты/перевода или реквизитов карты (фишинг/скам)",
    },
    {
        "name": "fake_prize_grant",
        "pattern": re.compile(
            r"(вы\s+выиграл\w*|поздравля\w*[,!\s]+(вы|ваш)|"
            r"ваш\s+(номер|аккаунт)\s+(выбран|выиграл)|вы\s+(стали\s+)?победител\w*|"
            r"получите?\s+(приз|грант|компенсаци\w*|кэшбэк|кешбэк|выплат\w*|подарок)\b|"
            r"вам\s+(положен\w*|начислен\w*|причитается)\s+(компенсаци\w*|грант|выплат\w*|приз|\d))",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Фейковый выигрыш/приз/грант как приманка",
    },
    {
        "name": "crypto_scam",
        "pattern": re.compile(
            r"(удво(им|ю|ить|ишь|its)\s+\w{0,12}\s*(деньги|депозит|вклад\w*|биткоин\w*|крипт\w*|сумм\w*|btc|вложени\w*)|"
            r"x[23]\s*(за\s+час|обратно|мгновенно|назад)|"
            r"(крипто[-\s]?)?арбитраж\w*|"
            r"облачн\w*\s+майнинг|"
            r"бот\s+(торгу\w*|удва\w*|зарабат\w*)|"
            r"сигнал\w*\s+на\s+(крипт\w*|монет\w*)|"
            r"закидыва\w*\s+на\s+(нашу\s+)?биржу|"
            r"раздач\w*\s+(usdt|btc|крипт\w*)|аирдроп\w*)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Крипто-схема: удвоение/арбитраж/майнинг/раздача (скам)",
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
            # после анти-обфускации '1 x b e t'/'1xbet' сворачиваются в '1хвет'
            # (b->в гомоглиф), а '1xб3т'->'1хбет'; ловим обе нормализованные формы:
            r"1хвет|1хбет|"
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
    {
        # Англоязычный стрим-гемблинг (Twitch/Kick): крипто-казино бренды и
        # лексика bonus-hunt-стримов. RU/KZ-сигналы это НЕ ловили. Аддитивный
        # regex-сигнал по НОРМАЛИЗОВАННОМУ тексту (build_features проходит его
        # штатно по списку). Границы слов аккуратные, чтобы 'slot' не ловил
        # 'slotted'/'slothful'; в gambling-контексте уклон допустим.
        "name": "streaming_casino_brand",
        "pattern": re.compile(
            r"(stake\.com|\bstake\b(\s+us)?|roobet|gamdom|rollbit|duelbits|"
            r"csgoroll|betclic|\bbc\.?game\b|\broobet\b|"
            r"bonus\s+hunt|bonus\s+buy|bonus\s+opening|"
            r"\bslots?\b|\bcasino\b|\bgamble\b|\bjackpot\b|"
            r"free\s+spins?|max\s+win|big\s+win|mega\s+win|\bsweeps\b)",
            re.I | re.U,
        ),
        "category_hint": "gambling",
        "evidence_ru": "Англоязычный казино-бренд/стрим-гемблинг (Stake, Roobet, bonus hunt, slots)",
    },
    {
        # F10 — импесонация публичной фигуры. pattern=None: вычисляется в build_features
        # газеттиром public_figures.match() по НОРМАЛИЗОВАННОМУ тексту (как visual_gambling
        # берётся из visual_concepts). НЕ переименовывать/не удалять существующие ключи.
        "name": "public_figure_impersonation",
        "pattern": None,
        "category_hint": "fraud",
        "evidence_ru": "Эксплуатация имени известной публичной фигуры РК (ложный «эндорсмент»)",
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


def normalized_text(extracted: Extracted) -> str:
    """Нормализованная (анти-обфускация) КОПИЯ всего текста поста для матчинга.

    Маленький хелпер, чтобы тот же нормализованный текст можно было поднять в
    объяснение/UI (см. app.decision.explain). Не разрушает исходный отображаемый
    текст — это отдельная копия для сопоставления сигналов.
    """
    return normalize_obfuscated(_combined(extracted))


def build_features(extracted: Extracted) -> dict:
    """Бинарные инженерные признаки по всему доступному тексту/визуалу поста.

    Возвращает dict {signal_name: int} c РОВНО одним ключом на каждую запись
    HANDCRAFTED_SIGNALS (включая `visual_gambling` и `public_figure_impersonation`).
    Этот dict скармливается DictVectorizer в составе FeatureUnion (см. train.py).

    F3 (анти-обфускация): regex-сигналы матчатся по НОРМАЛИЗОВАННОМУ тексту
    (normalize_obfuscated), поэтому срабатывают на «1 x b e t», «kаzино», «1xб3т».
    """
    text = normalized_text(extracted)
    feats: dict = {}
    for sig in HANDCRAFTED_SIGNALS:
        name = sig["name"]
        if name == "visual_gambling":
            feats["visual_gambling"] = (
                1
                if any(
                    vc.label in GAMBLING_VISUAL for vc in (extracted.visual_concepts or [])
                )
                else 0
            )
            continue
        if name == "public_figure_impersonation":
            # F10 — газеттир публичных фигур по нормализованному тексту.
            feats["public_figure_impersonation"] = 1 if public_figures.matches(text) else 0
            continue
        feats[name] = 1 if sig["pattern"].search(text) else 0
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
