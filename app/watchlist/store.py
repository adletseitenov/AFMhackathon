"""Персистентный список наблюдаемых записей watchlist (data/watchlist.json).

Формат на диске — JSON-список, элементы которого могут быть:
  • строка (ЛЕГАСИ: telegram-канал, читается как {"target": <str>, "platform": "telegram"})
  • dict {"target": <str>, "platform": <str>} (новый формат)

Это обеспечивает полную обратную совместимость со старыми watchlist.json-файлами.

Нормализация имени записи зависит от платформы:
  • telegram → _normalize() — strips @, t.me/ links, t.me/s/ (поведение не изменено)
  • все остальные платформы → strip пробелов + lstrip('@'), без t.me-парсинга

Идентификатор для дедупликации: (platform, casefold(target)).
Разные платформы с одинаковым target — РАЗНЫЕ записи.

Пустой target после нормализации → noop (запись отброшена).

DATA_DIR читается ЛЕНИВО (config.DATA_DIR) на каждый вызов, чтобы тесты могли
монкипатчить config.DATA_DIR + importlib.reload(store).

Публичное API:
  _normalize(name)              — telegram-нормализатор (НЕИЗМЕНЁН; тесты вызывают напрямую)
  list_channels()               — telegram-цели в порядке вставки (НЕИЗМЕНЁН)
  add_channel(name)             — добавить telegram-запись, вернуть list_channels() (НЕИЗМЕНЁН)
  remove_channel(name)          — удалить telegram-запись, вернуть list_channels() (НЕИЗМЕНЁН)
  list_entries()                — все записи как [{"target","platform"}, ...]
  add_entry(target, platform)   — добавить запись (с per-platform нормализацией), вернуть list_entries()
  remove_entry(target, platform)— удалить запись по identity-ключу, вернуть list_entries()
"""

import json

from app import config

_FILENAME = "watchlist.json"

# Допустимые платформы; всё остальное приводится к "telegram" (обратная совместимость).
_VALID_PLATFORMS = {"telegram", "youtube", "tiktok", "twitch", "kick", "instagram", "operator"}


def _path():
    """Путь к файлу watchlist.json (DATA_DIR читается лениво)."""
    return config.DATA_DIR / _FILENAME


def _normalize(name: str) -> str:
    """Приводит '@x', 'x', 'https://t.me/x', 'https://t.me/s/x' к 'x'.

    Используется ТОЛЬКО для telegram-платформы. Поведение не изменено —
    существующие тесты вызывают store._normalize() напрямую.
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    if "t.me" in raw.lower():
        # последний непустой сегмент пути (отбрасывает trailing slash и /s/)
        segs = [s for s in raw.rstrip("/").split("/") if s]
        raw = segs[-1] if segs else ""
    raw = raw.strip().lstrip("@").strip()
    return raw


def _normalize_for_platform(target: str, platform: str) -> str:
    """Нормализует target в зависимости от платформы.

    telegram → полный _normalize() (t.me-парсинг + strip @)
    прочие   → strip пробелов + lstrip('@'); t.me-ссылки не разбираются
    """
    if platform == "telegram":
        return _normalize(target)
    raw = (target or "").strip().lstrip("@")
    return raw


def _coerce_platform(platform) -> str:
    """Приводит значение платформы к допустимому; unknown/empty/None → 'telegram'."""
    if not platform or not isinstance(platform, str):
        return "telegram"
    p = platform.lower().strip()
    return p if p in _VALID_PLATFORMS else "telegram"


def _identity_key(platform: str, target: str):
    """Ключ дедупликации: (platform, casefold(target))."""
    return (platform, target.casefold())


def _read_entries() -> list[dict]:
    """Читает все записи из файла как [{"target","platform"}, ...].

    Поддерживает легаси-формат (плоский список строк) и новый формат (dict-элементы).
    Применяет дедупликацию по identity-ключу, сохраняет порядок вставки.
    Возвращает [] при отсутствии/повреждении/неверном типе файла.
    """
    path = _path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    seen: set = set()
    out: list[dict] = []
    for item in data:
        if isinstance(item, str):
            # легаси: bare string → telegram
            norm = _normalize(item)
            platform = "telegram"
        elif isinstance(item, dict):
            platform = _coerce_platform(item.get("platform"))
            raw_target = item.get("target", "")
            norm = _normalize_for_platform(raw_target, platform)
        else:
            continue
        if not norm:
            continue
        key = _identity_key(platform, norm)
        if key in seen:
            continue
        seen.add(key)
        out.append({"target": norm, "platform": platform})
    return out


def _write_entries(entries: list[dict]) -> None:
    """Создаёт DATA_DIR при необходимости и пишет список записей в JSON.

    Записывает dict-элементы (новый формат); OSError не валит вызывающий код.
    """
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Публичное API (обратно-совместимое, telegram-path)
# ---------------------------------------------------------------------------

def list_channels() -> list[str]:
    """Текущий список telegram-каналов (нормализованные имена, порядок вставки).

    КОНТРАКТ НЕИЗМЕНЁН: возвращает только telegram-цели как list[str].
    """
    return [e["target"] for e in _read_entries() if e["platform"] == "telegram"]


def add_channel(name: str) -> list[str]:
    """Добавляет telegram-канал (дедуп по нормализованному имени), персистит.

    КОНТРАКТ НЕИЗМЕНЁН: возвращает list_channels() (только telegram-цели).
    """
    norm = _normalize(name)
    if not norm:
        return list_channels()
    entries = _read_entries()
    key = _identity_key("telegram", norm)
    existing_keys = {_identity_key(e["platform"], e["target"]) for e in entries}
    if key not in existing_keys:
        entries.append({"target": norm, "platform": "telegram"})
        _write_entries(entries)
    return [e["target"] for e in _read_entries() if e["platform"] == "telegram"]


def remove_channel(name: str) -> list[str]:
    """Удаляет telegram-канал (по нормализованному имени), персистит.

    КОНТРАКТ НЕИЗМЕНЁН: возвращает list_channels() (только telegram-цели).
    """
    norm = _normalize(name)
    if not norm:
        return list_channels()
    entries = _read_entries()
    key = _identity_key("telegram", norm)
    new_entries = [e for e in entries if _identity_key(e["platform"], e["target"]) != key]
    if len(new_entries) != len(entries):
        _write_entries(new_entries)
    return [e["target"] for e in new_entries if e["platform"] == "telegram"]


# ---------------------------------------------------------------------------
# Новое мультиплатформенное API
# ---------------------------------------------------------------------------

def list_entries() -> list[dict]:
    """Все записи как [{"target": str, "platform": str}, ...] в порядке вставки.

    Гарантирована дедупликация по (platform, casefold(target)).
    """
    return _read_entries()


def add_entry(target: str, platform: str = "telegram") -> list[dict]:
    """Добавляет запись (с per-platform нормализацией), персистит, возвращает list_entries().

    Правила:
      • пустой/пробельный target или пустой после нормализации → noop
      • unknown/empty/None platform → "telegram"
      • дедупликация по (platform, casefold(target))
    """
    platform = _coerce_platform(platform)
    norm = _normalize_for_platform(target, platform)
    if not norm:
        return list_entries()
    entries = _read_entries()
    key = _identity_key(platform, norm)
    existing_keys = {_identity_key(e["platform"], e["target"]) for e in entries}
    if key not in existing_keys:
        entries.append({"target": norm, "platform": platform})
        _write_entries(entries)
    return _read_entries()


def remove_entry(target: str, platform: str = "telegram") -> list[dict]:
    """Удаляет запись по identity-ключу (platform, casefold(target)), персистит.

    Возвращает list_entries(). Если записи нет — noop.
    """
    platform = _coerce_platform(platform)
    norm = _normalize_for_platform(target, platform)
    if not norm:
        return list_entries()
    entries = _read_entries()
    key = _identity_key(platform, norm)
    new_entries = [e for e in entries if _identity_key(e["platform"], e["target"]) != key]
    if len(new_entries) != len(entries):
        _write_entries(new_entries)
        return _read_entries()
    return entries
