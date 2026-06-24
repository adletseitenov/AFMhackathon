"""Тесты мультиплатформенных маршрутов watchlist (TDD, Wave 2).

Проверяют новые возможности routes.py БЕЗ поломки существующих тестов.
Монкипатчим service и store через TestClient.

Контракт:
  GET  /api/watchlist               → {"channels":[...]}  (без "entries", backward-compat)
  POST /api/watchlist               body {channel/target, platform?}
    telegram → {"channels":[...]}   (backward-compat)
    non-tg   → {"channels":[...], "entries":[...]}
  DELETE /api/watchlist/{channel}?platform= → {"channels":[...]}
  POST /api/watchlist/scan          → {added, flagged, channels, entries, ...}
  GET  /api/watchlist/stats         → {"channels":[...]}  (legacy, ТОЧНО как get_stats())
  GET  /api/watchlist/entries       → {"entries":[...], "watched":[...]}
"""

import importlib

import pytest

from app import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store, stats, service
    importlib.reload(store)
    importlib.reload(stats)
    importlib.reload(service)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# GET /api/watchlist — backward compat: только channels, без entries           #
# --------------------------------------------------------------------------- #

def test_get_watchlist_empty_is_channels_only(client):
    """GET /api/watchlist пустой → {"channels": []} — строгое равенство."""
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    assert r.json() == {"channels": []}


def test_get_watchlist_has_channels_key(client):
    client.post("/api/watchlist", json={"channel": "news"})
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    assert r.json()["channels"] == ["news"]
    # "entries" НЕ должен присутствовать в GET /api/watchlist (backward compat)
    assert "entries" not in r.json()


# --------------------------------------------------------------------------- #
# POST /api/watchlist — telegram (backward compat)                             #
# --------------------------------------------------------------------------- #

def test_post_telegram_channel_backward_compat(client):
    """POST с platform=telegram → {"channels": [...]}."""
    r = client.post("/api/watchlist", json={"channel": "myChannel"})
    assert r.status_code == 200
    assert r.json()["channels"] == ["myChannel"]


def test_post_telegram_via_platform_field(client):
    r = client.post("/api/watchlist", json={"channel": "chan1", "platform": "telegram"})
    assert r.status_code == 200
    assert "chan1" in r.json()["channels"]


# --------------------------------------------------------------------------- #
# POST /api/watchlist — non-telegram platform                                  #
# --------------------------------------------------------------------------- #

def test_post_non_telegram_returns_channels_and_entries(client):
    """POST с platform=tiktok → ответ содержит channels И entries."""
    r = client.post("/api/watchlist", json={"target": "casino_tt", "platform": "tiktok"})
    assert r.status_code == 200
    body = r.json()
    assert "channels" in body
    assert "entries" in body
    # entries содержит добавленную запись
    entries = body["entries"]
    assert any(e["target"] == "casino_tt" and e["platform"] == "tiktok" for e in entries)


def test_post_youtube_entry(client):
    r = client.post("/api/watchlist", json={"target": "yt_brand", "platform": "youtube"})
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body
    assert any(e["platform"] == "youtube" for e in body["entries"])


def test_post_operator_entry(client):
    r = client.post("/api/watchlist", json={"target": "Olimpbet", "platform": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body
    assert any(e["target"] == "Olimpbet" and e["platform"] == "operator" for e in body["entries"])


def test_post_empty_target_returns_error(client):
    """Пустой target/channel → ошибка «укажите канал»."""
    r = client.post("/api/watchlist", json={"target": ""})
    assert r.status_code == 200
    assert "error" in r.json()
    assert "укажите" in r.json()["error"]


def test_post_unknown_platform_treated_as_telegram(client):
    """Неизвестная платформа → telegram-поведение."""
    r = client.post("/api/watchlist", json={"channel": "chan_x", "platform": "unknown_platform"})
    assert r.status_code == 200
    body = r.json()
    assert "channels" in body


# --------------------------------------------------------------------------- #
# DELETE /api/watchlist/{channel}                                              #
# --------------------------------------------------------------------------- #

def test_delete_telegram_channel(client):
    client.post("/api/watchlist", json={"channel": "del_me"})
    r = client.delete("/api/watchlist/del_me")
    assert r.status_code == 200
    assert r.json()["channels"] == []


def test_delete_non_telegram_with_platform_param(client):
    """DELETE с ?platform=tiktok удаляет non-telegram запись."""
    client.post("/api/watchlist", json={"target": "tt_user", "platform": "tiktok"})
    r = client.delete("/api/watchlist/tt_user?platform=tiktok")
    assert r.status_code == 200
    body = r.json()
    assert "channels" in body
    # entries не содержит удалённую запись
    if "entries" in body:
        assert not any(e["target"] == "tt_user" and e["platform"] == "tiktok"
                       for e in body["entries"])


# --------------------------------------------------------------------------- #
# POST /api/watchlist/scan — с записями разных платформ                       #
# --------------------------------------------------------------------------- #

def test_scan_with_non_telegram_entries_returns_entries_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store, stats, service
    importlib.reload(store)
    importlib.reload(stats)
    importlib.reload(service)

    store.add_entry("tt_user", "tiktok")

    def fake_scan_wl(conn=None):
        return {
            "added": 2, "flagged": 1,
            "channels": [],
            "entries": [{"target": "tt_user", "platform": "tiktok", "collected": 2, "flagged": 1}],
        }

    monkeypatch.setattr(service, "scan_watchlist", fake_scan_wl)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/watchlist/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == 2
        assert "entries" in body


# --------------------------------------------------------------------------- #
# GET /api/watchlist/stats — legacy, ТОЧНО как раньше                         #
# --------------------------------------------------------------------------- #

def test_stats_endpoint_legacy_exact_shape(client):
    """GET /api/watchlist/stats → {"channels": []} первоначально (точное равенство)."""
    r = client.get("/api/watchlist/stats")
    assert r.status_code == 200
    assert r.json() == {"channels": []}


# --------------------------------------------------------------------------- #
# GET /api/watchlist/entries — НОВЫЙ маршрут                                  #
# --------------------------------------------------------------------------- #

def test_entries_endpoint_exists(client):
    """GET /api/watchlist/entries → 200 и содержит watched и entries."""
    r = client.get("/api/watchlist/entries")
    assert r.status_code == 200
    body = r.json()
    assert "entries" in body
    assert "watched" in body


def test_entries_endpoint_shows_added_entries(client):
    """Добавленные non-telegram записи видны в GET /api/watchlist/entries."""
    client.post("/api/watchlist", json={"target": "kick_user", "platform": "kick"})
    r = client.get("/api/watchlist/entries")
    assert r.status_code == 200
    body = r.json()
    watched = body["watched"]
    assert any(e["target"] == "kick_user" and e["platform"] == "kick" for e in watched)


def test_entries_endpoint_initial_empty(client):
    """GET /api/watchlist/entries пустой watchlist → watched=[] entries=[]."""
    r = client.get("/api/watchlist/entries")
    assert r.status_code == 200
    assert r.json() == {"entries": [], "watched": []}
