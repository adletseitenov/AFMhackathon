"""Реестр БУКМЕКЕРОВ, ЛИЦЕНЗИРОВАННЫХ в Казахстане (легальные операторы).

ЗАЧЕМ: не всякое упоминание ставок = нарушение. Букмекеры с лицензией РК работают
ЛЕГАЛЬНО (под надзором, расчёты через ЕУЦ — Единый учётный центр ставок). КӨЗ
помечает такие посты флагом «разрешён в РК», чтобы аналитик сразу видел: БЛОКИРОВКА
НЕ ТРЕБУЕТСЯ — проверять нужно соблюдение РЕКЛАМНЫХ норм (возраст 21+, обязательная
пометка «реклама», запрет агрессивных бонусов/гарантий выигрыша), а не сам факт
деятельности оператора. Риск-скор при этом НЕ занижается (контент всё равно
гемблинг) — флаг лишь добавляет контекст для решения человека.

ВАЖНО (честная рамка):
- Это КОНФИГУРИРУЕМЫЙ справочник-стаб. Единственный authoritative-источник —
  ОФИЦИАЛЬНЫЙ реестр лицензий АФМ/регулятора РК. Значения ниже — стартовый набор и
  ТРЕБУЮТ сверки с официальным реестром перед оперативным применением.
- Онлайн-КАЗИНО в РК запрещены полностью; лицензии касаются только
  БУКМЕКЕРОВ/тотализаторов. Поэтому «казино»-бренд в списке быть не может — только
  букмекерская деятельность лицензированного оператора.
- Лицензия покрывает деятельность, НЕ безусловную рекламу: даже у разрешённого
  оператора реклама может нарушать правила (несовершеннолетние, отсутствие
  раскрытия, «гарантированный доход») — это и есть зона проверки.
"""

import json
import re

from app import config
from app.model.normalize import normalize_obfuscated

# canonical (отображаемое имя) -> {"pattern": скомпилированный regex по нормализованному
# тексту, "note": краткая RU-пометка для аналитика}. ВЕРИФИЦИРОВАТЬ по реестру АФМ.
LICENSED_KZ_BOOKMAKERS = {
    "Olimpbet": {
        "pattern": re.compile(r"\bolimp\s?bet\b|\bолимп\s?бет\b|\bolimpbet\b|\bолимпбет\b", re.I | re.U),
        "note": "Букмекер с лицензией РК (легальная деятельность).",
    },
    "PARI": {
        # «PARI» (ребренд Parimatch в ряде юрисдикций). Сверять статус по реестру.
        "pattern": re.compile(r"\bpari\b|\bпари\b(?!\w)|\bparimatch\b|\bпариматч\b", re.I | re.U),
        "note": "Букмекер с лицензией РК — статус сверять по реестру АФМ.",
    },
    "Tennisi": {
        "pattern": re.compile(r"\btennisi\b|\bтенниси\b", re.I | re.U),
        "note": "Лицензированный букмекер (сверять по реестру АФМ).",
    },
    "Aturbet": {
        "pattern": re.compile(r"\baturbet\b|\bатурбет\b", re.I | re.U),
        "note": "Лицензированный казахстанский букмекер (сверять по реестру АФМ).",
    },
    "Orakbet": {
        "pattern": re.compile(r"\borakbet\b|\bоракбет\b", re.I | re.U),
        "note": "Лицензированный казахстанский букмекер (сверять по реестру АФМ).",
    },
    "1xBet": {
        # 1xBet работает в РК через местную лицензию (БК «1xBet.kz»). ВНИМАНИЕ:
        # глобальный 1xbet.com — отдельный нелицензированный бренд; различать по домену.
        "pattern": re.compile(r"\b1\s?x\s?bet\b|\b1\s?икс\s?бет\b|\b1хбет\b|\b1xbet\b", re.I | re.U),
        "note": "БК 1xBet.kz имеет лицензию РК — сверять с реестром АФМ; глобальный 1xbet.com нелегален.",
    },
}

# --- Аналитик-редактируемые ОВЕРРАЙДЫ реестра (data/licensed_registry.json) ---------
# Формат: {"add": {"<Имя>": {"keywords": ["...", ...], "note": "..."}}, "remove": ["<Имя>"]}
# add — добавить оператора (матчинг по ключевым словам/подстрокам, без regex);
# remove — отключить дефолтного оператора. Файл редактируется через API/UI «обновить
# данные» — реестр конфигурируем без правки кода и без переобучения модели.
_REGISTRY_PATH = config.DATA_DIR / "licensed_registry.json"


def _load_overrides() -> dict:
    try:
        if _REGISTRY_PATH.exists():
            data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_overrides(data: dict) -> None:
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# Дисклеймер, который UI показывает рядом с флагом (чтобы не выдавать стаб за истину).
REGISTRY_DISCLAIMER = (
    "Справочный список лицензированных операторов; сверять с официальным реестром АФМ/ЕУЦ."
)

# Что проверять у разрешённого оператора вместо блокировки.
COMPLIANCE_HINT = (
    "Оператор лицензирован — блокировка не требуется. Проверьте рекламные нормы: "
    "возраст 21+, пометка «реклама», запрет «гарантированного дохода» и агрессивных бонусов."
)


def licensed_operators(text: "str | None") -> list:
    """Список КАНОНИЧЕСКИХ имён лицензированных операторов, упомянутых в тексте.

    Матчинг идёт по НОРМАЛИЗОВАННОМУ тексту (как и сигналы модели — ловит «о л и м п б е т»,
    смешанную латиницу/кириллицу). Учитывает аналитик-оверрайды: добавленные операторы
    (по ключевым словам) и отключённые (remove). Пусто -> []. Не выбрасывает исключение.
    """
    if not text:
        return []
    try:
        norm = normalize_obfuscated(text)
    except Exception:
        norm = str(text)
    ov = _load_overrides()
    removed = {str(n).strip().lower() for n in (ov.get("remove") or [])}
    added = ov.get("add") or {}

    hits = []
    for name, spec in LICENSED_KZ_BOOKMAKERS.items():
        if name.lower() in removed:
            continue
        try:
            if spec["pattern"].search(norm) or spec["pattern"].search(str(text)):
                hits.append(name)
        except Exception:
            continue
    low = norm.lower()
    for name, spec in added.items():
        if name.lower() in removed or name in hits:
            continue
        try:
            kws = spec.get("keywords") or [name]
            if any(str(k).strip().lower() in low for k in kws if str(k).strip()):
                hits.append(name)
        except Exception:
            continue
    return hits


def is_licensed(text: "str | None") -> bool:
    """True, если в тексте упомянут хотя бы один лицензированный в РК оператор."""
    return bool(licensed_operators(text))


def registry_state() -> dict:
    """Текущий эффективный реестр для UI: список операторов {name, note, source,
    keywords}, плюс дисклеймер. source = 'default' | 'added'. Учитывает remove."""
    ov = _load_overrides()
    removed = {str(n).strip().lower() for n in (ov.get("remove") or [])}
    added = ov.get("add") or {}
    operators = []
    for name, spec in LICENSED_KZ_BOOKMAKERS.items():
        if name.lower() in removed:
            continue
        operators.append({"name": name, "note": spec.get("note", ""),
                          "source": "default", "keywords": []})
    for name, spec in added.items():
        if name.lower() in removed:
            continue
        operators.append({"name": name, "note": spec.get("note", ""),
                          "source": "added", "keywords": list(spec.get("keywords") or [])})
    operators.sort(key=lambda o: o["name"].lower())
    return {"operators": operators, "disclaimer": REGISTRY_DISCLAIMER,
            "compliance_hint": COMPLIANCE_HINT}


def set_operator(name: str, licensed: bool, keywords=None, note: str = "") -> dict:
    """Аналитик помечает оператора лицензированным (licensed=True) или нелицензированным
    (False). Персистится в data/licensed_registry.json. Возвращает новый registry_state().

    licensed=True: добавляет оператора (по keywords) ИЛИ снимает его из remove (если был
    дефолтным и отключён). licensed=False: дефолтного -> в remove; добавленного -> убрать
    из add. Это и есть «ручной выбор, что легально в РК».
    """
    name = (name or "").strip()
    if not name:
        return registry_state()
    ov = _load_overrides()
    add = dict(ov.get("add") or {})
    remove = [str(n) for n in (ov.get("remove") or [])]
    is_default = any(name.lower() == d.lower() for d in LICENSED_KZ_BOOKMAKERS)

    if licensed:
        remove = [n for n in remove if n.lower() != name.lower()]  # снять возможный disable
        if not is_default:
            kws = [str(k).strip() for k in (keywords or [name]) if str(k).strip()]
            add[name] = {"keywords": kws or [name], "note": note or "Помечен аналитиком как лицензированный в РК."}
    else:
        add = {k: v for k, v in add.items() if k.lower() != name.lower()}  # убрать из добавленных
        if is_default and not any(n.lower() == name.lower() for n in remove):
            remove.append(name)  # отключить дефолтного
    _save_overrides({"add": add, "remove": remove})
    return registry_state()
