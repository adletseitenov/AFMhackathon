"""Тесты WATCHLIST + непрерывного скана КӨЗ.

Должны проходить БЕЗ тяжёлых библиотек: ничего тут их не требует
(scan_telegram монкипатчится, БД/HTTP не дёргаются).
DATA_DIR монкипатчится в tmp_path, чтобы не трогать реальный data/watchlist.json.
"""

import importlib

import pytest

from app import config


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Подменяет config.DATA_DIR на tmp и перезагружает store (читает DATA_DIR лениво)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store
    importlib.reload(store)
    return store


# --- _normalize ---

def test_normalize_strips_at_and_whitespace(tmp_data):
    assert tmp_data._normalize("  @x  ") == "x"


def test_normalize_plain(tmp_data):
    assert tmp_data._normalize("x") == "x"


def test_normalize_tme_link(tmp_data):
    assert tmp_data._normalize("https://t.me/x") == "x"


def test_normalize_tme_link_with_trailing_slash(tmp_data):
    assert tmp_data._normalize("https://t.me/x/") == "x"


def test_normalize_tme_s_link(tmp_data):
    assert tmp_data._normalize("https://t.me/s/somechannel") == "somechannel"


def test_normalize_all_variants_equal(tmp_data):
    forms = ["@x", "x", "https://t.me/x", "  @x "]
    assert {tmp_data._normalize(f) for f in forms} == {"x"}


def test_normalize_empty(tmp_data):
    assert tmp_data._normalize("   ") == ""
    assert tmp_data._normalize("@") == ""


# --- list / add / remove / dedup / persistence ---

def test_list_empty_initially(tmp_data):
    assert tmp_data.list_channels() == []


def test_add_channel(tmp_data):
    result = tmp_data.add_channel("@durov")
    assert result == ["durov"]
    assert tmp_data.list_channels() == ["durov"]


def test_add_dedup_across_forms(tmp_data):
    tmp_data.add_channel("@x")
    tmp_data.add_channel("x")
    tmp_data.add_channel("https://t.me/x")
    assert tmp_data.list_channels() == ["x"]


def test_add_multiple_distinct(tmp_data):
    tmp_data.add_channel("a")
    result = tmp_data.add_channel("@b")
    assert result == ["a", "b"]


def test_add_empty_is_noop(tmp_data):
    assert tmp_data.add_channel("   ") == []
    assert tmp_data.add_channel("@") == []
    assert tmp_data.list_channels() == []


def test_remove_channel(tmp_data):
    tmp_data.add_channel("a")
    tmp_data.add_channel("b")
    result = tmp_data.remove_channel("@a")
    assert result == ["b"]
    assert tmp_data.list_channels() == ["b"]


def test_remove_missing_is_noop(tmp_data):
    tmp_data.add_channel("a")
    assert tmp_data.remove_channel("zzz") == ["a"]


def test_persistence_across_reload(tmp_data):
    tmp_data.add_channel("persisted")
    importlib.reload(tmp_data)
    assert tmp_data.list_channels() == ["persisted"]


def test_corrupt_file_returns_empty(tmp_data, tmp_path):
    (tmp_path / "watchlist.json").write_text("{ not json ", encoding="utf-8")
    assert tmp_data.list_channels() == []
    # и можно снова добавлять поверх битого файла
    assert tmp_data.add_channel("ok") == ["ok"]


def test_missing_file_returns_empty(tmp_data, tmp_path):
    assert not (tmp_path / "watchlist.json").exists()
    assert tmp_data.list_channels() == []


# --- service.scan_watchlist ---

def test_scan_watchlist_empty(tmp_data, monkeypatch):
    from app.watchlist import service
    importlib.reload(service)
    result = service.scan_watchlist(conn=None)
    assert result == {"added": 0, "flagged": 0, "channels": []}


def test_scan_watchlist_aggregates(tmp_data, monkeypatch):
    tmp_data.add_channel("a")
    tmp_data.add_channel("b")

    from app.watchlist import service
    importlib.reload(service)

    captured = {}

    def fake_scan(channels, conn=None, limit=12):
        captured["channels"] = list(channels)
        captured["conn"] = conn
        return {
            "added": 5,
            "flagged": 2,
            "channels": [{"channel": c, "fetched": 3, "added": 1, "flagged": 0} for c in channels],
        }

    monkeypatch.setattr(service, "scan_telegram", fake_scan)
    result = service.scan_watchlist(conn="SENTINEL")

    assert captured["channels"] == ["a", "b"]
    assert captured["conn"] == "SENTINEL"
    assert result["added"] == 5
    assert result["flagged"] == 2
    assert len(result["channels"]) == 2


def test_scan_watchlist_never_raises(tmp_data, monkeypatch):
    tmp_data.add_channel("a")

    from app.watchlist import service
    importlib.reload(service)

    def boom(channels, conn=None, limit=12):
        raise RuntimeError("network down")

    monkeypatch.setattr(service, "scan_telegram", boom)
    result = service.scan_watchlist(conn=None)
    # не должно бросать — структурный ответ
    assert result["added"] == 0
    assert result["flagged"] == 0
    assert "error" in result


# --- routes via TestClient ---

def test_routes_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # перезагружаем store, чтобы он указывал на tmp DATA_DIR
    from app.watchlist import store
    importlib.reload(store)

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        # начально пусто (по нашему tmp DATA_DIR)
        r = client.get("/api/watchlist")
        assert r.status_code == 200
        assert r.json() == {"channels": []}

        # add
        r = client.post("/api/watchlist", json={"channel": "@news"})
        assert r.status_code == 200
        assert r.json()["channels"] == ["news"]

        # add dedup via link form
        r = client.post("/api/watchlist", json={"channel": "https://t.me/news"})
        assert r.json()["channels"] == ["news"]

        # list
        r = client.get("/api/watchlist")
        assert r.json()["channels"] == ["news"]

        # remove
        r = client.delete("/api/watchlist/news")
        assert r.status_code == 200
        assert r.json()["channels"] == []


def test_routes_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store, service
    importlib.reload(store)
    importlib.reload(service)

    def fake_scan(channels, conn=None, limit=12):
        return {"added": 1, "flagged": 0, "channels": [{"channel": c} for c in channels]}

    monkeypatch.setattr(service, "scan_telegram", fake_scan)

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        client.post("/api/watchlist", json={"channel": "x"})
        r = client.post("/api/watchlist/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == 1
        assert body["channels"] == [{"channel": "x"}]
