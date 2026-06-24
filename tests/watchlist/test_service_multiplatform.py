"""Тесты мультиплатформенного scan_watchlist (TDD, Wave 2).

Все внешние вызовы монкипатчятся — сеть/БД не дёргаются.
DATA_DIR монкипатчится в tmp_path.

Проверяемые свойства:
  • dispatch по платформам (tiktok/youtube/twitch/kick/instagram/operator/telegram)
  • агрегация added/flagged (telegram-часть идёт через scan_telegram одним вызовом)
  • per-entry stats через record_entry_scan
  • оператор: licensed-флаг вычисляется и передаётся в stats
  • robustness: ошибка одной записи не обрывает весь скан
  • BACKWARD-COMPAT: telegram-only watchlist → result совпадает с test_scan_watchlist_aggregates
  • result содержит новый ключ "entries" со сводкой по всем записям
"""

import importlib

import pytest

from app import config


# --------------------------------------------------------------------------- #
# Fixture: изолированный DATA_DIR                                              #
# --------------------------------------------------------------------------- #

@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store
    importlib.reload(store)
    return store


@pytest.fixture
def svc(tmp_data, monkeypatch):
    """Перезагружает service с изолированным DATA_DIR."""
    from app.watchlist import service
    importlib.reload(service)
    return service


# --------------------------------------------------------------------------- #
# Вспомогательные заглушки                                                     #
# --------------------------------------------------------------------------- #

def _fake_scan_telegram(channels, conn=None, limit=12):
    """Возвращает 2 добавленных поста, 1 флаг, записи channels."""
    return {
        "added": 2,
        "flagged": 1,
        "channels": [{"channel": c, "fetched": 3, "added": 1, "flagged": 0} for c in channels],
    }


def _fake_list_account_posts_factory(n_posts=2, raises=False):
    def _fn(account_url, limit=12):
        if raises:
            raise RuntimeError("network error")
        return [
            {"url": f"https://tiktok.com/video/{i}", "title": f"post {i}",
             "description": f"desc {i}", "author_handle": "user", "thumb_url": "", "view_count": 0}
            for i in range(n_posts)
        ]
    return _fn


def _fake_search_youtube_factory(n=2):
    def _fn(query, limit=5):
        return [
            {"url": f"https://youtube.com/watch?v=vid{i}", "caption": f"title {i}",
             "platform": "youtube", "author_handle": "chan", "thumb_url": "", "view_count": 0}
            for i in range(n)
        ]
    return _fn


def _fake_fetch_twitch(channel, limit=12):
    return [{"url": f"https://twitch.tv/videos/{channel}_{i}", "title": f"stream {i}",
             "description": "", "author_handle": channel, "thumb_url": "", "view_count": 0, "live": False}
            for i in range(2)]


def _fake_fetch_kick(channel, limit=12):
    return [{"url": f"https://kick.com/video/k{i}", "title": f"kick {i}",
             "description": "", "author_handle": channel, "thumb_url": "", "view_count": 0, "live": False}
            for i in range(2)]


def _noop_ingest(conn, post, cap, score_text=None):
    """Заглушка _ingest: возвращает Score с risk=0 (ничего не флажится)."""
    from unittest.mock import MagicMock
    sc = MagicMock()
    sc.risk = 0
    sc.category = "clean"
    return sc


def _flagging_ingest(conn, post, cap, score_text=None):
    """Заглушка _ingest: risk=80 → всё флажится."""
    from unittest.mock import MagicMock
    sc = MagicMock()
    sc.risk = 80
    sc.category = "gambling"
    return sc


# --------------------------------------------------------------------------- #
# 1. Telegram-only watchlist остаётся совместимым                              #
# --------------------------------------------------------------------------- #

def test_telegram_only_backward_compat(tmp_data, svc, monkeypatch):
    """Telegram-only watchlist: added/flagged/channels из scan_telegram, entries добавлен."""
    tmp_data.add_channel("a")
    tmp_data.add_channel("b")

    monkeypatch.setattr(svc, "scan_telegram", _fake_scan_telegram)

    result = svc.scan_watchlist(conn=None)

    assert result["added"] == 2
    assert result["flagged"] == 1
    assert len(result["channels"]) == 2
    # новый ключ entries присутствует
    assert "entries" in result
    # telegram-записи попали в entries
    targets = {e["target"] for e in result["entries"]}
    assert "a" in targets and "b" in targets


def test_telegram_single_batch_call(tmp_data, svc, monkeypatch):
    """Все telegram-каналы передаются в scan_telegram ОДНИМ вызовом."""
    tmp_data.add_channel("x")
    tmp_data.add_channel("y")
    tmp_data.add_channel("z")

    calls = []

    def capturing_scan(channels, conn=None, limit=12):
        calls.append(list(channels))
        return {"added": 3, "flagged": 0, "channels": [{"channel": c, "fetched": 1, "added": 1, "flagged": 0} for c in channels]}

    monkeypatch.setattr(svc, "scan_telegram", capturing_scan)
    svc.scan_watchlist(conn=None)

    assert len(calls) == 1
    assert sorted(calls[0]) == ["x", "y", "z"]


# --------------------------------------------------------------------------- #
# 2. TikTok dispatch                                                           #
# --------------------------------------------------------------------------- #

def test_tiktok_dispatch_calls_list_account_posts(tmp_data, svc, monkeypatch):
    """tiktok-запись → fetch_mod.list_account_posts вызывается; added/entries обновляются."""
    tmp_data.add_entry("testuser", "tiktok")

    called_urls = []

    def fake_posts(account_url, limit=12):
        called_urls.append(account_url)
        return [{"url": "https://tiktok.com/video/1", "title": "t", "description": "d",
                 "author_handle": "testuser", "thumb_url": "", "view_count": 0}]

    monkeypatch.setattr(svc.fetch_mod, "list_account_posts", fake_posts)
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    result = svc.scan_watchlist(conn=None)

    assert any("tiktok" in u for u in called_urls)
    assert result["added"] >= 1
    entries = result["entries"]
    tiktok_entry = next((e for e in entries if e["platform"] == "tiktok"), None)
    assert tiktok_entry is not None
    assert tiktok_entry["target"] == "testuser"


# --------------------------------------------------------------------------- #
# 3. Instagram dispatch                                                        #
# --------------------------------------------------------------------------- #

def test_instagram_dispatch(tmp_data, svc, monkeypatch):
    tmp_data.add_entry("gram_user", "instagram")

    called = []

    def fake_posts(account_url, limit=12):
        called.append(account_url)
        return [{"url": "https://instagram.com/p/abc", "title": "ig", "description": "",
                 "author_handle": "gram_user", "thumb_url": "", "view_count": 0}]

    monkeypatch.setattr(svc.fetch_mod, "list_account_posts", fake_posts)
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    result = svc.scan_watchlist(conn=None)
    assert any("instagram" in u for u in called)
    assert any(e["platform"] == "instagram" for e in result["entries"])


# --------------------------------------------------------------------------- #
# 4. YouTube dispatch                                                          #
# --------------------------------------------------------------------------- #

def test_youtube_dispatch(tmp_data, svc, monkeypatch):
    tmp_data.add_entry("ютуб канал казино", "youtube")

    called_queries = []

    def fake_yt(query, limit=5):
        called_queries.append(query)
        return [{"url": "https://youtube.com/watch?v=abc", "caption": "t",
                 "platform": "youtube", "author_handle": "chan", "thumb_url": "", "view_count": 0}]

    monkeypatch.setattr(svc.youtube_mod, "search_youtube", fake_yt)
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    result = svc.scan_watchlist(conn=None)
    assert called_queries  # search_youtube был вызван
    assert any(e["platform"] == "youtube" for e in result["entries"])


# --------------------------------------------------------------------------- #
# 5. Twitch dispatch                                                           #
# --------------------------------------------------------------------------- #

def test_twitch_dispatch(tmp_data, svc, monkeypatch):
    tmp_data.add_entry("twitchstreamer", "twitch")

    called = []

    def fake_twitch(channel, limit=12):
        called.append(channel)
        return _fake_fetch_twitch(channel, limit)

    monkeypatch.setattr(svc.streaming_mod, "fetch_twitch_videos", fake_twitch)
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    result = svc.scan_watchlist(conn=None)
    assert "twitchstreamer" in called
    assert any(e["platform"] == "twitch" for e in result["entries"])


# --------------------------------------------------------------------------- #
# 6. Kick dispatch                                                             #
# --------------------------------------------------------------------------- #

def test_kick_dispatch(tmp_data, svc, monkeypatch):
    tmp_data.add_entry("kickstreamer", "kick")

    called = []

    def fake_kick(channel, limit=12):
        called.append(channel)
        return _fake_fetch_kick(channel, limit)

    monkeypatch.setattr(svc.streaming_mod, "fetch_kick_videos", fake_kick)
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    result = svc.scan_watchlist(conn=None)
    assert "kickstreamer" in called
    assert any(e["platform"] == "kick" for e in result["entries"])


# --------------------------------------------------------------------------- #
# 7. Operator dispatch: YouTube + TikTok + licensed flag                       #
# --------------------------------------------------------------------------- #

def test_operator_dispatch_youtube_and_tiktok(tmp_data, svc, monkeypatch):
    """Operator-запись → запускаем YouTube И TikTok поиск."""
    tmp_data.add_entry("1xbet", "operator")

    yt_called = []
    tt_called = []

    def fake_yt(query, limit=5):
        yt_called.append(query)
        return []

    def fake_posts(account_url, limit=12):
        tt_called.append(account_url)
        return []

    monkeypatch.setattr(svc.youtube_mod, "search_youtube", fake_yt)
    monkeypatch.setattr(svc.fetch_mod, "list_account_posts", fake_posts)
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    result = svc.scan_watchlist(conn=None)
    assert yt_called, "YouTube должен быть вызван для operator"
    assert tt_called, "TikTok должен быть вызван для operator"
    assert any(e["platform"] == "operator" for e in result["entries"])


def test_operator_licensed_flag_unlicensed(tmp_data, svc, monkeypatch):
    """1xbet — нелицензированный оператор: licensed=False в record_entry_scan."""
    tmp_data.add_entry("1xbet", "operator")

    monkeypatch.setattr(svc.youtube_mod, "search_youtube", lambda q, limit=5: [])
    monkeypatch.setattr(svc.fetch_mod, "list_account_posts", lambda u, limit=12: [])
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    recorded = []

    real_record = svc.stats.record_entry_scan

    def capturing_record(target, platform, added, flagged, scanned_at=None, licensed=None):
        recorded.append({"target": target, "platform": platform, "licensed": licensed})
        return real_record(target, platform, added, flagged, scanned_at=scanned_at, licensed=licensed)

    monkeypatch.setattr(svc.stats, "record_entry_scan", capturing_record)

    svc.scan_watchlist(conn=None)

    op_rec = next((r for r in recorded if r["platform"] == "operator"), None)
    assert op_rec is not None
    # 1xbet не лицензирован → False (или None — оба допустимы, главное not True)
    assert op_rec["licensed"] is not True


def test_operator_licensed_flag_licensed(tmp_data, svc, monkeypatch):
    """Olimpbet — лицензированный оператор: licensed=True в record_entry_scan."""
    tmp_data.add_entry("Olimpbet", "operator")

    monkeypatch.setattr(svc.youtube_mod, "search_youtube", lambda q, limit=5: [])
    monkeypatch.setattr(svc.fetch_mod, "list_account_posts", lambda u, limit=12: [])
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    recorded = []

    real_record = svc.stats.record_entry_scan

    def capturing_record(target, platform, added, flagged, scanned_at=None, licensed=None):
        recorded.append({"target": target, "platform": platform, "licensed": licensed})
        return real_record(target, platform, added, flagged, scanned_at=scanned_at, licensed=licensed)

    monkeypatch.setattr(svc.stats, "record_entry_scan", capturing_record)

    svc.scan_watchlist(conn=None)

    op_rec = next((r for r in recorded if r["platform"] == "operator"), None)
    assert op_rec is not None
    assert op_rec["licensed"] is True


# --------------------------------------------------------------------------- #
# 8. Per-entry stats: record_entry_scan вызывается для каждой записи          #
# --------------------------------------------------------------------------- #

def test_per_entry_stats_recorded(tmp_data, svc, monkeypatch):
    """record_entry_scan вызывается для каждой non-telegram записи."""
    tmp_data.add_entry("myuser", "tiktok")

    monkeypatch.setattr(svc.fetch_mod, "list_account_posts",
                        _fake_list_account_posts_factory(n_posts=1))
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    recorded = []
    real_rec = svc.stats.record_entry_scan

    def capturing(target, platform, added, flagged, scanned_at=None, licensed=None):
        recorded.append((target, platform, added))
        return real_rec(target, platform, added, flagged, scanned_at=scanned_at, licensed=licensed)

    monkeypatch.setattr(svc.stats, "record_entry_scan", capturing)

    svc.scan_watchlist(conn=None)

    tiktok_rec = [r for r in recorded if r[1] == "tiktok"]
    assert tiktok_rec, "record_entry_scan должен быть вызван для tiktok"
    assert tiktok_rec[0][0] == "myuser"


def test_telegram_per_entry_stats_recorded(tmp_data, svc, monkeypatch):
    """record_entry_scan вызывается и для telegram-записей."""
    tmp_data.add_channel("news_chan")

    monkeypatch.setattr(svc, "scan_telegram", lambda channels, conn=None, limit=12: {
        "added": 1, "flagged": 0,
        "channels": [{"channel": "news_chan", "fetched": 2, "added": 1, "flagged": 0}]
    })

    recorded = []
    real_rec = svc.stats.record_entry_scan

    def capturing(target, platform, added, flagged, scanned_at=None, licensed=None):
        recorded.append((target, platform))
        return real_rec(target, platform, added, flagged, scanned_at=scanned_at, licensed=licensed)

    monkeypatch.setattr(svc.stats, "record_entry_scan", capturing)

    svc.scan_watchlist(conn=None)

    tg_recs = [r for r in recorded if r[1] == "telegram"]
    assert tg_recs, "record_entry_scan должен вызываться и для telegram"


# --------------------------------------------------------------------------- #
# 9. Robustness: ошибка одной записи не обрывает весь скан                    #
# --------------------------------------------------------------------------- #

def test_one_platform_failure_does_not_abort_scan(tmp_data, svc, monkeypatch):
    """Если один platform сборщик бросает — остальные записи сканируются."""
    tmp_data.add_entry("boom_user", "tiktok")
    tmp_data.add_entry("ok_user", "youtube")

    def failing_posts(account_url, limit=12):
        raise RuntimeError("tiktok unavailable")

    yt_called = []

    def fake_yt(query, limit=5):
        yt_called.append(query)
        return []

    monkeypatch.setattr(svc.fetch_mod, "list_account_posts", failing_posts)
    monkeypatch.setattr(svc.youtube_mod, "search_youtube", fake_yt)
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    # НЕ должен бросить
    result = svc.scan_watchlist(conn=None)
    assert "added" in result
    assert yt_called, "youtube должен был сработать несмотря на ошибку tiktok"


def test_scan_never_raises_with_mixed_platforms(tmp_data, svc, monkeypatch):
    """scan_watchlist никогда не бросает, даже если ВСЕ платформы падают."""
    tmp_data.add_entry("x", "tiktok")
    tmp_data.add_entry("y", "youtube")
    tmp_data.add_entry("z", "twitch")

    def boom(*a, **kw):
        raise RuntimeError("все сломалось")

    monkeypatch.setattr(svc.fetch_mod, "list_account_posts", boom)
    monkeypatch.setattr(svc.youtube_mod, "search_youtube", boom)
    monkeypatch.setattr(svc.streaming_mod, "fetch_twitch_videos", boom)
    monkeypatch.setattr(svc, "_ingest_post", boom)

    result = svc.scan_watchlist(conn=None)
    assert isinstance(result, dict)
    assert "added" in result


# --------------------------------------------------------------------------- #
# 10. Flagging: added/flagged из non-telegram записей аддитивны               #
# --------------------------------------------------------------------------- #

def test_non_telegram_added_flagged_additive(tmp_data, svc, monkeypatch):
    """added/flagged включают вклад non-telegram записей поверх telegram."""
    # Только tiktok-запись
    tmp_data.add_entry("casino_tiktok", "tiktok")

    monkeypatch.setattr(svc.fetch_mod, "list_account_posts",
                        _fake_list_account_posts_factory(n_posts=3))
    monkeypatch.setattr(svc, "_ingest_post", _flagging_ingest)  # все флажатся

    result = svc.scan_watchlist(conn=None)
    # 3 поста добавлено, 3 флажено
    assert result["added"] >= 3
    assert result["flagged"] >= 3


# --------------------------------------------------------------------------- #
# 11. Пустой watchlist → пустой результат (backward compat)                  #
# --------------------------------------------------------------------------- #

def test_empty_watchlist_returns_empty(tmp_data, svc, monkeypatch):
    result = svc.scan_watchlist(conn=None)
    assert result == {"added": 0, "flagged": 0, "channels": [], "entries": []}


# --------------------------------------------------------------------------- #
# 12. Mixed: telegram + tiktok → telegram-channels в result["channels"]       #
# --------------------------------------------------------------------------- #

def test_mixed_channels_and_entries_in_result(tmp_data, svc, monkeypatch):
    """telegram-записи → result['channels'] (совместимость); все → result['entries']."""
    tmp_data.add_channel("tg_chan")
    tmp_data.add_entry("tt_user", "tiktok")

    monkeypatch.setattr(svc, "scan_telegram", _fake_scan_telegram)
    monkeypatch.setattr(svc.fetch_mod, "list_account_posts",
                        _fake_list_account_posts_factory(n_posts=1))
    monkeypatch.setattr(svc, "_ingest_post", _noop_ingest)

    result = svc.scan_watchlist(conn=None)

    # channels содержит telegram-ряды
    assert any(ch.get("channel") == "tg_chan" for ch in result["channels"])
    # entries содержит и telegram и tiktok
    platforms = {e["platform"] for e in result["entries"]}
    assert "telegram" in platforms
    assert "tiktok" in platforms
