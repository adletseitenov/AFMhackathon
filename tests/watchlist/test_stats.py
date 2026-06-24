"""Тесты per-channel статистики сканов watchlist (app.watchlist.stats).

Без тяжёлых библиотек и без сети: scan_telegram монкипатчится, БД/HTTP не дёргаются.
DATA_DIR монкипатчится в tmp_path, чтобы не трогать реальный data/watchlist_stats.json.
"""

import importlib
import json

import pytest

from app import config


@pytest.fixture
def tmp_stats(tmp_path, monkeypatch):
    """Подменяет config.DATA_DIR на tmp и перезагружает stats (читает DATA_DIR лениво)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import stats
    importlib.reload(stats)
    return stats


# --- пустое / отсутствующее / битое состояние ---

def test_get_stats_empty_when_no_file(tmp_stats, tmp_path):
    assert not (tmp_path / "watchlist_stats.json").exists()
    assert tmp_stats.get_stats() == {"channels": []}


def test_get_stats_corrupt_file_returns_empty(tmp_stats, tmp_path):
    (tmp_path / "watchlist_stats.json").write_text("{ not json ", encoding="utf-8")
    assert tmp_stats.get_stats() == {"channels": []}
    # поверх битого файла можно снова записывать
    tmp_stats.record_scan("a", added=1, flagged=0)
    chans = tmp_stats.get_stats()["channels"]
    assert [c["channel"] for c in chans] == ["a"]


def test_get_stats_non_dict_file_returns_empty(tmp_stats, tmp_path):
    (tmp_path / "watchlist_stats.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert tmp_stats.get_stats() == {"channels": []}


# --- record_scan: запись и накопление ---

def test_record_scan_persists_entry(tmp_stats, tmp_path):
    tmp_stats.record_scan("news", added=3, flagged=1, scanned_at="2026-06-24T00:00:00+00:00")
    raw = json.loads((tmp_path / "watchlist_stats.json").read_text(encoding="utf-8"))
    assert raw["news"] == {
        "last_scan": "2026-06-24T00:00:00+00:00",
        "last_added": 3,
        "last_flagged": 1,
        "total_collected": 3,
    }


def test_record_scan_accumulates_total(tmp_stats):
    tmp_stats.record_scan("a", added=2, flagged=1, scanned_at="2026-06-24T00:00:00+00:00")
    tmp_stats.record_scan("a", added=5, flagged=0, scanned_at="2026-06-24T01:00:00+00:00")
    entry = {c["channel"]: c for c in tmp_stats.get_stats()["channels"]}["a"]
    assert entry["total_collected"] == 7  # 2 + 5
    assert entry["last_added"] == 5  # последний скан
    assert entry["last_flagged"] == 0
    assert entry["last_scan"] == "2026-06-24T01:00:00+00:00"


def test_record_scan_default_timestamp(tmp_stats, monkeypatch):
    monkeypatch.setattr(tmp_stats, "_now_iso", lambda: "2026-06-24T12:00:00+00:00")
    tmp_stats.record_scan("x", added=1, flagged=0)
    entry = tmp_stats.get_stats()["channels"][0]
    assert entry["last_scan"] == "2026-06-24T12:00:00+00:00"


def test_record_scan_empty_channel_noop(tmp_stats):
    tmp_stats.record_scan("  ", added=5, flagged=2)
    assert tmp_stats.get_stats() == {"channels": []}


# --- record_scan_result: применение per-channel результата scan_telegram ---

def test_record_scan_result_applies_per_channel(tmp_stats, monkeypatch):
    monkeypatch.setattr(tmp_stats, "_now_iso", lambda: "2026-06-24T09:00:00+00:00")
    result = {
        "added": 4,
        "flagged": 1,
        "channels": [
            {"channel": "a", "fetched": 3, "added": 3, "flagged": 1},
            {"channel": "b", "fetched": 2, "added": 1, "flagged": 0},
        ],
    }
    tmp_stats.record_scan_result(result)
    by = {c["channel"]: c for c in tmp_stats.get_stats()["channels"]}
    assert by["a"]["last_added"] == 3 and by["a"]["last_flagged"] == 1
    assert by["a"]["total_collected"] == 3
    assert by["b"]["last_added"] == 1 and by["b"]["last_flagged"] == 0
    # единая метка времени на весь скан
    assert by["a"]["last_scan"] == by["b"]["last_scan"] == "2026-06-24T09:00:00+00:00"


def test_record_scan_result_tolerates_bad_shapes(tmp_stats):
    # не dict / без channels / битые строки внутри — не должно бросать
    tmp_stats.record_scan_result(None)
    tmp_stats.record_scan_result({"added": 1})
    tmp_stats.record_scan_result({"channels": "nope"})
    tmp_stats.record_scan_result({"channels": [None, {}, {"channel": ""}, 42]})
    assert tmp_stats.get_stats() == {"channels": []}


def test_get_stats_sorted_by_channel(tmp_stats):
    tmp_stats.record_scan("zeta", added=1, flagged=0)
    tmp_stats.record_scan("alpha", added=1, flagged=0)
    chans = [c["channel"] for c in tmp_stats.get_stats()["channels"]]
    assert chans == ["alpha", "zeta"]


# --- интеграция: service.scan_watchlist обновляет статистику ---

def test_scan_watchlist_records_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store, stats, service
    importlib.reload(store)
    importlib.reload(stats)
    importlib.reload(service)

    store.add_channel("a")
    store.add_channel("b")

    def fake_scan(channels, conn=None, limit=12):
        return {
            "added": 4,
            "flagged": 1,
            "channels": [
                {"channel": "a", "fetched": 5, "added": 3, "flagged": 1},
                {"channel": "b", "fetched": 2, "added": 1, "flagged": 0},
            ],
        }

    monkeypatch.setattr(service, "scan_telegram", fake_scan)
    result = service.scan_watchlist(conn="SENTINEL")
    assert result["added"] == 4

    by = {c["channel"]: c for c in stats.get_stats()["channels"]}
    assert by["a"]["last_added"] == 3
    assert by["a"]["last_flagged"] == 1
    assert by["a"]["total_collected"] == 3
    assert by["a"]["last_scan"] is not None
    assert by["b"]["last_added"] == 1


def test_scan_watchlist_stats_failure_does_not_break_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store, stats, service
    importlib.reload(store)
    importlib.reload(stats)
    importlib.reload(service)

    store.add_channel("a")

    def fake_scan(channels, conn=None, limit=12):
        return {"added": 1, "flagged": 0, "channels": [{"channel": "a", "added": 1, "flagged": 0}]}

    monkeypatch.setattr(service, "scan_telegram", fake_scan)

    def boom(result, scanned_at=None):
        raise RuntimeError("stats disk error")

    monkeypatch.setattr(service.stats, "record_scan_result", boom)
    # скан всё равно возвращает результат, статистика просто не записалась
    result = service.scan_watchlist(conn=None)
    assert result["added"] == 1


# --- эндпоинт GET /api/watchlist/stats через TestClient ---

def test_stats_endpoint_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store, stats, service
    importlib.reload(store)
    importlib.reload(stats)
    importlib.reload(service)

    def fake_scan(channels, conn=None, limit=12):
        return {
            "added": 2,
            "flagged": 1,
            "channels": [{"channel": "news", "fetched": 4, "added": 2, "flagged": 1}],
        }

    monkeypatch.setattr(service, "scan_telegram", fake_scan)

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        # изначально пусто
        r = client.get("/api/watchlist/stats")
        assert r.status_code == 200
        assert r.json() == {"channels": []}

        # добавим канал и просканируем -> статистика появляется
        client.post("/api/watchlist", json={"channel": "news"})
        rs = client.post("/api/watchlist/scan")
        assert rs.status_code == 200

        r = client.get("/api/watchlist/stats")
        assert r.status_code == 200
        body = r.json()
        assert "channels" in body
        assert len(body["channels"]) == 1
        entry = body["channels"][0]
        assert set(entry.keys()) == {
            "channel",
            "last_scan",
            "last_added",
            "last_flagged",
            "total_collected",
        }
        assert entry["channel"] == "news"
        assert entry["last_added"] == 2
        assert entry["last_flagged"] == 1
        assert entry["total_collected"] == 2
        assert entry["last_scan"] is not None
