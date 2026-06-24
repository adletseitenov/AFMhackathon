"""Персистентный список наблюдаемых Telegram-каналов (data/watchlist.json).

Хранит плоский JSON-список нормализованных имён каналов. Толерантен к
отсутствующему/битому файлу (возвращает []). DATA_DIR читается лениво на
каждый вызов, чтобы тесты могли монкипатчить config.DATA_DIR на tmp_path.

Нормализация имени:
  - strip пробелов
  - для t.me-ссылок берётся последний значащий сегмент пути
    (учитывая web-preview формат t.me/s/<channel>)
  - lstrip('@')
"""

import json

from app import config

_FILENAME = "watchlist.json"


def _path():
    """Путь к файлу watchlist.json (DATA_DIR читается лениво)."""
    return config.DATA_DIR / _FILENAME


def _normalize(name: str) -> str:
    """Приводит '@x', 'x', 'https://t.me/x', 'https://t.me/s/x' к 'x'."""
    raw = (name or "").strip()
    if not raw:
        return ""
    if "t.me" in raw.lower():
        # последний непустой сегмент пути (отбрасывает trailing slash и /s/)
        segs = [s for s in raw.rstrip("/").split("/") if s]
        raw = segs[-1] if segs else ""
    raw = raw.strip().lstrip("@").strip()
    return raw


def _read() -> list[str]:
    """Читает список из файла; [] при отсутствии/повреждении/неверном типе."""
    path = _path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        norm = _normalize(item if isinstance(item, str) else str(item))
        if norm and norm not in out:
            out.append(norm)
    return out


def _write(channels: list[str]) -> None:
    """Создаёт DATA_DIR при необходимости и атомарно-достаточно пишет JSON."""
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(channels, fh, ensure_ascii=False, indent=2)
    except OSError:
        # запись на диск не должна валить вызывающий код
        pass


def list_channels() -> list[str]:
    """Текущий список наблюдаемых каналов (нормализованные имена)."""
    return _read()


def add_channel(name: str) -> list[str]:
    """Добавляет канал (дедуп по нормализованному имени), персистит, возвращает список."""
    norm = _normalize(name)
    channels = _read()
    if norm and norm not in channels:
        channels.append(norm)
        _write(channels)
    return channels


def remove_channel(name: str) -> list[str]:
    """Удаляет канал (по нормализованному имени), персистит, возвращает список."""
    norm = _normalize(name)
    channels = _read()
    if norm in channels:
        channels = [c for c in channels if c != norm]
        _write(channels)
    return channels
