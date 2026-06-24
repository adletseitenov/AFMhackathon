"""Персистентная per-channel/per-entry статистика сканов watchlist.

ДИЗАЙН — ДВА ПАРАЛЛЕЛЬНЫХ ХРАНИЛИЩА:

1. data/watchlist_stats.json — ЛЕГАСИ, telegram-only.
   Плоский {channel: {last_scan, last_added, last_flagged, total_collected}}.
   Используется record_scan() / record_scan_result() / get_stats().
   КОНТРАКТ НЕИЗМЕНЁН: get_stats() -> {"channels": [...]} отсортирован по channel.
   Существующие 16 тестов в test_stats.py проходят БЕЗ изменений.

2. data/watchlist_entry_stats.json — НОВЫЙ, мультиплатформенный.
   Плоский {"platform|target": {target, platform, last_scan, last_added,
   last_flagged, total_collected, licensed}}.
   Используется record_entry_scan() / get_entry_stats().

Оба модуля разделены намеренно, чтобы новое не ломало старое.
DATA_DIR читается ЛЕНИВО (config.DATA_DIR) на каждый вызов.
Толерантны к отсутствующему/битому файлу; никогда не бросают наружу.
"""

import json
from datetime import datetime, timezone

from app import config

# --- Легаси-файл (telegram stats) ---
_FILENAME = "watchlist_stats.json"

# --- Новый файл (мультиплатформенные entry stats) ---
_ENTRY_FILENAME = "watchlist_entry_stats.json"

# Поля одной записи + дефолты (служат и схемой нормализации при чтении).
_DEFAULTS = {
    "last_scan": None,
    "last_added": 0,
    "last_flagged": 0,
    "total_collected": 0,
}

# Поля per-entry записи + дефолты.
_ENTRY_DEFAULTS = {
    "last_scan": None,
    "last_added": 0,
    "last_flagged": 0,
    "total_collected": 0,
    "licensed": None,
}


def _path():
    """Путь к файлу watchlist_stats.json (DATA_DIR читается лениво)."""
    return config.DATA_DIR / _FILENAME


def _entry_path():
    """Путь к файлу watchlist_entry_stats.json (DATA_DIR читается лениво)."""
    return config.DATA_DIR / _ENTRY_FILENAME


def _now_iso() -> str:
    """Текущее UTC-время в ISO-формате (вынесено для монкипатча в тестах)."""
    return datetime.now(timezone.utc).isoformat()


def _int(value, default: int = 0) -> int:
    """Безопасное приведение к int (битые значения -> default)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_entry(raw) -> dict:
    """Приводит одну telegram-запись к каноничной форме с дефолтами; битую -> дефолты."""
    if not isinstance(raw, dict):
        raw = {}
    last_scan = raw.get("last_scan")
    if last_scan is not None and not isinstance(last_scan, str):
        last_scan = str(last_scan)
    return {
        "last_scan": last_scan,
        "last_added": _int(raw.get("last_added"), 0),
        "last_flagged": _int(raw.get("last_flagged"), 0),
        "total_collected": _int(raw.get("total_collected"), 0),
    }


def _read() -> dict:
    """Читает {channel: entry} из watchlist_stats.json; {} при отсутствии/повреждении."""
    path = _path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for channel, entry in data.items():
        if not isinstance(channel, str) or not channel.strip():
            continue
        out[channel] = _normalize_entry(entry)
    return out


def _write(stats: dict) -> None:
    """Создаёт DATA_DIR при необходимости и пишет JSON; ошибки диска не валят вызов."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Легаси-API (telegram-only, КОНТРАКТ НЕИЗМЕНЁН)
# ---------------------------------------------------------------------------

def record_scan(channel: str, added: int, flagged: int, scanned_at: str | None = None) -> dict:
    """Фиксирует результат скана одного telegram-канала и персистит.

    Обновляет last_scan/last_added/last_flagged и накапливает total_collected.
    Возвращает обновлённую запись канала. Никогда не бросает.
    """
    channel = (channel or "").strip()
    if not channel:
        return dict(_DEFAULTS)
    added = _int(added, 0)
    flagged = _int(flagged, 0)
    stats = _read()
    prev = stats.get(channel, _DEFAULTS)
    entry = {
        "last_scan": scanned_at or _now_iso(),
        "last_added": added,
        "last_flagged": flagged,
        "total_collected": _int(prev.get("total_collected"), 0) + added,
    }
    stats[channel] = entry
    _write(stats)
    return entry


def record_scan_result(result: dict, scanned_at: str | None = None) -> None:
    """Применяет per-channel результат scan_telegram к статистике.

    result: {..., "channels": [{"channel", "added", "flagged"}, ...]}.
    Берёт added/flagged из каждой per-channel записи; единая метка времени на скан.
    Толерантен к отсутствию/битости полей. Никогда не бросает.
    """
    if not isinstance(result, dict):
        return
    per_channel = result.get("channels")
    if not isinstance(per_channel, list):
        return
    when = scanned_at or _now_iso()
    for row in per_channel:
        if not isinstance(row, dict):
            continue
        channel = row.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            continue
        record_scan(
            channel,
            added=_int(row.get("added"), 0),
            flagged=_int(row.get("flagged"), 0),
            scanned_at=when,
        )


def get_stats() -> dict:
    """Стабильная форма для роута: {"channels": [ {channel, ...поля...}, ... ]}.

    Список отсортирован по имени канала для детерминизма. Никогда не бросает.
    """
    stats = _read()
    channels = []
    for channel in sorted(stats):
        entry = stats[channel]
        channels.append(
            {
                "channel": channel,
                "last_scan": entry.get("last_scan"),
                "last_added": entry.get("last_added", 0),
                "last_flagged": entry.get("last_flagged", 0),
                "total_collected": entry.get("total_collected", 0),
            }
        )
    return {"channels": channels}


# ---------------------------------------------------------------------------
# Вспомогательные функции для нового entry-хранилища
# ---------------------------------------------------------------------------

def _entry_key(target: str, platform: str) -> str:
    """Составной ключ для хранилища: "platform|target"."""
    return f"{platform}|{target}"


def _normalize_entry_record(raw) -> dict:
    """Приводит одну entry-запись к каноничной форме; битую -> дефолты."""
    if not isinstance(raw, dict):
        raw = {}
    last_scan = raw.get("last_scan")
    if last_scan is not None and not isinstance(last_scan, str):
        last_scan = str(last_scan)
    licensed = raw.get("licensed", None)
    # licensed должен быть bool или None
    if licensed is not None and not isinstance(licensed, bool):
        licensed = bool(licensed)
    return {
        "target": raw.get("target", ""),
        "platform": raw.get("platform", "telegram"),
        "last_scan": last_scan,
        "last_added": _int(raw.get("last_added"), 0),
        "last_flagged": _int(raw.get("last_flagged"), 0),
        "total_collected": _int(raw.get("total_collected"), 0),
        "licensed": licensed,
    }


def _read_entry_stats() -> dict:
    """Читает {"platform|target": entry} из watchlist_entry_stats.json.

    Возвращает {} при отсутствии/повреждении/неверном типе файла.
    """
    path = _entry_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for key, entry in data.items():
        if not isinstance(key, str):
            continue
        out[key] = _normalize_entry_record(entry)
    return out


def _write_entry_stats(data: dict) -> None:
    """Записывает entry-stats в watchlist_entry_stats.json; OSError не валит вызов."""
    path = _entry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Новое мультиплатформенное API
# ---------------------------------------------------------------------------

def record_entry_scan(
    target,
    platform: str,
    added: int,
    flagged: int,
    scanned_at: str | None = None,
    licensed=None,
) -> dict:
    """Фиксирует результат скана одной записи (любая платформа) и персистит.

    Накапливает total_collected, обновляет last_scan/last_added/last_flagged.
    Хранит licensed (bool|None) для operator-записей.
    Возвращает обновлённую запись. Никогда не бросает.

    Сигнатура (frozen):
      record_entry_scan(target, platform, added, flagged,
                        scanned_at=None, licensed=None) -> dict
    """
    try:
        target = (target or "").strip() if isinstance(target, str) else ""
        platform = (platform or "telegram").strip() if isinstance(platform, str) else "telegram"
        if not target or not platform:
            return dict(_ENTRY_DEFAULTS)
        added_int = _int(added, 0)
        flagged_int = _int(flagged, 0)

        data = _read_entry_stats()
        key = _entry_key(target, platform)
        prev = data.get(key, {})
        prev_total = _int(prev.get("total_collected"), 0)

        # licensed: новое значение перекрывает None, None не перекрывает bool
        prev_licensed = prev.get("licensed", None)
        new_licensed = licensed if licensed is not None else prev_licensed

        entry = {
            "target": target,
            "platform": platform,
            "last_scan": scanned_at or _now_iso(),
            "last_added": added_int,
            "last_flagged": flagged_int,
            "total_collected": prev_total + added_int,
            "licensed": new_licensed,
        }
        data[key] = entry
        _write_entry_stats(data)
        return entry
    except Exception:
        # никогда не бросаем наружу
        return dict(_ENTRY_DEFAULTS)


def get_entry_stats() -> dict:
    """Возвращает мультиплатформенную статистику в стабильной форме для роута.

    Форма ответа (frozen):
      {"entries": [
          {"target": str, "platform": str, "last_scan": str|None,
           "last_added": int, "last_flagged": int, "total_collected": int,
           "licensed": bool|None},
          ...
      ]}
    Отсортировано детерминированно по (platform, target). Никогда не бросает.
    """
    try:
        data = _read_entry_stats()
        entries = []
        for entry in data.values():
            entries.append(
                {
                    "target": entry.get("target", ""),
                    "platform": entry.get("platform", "telegram"),
                    "last_scan": entry.get("last_scan"),
                    "last_added": entry.get("last_added", 0),
                    "last_flagged": entry.get("last_flagged", 0),
                    "total_collected": entry.get("total_collected", 0),
                    "licensed": entry.get("licensed"),
                }
            )
        entries.sort(key=lambda e: (e["platform"], e["target"]))
        return {"entries": entries}
    except Exception:
        return {"entries": []}
