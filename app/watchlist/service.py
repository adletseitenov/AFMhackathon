"""Непрерывный скан: прогон всех каналов watchlist через live-сканер Telegram.

scan_watchlist(conn) читает список каналов из store и передаёт их в
app.ingestion.scan.scan_telegram. Любой сбой одного скана не должен бросать
исключение наружу — возвращаем структурный ответ с полем error.

scan_telegram импортируется в namespace модуля, чтобы тесты могли его
монкипатчить (monkeypatch.setattr(service, "scan_telegram", ...)).
"""

from app.ingestion.scan import scan_telegram
from app.watchlist import stats, store

_EMPTY = {"added": 0, "flagged": 0, "channels": []}


def scan_watchlist(conn=None) -> dict:
    """Сканирует все каналы из watchlist; пустой список -> нулевой результат.

    Возвращает {added, flagged, channels:[...]} (структура scan_telegram).
    Никогда не бросает: при ошибке -> {added:0, flagged:0, channels:[], error:str}.
    """
    try:
        channels = store.list_channels()
    except Exception as exc:  # чтение списка не должно валить вызов
        return {**_EMPTY, "error": f"watchlist read failed: {exc}"}

    if not channels:
        return dict(_EMPTY)

    try:
        result = scan_telegram(channels, conn=conn)
    except Exception as exc:  # сбой скана не пробрасываем как 500
        return {**_EMPTY, "error": f"scan failed: {exc}"}

    if not isinstance(result, dict):
        return dict(_EMPTY)
    result.setdefault("added", 0)
    result.setdefault("flagged", 0)
    result.setdefault("channels", [])

    # Фиксируем per-channel статистику скана (last_scan/added/flagged/total).
    # Запись стат не должна валить скан — изолируем её сбои.
    try:
        stats.record_scan_result(result)
    except Exception:  # noqa: BLE001 — мониторинг не критичнее самого скана
        pass

    return result
