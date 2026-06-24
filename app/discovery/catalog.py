"""Реестр НАСТРАИВАЕМОГО автопоиска КӨЗ — единая точка расширения.

Здесь собраны все «крутилки» автопоиска, чтобы добавить новую страну/категорию/
тип контента/сортировку/источник можно было ОДНОЙ записью, не трогая движок
discover(). Движок читает запросы через build_queries(country, categories) и
сид-аккаунты через SEED_ACCOUNTS; роут отдаёт фронту весь реестр через
GET /api/discover/catalog, чтобы селекторы наполнялись сами.

Контракт (агенты стыкуются по нему):
- COUNTRIES   = {id: {"label": ..}}            — минимум kz, ru, all.
- CATEGORIES  = {id: {"label": .., "queries": [...]}} — all/casino/pyramid/fraud/crypto.
- CONTENT_TYPES= {id: {"label": ..}}            — all/video(VOD/посты)/live(эфиры).
- SORTS       = {id: {"label": ..}}            — relevance/recent/popular.
- SEED_ACCOUNTS= {"tiktok":[...], "twitch":[...], "kick":[...], "instagram":[...]}.
- build_queries(country, categories) -> list[str] — локализованные запросы по
  стране+категориям ('all' категория = объединение всех; 'all' страна = базовые
  запросы без гео-привязки).

Все пользовательские строки — на русском.
"""

# --- Страны (гео-профиль выдачи) ---------------------------------------------
# Для каждой страны — суффиксы-гео, которые подмешиваются к запросам категории,
# чтобы выдача была локализованной (KZ-латиница ловит казахскую выдачу, которая
# часто индексируется именно так). 'all' = без гео-привязки (базовые запросы).
COUNTRIES = {
    "all": {"label": "Все страны", "suffixes": []},
    "kz": {"label": "Казахстан", "suffixes": ["казахстан", "kz", "kazakhstan"]},
    "ru": {"label": "Россия", "suffixes": ["россия", "rf"]},
}

# --- Категории угроз (каждая со своим набором базовых запросов) ----------------
# 'all' — служебная: её queries собираются из объединения всех остальных в
# build_queries(); список здесь оставлен пустым намеренно.
CATEGORIES = {
    "all": {"label": "Все категории", "queries": []},
    "casino": {
        "label": "Казино / букмекеры",
        "queries": [
            "казино онлайн промокод занос",
            "1xbet промокод бонус сегодня",
            "mostbet занос стратегия казино",
            "melbet промокод фрибет регистрация",
            "pinup казино бонус занос онлайн",
            "vavada фриспины занос казино",
            "1win промокод бонус депозит",
            "бездепозитный бонус казино промокод",
            "фриспины занос казино онлайн",
            "kazino promokod bonus tirkelu",  # KZ-латиница
            "тегін бонус казино промокод",  # KZ-кириллица
        ],
    },
    "pyramid": {
        "label": "Финпирамиды",
        "queries": [
            "гарантированный доход инвестиции в месяц",
            "пирамида заработок без вложений",
            "пассивный доход телеграм инвестиции",
            "хайп проект инвестиции доход",
            "инвестиции телеграм гарантированная прибыль",
            "удвоение депозита проект заработок",
            "investitsiya kiris kepildik telegram",  # KZ-латиница: гарант. доход
        ],
    },
    "fraud": {
        "label": "Мошенничество",
        "queries": [
            "заработок схема развод деньги",
            "обещают вернуть деньги предоплата комиссия",
            "лёгкий заработок онлайн вложи получи",
        ],
    },
    "crypto": {
        "label": "Крипто-удвоение / сигналы",
        "queries": [
            "крипто удвоение депозита за час",
            "крипто сигналы удвоение телеграм",
            "обмен крипты гарантия вывод деньги",
        ],
    },
}

# --- Тип контента -------------------------------------------------------------
CONTENT_TYPES = {
    "all": {"label": "Всё"},
    "video": {"label": "VOD / посты"},
    "live": {"label": "Прямые эфиры"},
}

# --- Сортировка найденного перед ингестом -------------------------------------
SORTS = {
    "relevance": {"label": "По релевантности"},
    "recent": {"label": "Сначала свежие"},
    "popular": {"label": "По популярности"},
}

# --- Сид-аккаунты по площадкам (расширяемо: новый источник = ещё одна строка) --
# Перенесено из discover.py + добавлены проверяемые казино-стримеры/аккаунты.
# Реклама этих казино/букмекеров/HYIP в РК нелегальна; их ленты/VOD-ы — основной
# источник нелегального гемблинг-контента.
SEED_ACCOUNTS = {
    "tiktok": [
        "mostbet_official", "1win", "parimatch", "olimpbet", "betboom",
        "1xbet_global", "betwinner", "1win_casino", "olimp",
        # добавлены: ещё казино/слот-аккаунты с активной лентой
        "pin_up_casino", "vavada_official", "melbet_official",
        "leon_bookmaker", "fonbet_official",
    ],
    "instagram": [
        "1xbet", "mostbet", "parimatch",
        # добавлены best-effort (закрыты логин-волом, но в реестре)
        "pinup.casino", "vavada.casino", "melbet.official", "betwinner.official",
    ],
    "twitch": [
        "xposed", "roshtein", "classybeef", "trainwreckstv", "ayezee",
        # добавлены: проверяемые слот-/казино-стримеры
        "deuceace", "casinodaddy", "fruityslots", "letsgiveitaspin", "watchgamestv",
    ],
    "kick": [
        "roshtein", "xposed", "trainwreck", "classybeef", "slots",
        # добавлены: казино-стримеры с активным архивом на Kick
        "deuceace", "casinodaddy", "fruityslots", "adin", "stake",
    ],
}


def _norm_categories(categories) -> list:
    """Нормализует список категорий: None/пусто/'all' -> все реальные категории
    (без служебной 'all'); иначе — только валидные id в порядке реестра."""
    real = [c for c in CATEGORIES if c != "all"]
    if not categories:
        return real
    wanted = {str(c).lower().strip() for c in categories}
    if "all" in wanted:
        return real
    picked = [c for c in real if c in wanted]
    return picked or real  # неизвестные id -> фоллбэк на все, чтобы не вернуть пусто


def build_queries(country: str = "all", categories=None) -> list:
    """Локализованные запросы по стране+категориям.

    categories: None/[]/['all'] -> объединение запросов ВСЕХ категорий; иначе —
      только выбранные (в порядке реестра, дедуп). Неизвестные id игнорируются
      (если все неизвестны — фоллбэк на все категории, чтобы не вернуть пусто).
    country: 'all' -> базовые запросы как есть; иначе к КАЖДОМУ запросу
      добавляется ОДИН гео-суффикс страны (первый из COUNTRIES[country]['suffixes'])
      — это сужает выдачу под регион, не размножая список.

    Возвращает список без дублей, сохраняя порядок.
    """
    cats = _norm_categories(categories)
    base: list = []
    seen: set = set()
    for c in cats:
        for q in CATEGORIES[c]["queries"]:
            if q not in seen:
                seen.add(q)
                base.append(q)

    country = (country or "all").lower().strip()
    suffixes = COUNTRIES.get(country, {}).get("suffixes") or []
    if not suffixes:
        return base

    geo = suffixes[0]
    out: list = []
    out_seen: set = set()
    for q in base:
        # уже содержит гео-слово — не дублируем
        loc = q if geo in q.lower() else f"{q} {geo}"
        if loc not in out_seen:
            out_seen.add(loc)
            out.append(loc)
    return out


def catalog_payload() -> dict:
    """Плоский реестр для GET /api/discover/catalog (списки {id,label})."""
    def _items(reg):
        return [{"id": k, "label": v.get("label", k)} for k, v in reg.items()]

    return {
        "countries": _items(COUNTRIES),
        "categories": _items(CATEGORIES),
        "content_types": _items(CONTENT_TYPES),
        "sorts": _items(SORTS),
    }
