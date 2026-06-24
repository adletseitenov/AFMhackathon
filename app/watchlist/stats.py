"""Персистентная per-channel статистика сканов watchlist (data/watchlist_stats.json).

Для каждого канала храним последнее состояние мониторинга:
  {
    "last_scan":       ISO-строка времени последнего скана (UTC),
    "last_added":      сколько постов добавлено в последний скан,
    "last_flagged":    сколько из них флагнуто (risk >= ESCALATE_THRESHOLD),
    "total_collected": накопленное число добавленных постов за всё время,
  }

Файл — плоский JSON-объект {channel: {...}}. Толерантен к
отсутствующему/битому файлу (возвращает {}). DATA_DIR читается ЛЕНИВО на каждый
вызов, чтобы тесты могли монкипатчить config.DATA_DIR на tmp_path (как в store.py).

Контракт чтения наружу — get_stats() -> {"channels": [ {channel, last_scan,
last_added, last_flagged, total_collected}, ... ]} (стабильная форма для роута).
"""

import json
from datetime import datetime, timezone

from app import config

_FILENAME = "watchlist_stats.json"

# Поля одной записи + дефолты (служат и схемой нормализации при чтении).
_DEFAULTS = {
    "last_scan": None,
    "last_added": 0,
    "last_flagged": 0,
    "total_collected": 0,
}


def _path():
    """Путь к файлу watchlist_stats.json (DATA_DIR читается лениво)."""
    return config.DATA_DIR / _FILENAME


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
    """Приводит одну запись к каноничной форме с дефолтами; битую -> дефолты."""
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
    """Читает {channel: entry}; {} при отсутствии/повреждении/неверном типе."""
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
        # запись на диск не должна валить вызывающий код
        pass


def record_scan(channel: str, added: int, flagged: int, scanned_at: str | None = None) -> dict:
    """Фиксирует результат скана одного канала и персистит.

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
