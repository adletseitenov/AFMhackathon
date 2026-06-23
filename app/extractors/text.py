"""Текстовый экстрактор КӨЗ: нормализация + извлечение сущностей (entities).

Это ЯДРО слоя экстракторов и единственная реализация извлечения telegram/
бренд/промокод/крипто/телефон/payout-сущностей (§0.5: F8 переиспользует, не
переписывает). Полностью на regex + курируемых бренд-списках — никаких тяжёлых
моделей, всегда доступно и детерминированно.
"""

import re

from app.models import Entity

_WS_RE = re.compile(r"\s+")


def normalize(text: "str | None") -> str:
    """Схлопывает пробелы, обрезает края. Сохраняет регистр и кириллицу."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


# --- regex-константы для сущностей ---
_TELEGRAM_RE = re.compile(r"(?:https?://)?t\.me/(\w{3,})", re.IGNORECASE)
_WHATSAPP_RE = re.compile(
    r"(?:https?://)?(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\d{6,15})",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_PROMO_RE = re.compile(
    r"\b(?:промокод|промо|promo|bonus|бонус)[\s:]*([A-Z0-9]{3,12})\b",
    re.IGNORECASE,
)
_CRYPTO_RE = re.compile(
    r"\b(?:bc1[a-z0-9]{20,}|0x[a-fA-F0-9]{40}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|T[A-Za-z0-9]{33})\b"
)
_PHONE_RE = re.compile(
    r"\+?7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
_HANDLE_RE = re.compile(r"(?<![\w/.])@([A-Za-z0-9_]{3,32})")
_PAYOUT_RE = re.compile(
    r"(гарантированн\w*\s+(?:доход|прибыл\w*)|"
    r"(?:доход|прибыл\w*)\s+\d{1,3}\s*%|"
    r"\d{1,3}\s*%\s*(?:в\s+месяц|в\s+день|в\s+неделю|годовых)|"
    r"\d{1,3}\s*%\s*(?:доход\w*|прибыл\w*|гарант\w*)|"
    r"гарант\w*\s+\d{1,3}\s*%|"
    r"кепілдік\w*\s+табыс|табыс\s+\d{1,3}\s*%)",
    re.IGNORECASE,
)

# --- курируемые бренд-списки (§ТЗ: 1xBet/Mostbet/Melbet/Pin-Up/1win обязательны) ---
CASINO_BRANDS = [
    "1win", "vavada", "pin-up", "pinup", "joycasino", "casinox", "riobet",
    "playfortuna", "sol casino", "drip casino", "izzi", "champion casino",
    "888 casino", "azino777", "azino", "vulkan", "vulcan", "pin up",
]
BETTING_BRANDS = [
    "1xbet", "1xstavka", "melbet", "olimp", "olimpbet", "parimatch", "betcity",
    "fonbet", "leon", "marathonbet", "winline", "mostbet",
]


def _match_brands(text_lower: str, brands: list, etype: str) -> list:
    """Находит бренды как отдельные слова (без частичных совпадений)."""
    found: list = []
    seen: set = set()
    for brand in brands:
        if brand in seen:
            continue
        # граница слова с учётом дефисов/пробелов в самих брендах
        pattern = r"(?<![\w])" + re.escape(brand) + r"(?![\w])"
        if re.search(pattern, text_lower):
            found.append(Entity(type=etype, value=brand, normalized=brand.lower()))
            seen.add(brand)
    return found


def extract_entities(text: "str | None") -> list:
    """Извлекает все сущности из текста через regex + курируемый бренд-словарь.

    Возвращает list[Entity] для всех значений Entity.type:
    telegram / whatsapp / url / promo_code / crypto_wallet / phone / handle /
    payout_promise / casino_brand / betting_brand.
    """
    text = normalize(text)
    ents: list = []
    if not text:
        return ents

    for m in _TELEGRAM_RE.finditer(text):
        h = m.group(1)
        ents.append(Entity(type="telegram", value=m.group(0), normalized=h.lower()))
    for m in _WHATSAPP_RE.finditer(text):
        ents.append(Entity(type="whatsapp", value=m.group(0), normalized=m.group(1)))
    for m in _URL_RE.finditer(text):
        u = m.group(0)
        low = u.lower()
        if "t.me/" in low or "wa.me/" in low or "api.whatsapp.com" in low:
            continue
        ents.append(Entity(type="url", value=u, normalized=low.rstrip("/.,)")))
    for m in _PROMO_RE.finditer(text):
        ents.append(
            Entity(type="promo_code", value=m.group(0), normalized=m.group(1).upper())
        )
    for m in _CRYPTO_RE.finditer(text):
        ents.append(
            Entity(type="crypto_wallet", value=m.group(0), normalized=m.group(0))
        )
    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        ents.append(Entity(type="phone", value=m.group(0), normalized=digits))
    for m in _HANDLE_RE.finditer(text):
        ents.append(
            Entity(type="handle", value=m.group(0), normalized=m.group(1).lower())
        )
    for m in _PAYOUT_RE.finditer(text):
        val = m.group(0).strip()
        ents.append(Entity(type="payout_promise", value=val, normalized=val.lower()))

    text_lower = text.lower()
    ents.extend(_match_brands(text_lower, CASINO_BRANDS, "casino_brand"))
    ents.extend(_match_brands(text_lower, BETTING_BRANDS, "betting_brand"))
    return ents
