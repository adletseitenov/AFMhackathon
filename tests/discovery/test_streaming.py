"""Тесты автопоиска нежелательного (гемблинг) контента на TWITCH и KICK.

Сеть замокана (monkeypatch seam'ов streaming.fetch_kick_videos /
streaming.fetch_twitch_videos и score_post). Проверяем: ветка discover для
platform=twitch/kick реально ингестит видео стримеров в ленту с правильными
url/platform/source='discovered' и проставленным view_count; платформенные
сборщики РОБАСТНЫ (возвращают [] при любой ошибке, не пробрасывают).
"""

import app.discovery.discover as discovery
import app.discovery.streaming as streaming
from app import config, db
from app.models import Score


def _fake_yt(q, limit):
    """Заглушка YouTube-поиска: один промо-казино ролик (не должен использоваться
    для platform=twitch/kick — ytsearch там не вызывается)."""
    return [{
        "platform": "youtube",
        "url": "https://www.youtube.com/watch?v=YTSHOULDSKIP",
        "video_id": "YTSHOULDSKIP",
        "author_handle": "@x",
        "caption": "Промокод казино занос",
        "thumb_url": "https://i.ytimg.com/vi/YTSHOULDSKIP/hqdefault.jpg",
    }]


def _fake_score(post, extracted, conn=None):
    """Скор по тексту (единственный seam): казино-термины -> высокий риск gambling,
    иначе низкий fraud. Сохраняем скор в БД ровно как настоящий score_post."""
    blob = (extracted.combined_text or "").lower()
    if any(w in blob for w in ("казино", "занос", "stake", "roobet",
                               "bonus", "slot", "1xbet", "casino")):
        sc = Score(post_id=post.id, risk=90, category="gambling",
                   class_probs={}, top_features=[])
    else:
        sc = Score(post_id=post.id, risk=10, category="fraud",
                   class_probs={}, top_features=[])
    if conn is not None:
        db.upsert_score(conn, sc, config.action_for_risk(sc.risk), "")
    return sc


# --- streaming collectors are robust: [] on any error, never raise ---

def test_fetch_kick_videos_empty_on_failure(monkeypatch):
    # curl_cffi.requests.get кидает — сборщик должен проглотить и вернуть [].
    import sys
    import types
    boom = types.ModuleType("curl_cffi")
    boom_requests = types.ModuleType("curl_cffi.requests")

    def _boom(*a, **k):
        raise RuntimeError("network down")

    boom_requests.get = _boom
    boom.requests = boom_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", boom)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", boom_requests)
    assert streaming.fetch_kick_videos("roshtein", limit=5) == []


def test_fetch_twitch_videos_empty_on_failure(monkeypatch):
    import sys
    import types

    class _BoomYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise RuntimeError("extractor failed")

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _BoomYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    assert streaming.fetch_twitch_videos("xposed", limit=5) == []


def test_fetch_kick_videos_maps_vod_fields(monkeypatch):
    """curl_cffi отдаёт СПИСОК VOD-ов; маппим в нормализованный dict, элементы без
    video.uuid пропускаем."""
    import sys
    import types

    class _Resp:
        def json(self):
            return [
                {"session_title": "BONUS HUNT on STAKE $5000",
                 "views": 1234,
                 "video": {"uuid": "uuid-1"},
                 "thumbnail": {"src": "https://kick.thumb/1.jpg"}},
                {"session_title": "no uuid here", "views": 5,
                 "video": {}, "thumbnail": {"src": "x"}},  # без uuid -> пропуск
            ]

    fake = types.ModuleType("curl_cffi")
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.get = lambda url, **k: _Resp()
    fake.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    out = streaming.fetch_kick_videos("roshtein", limit=5)
    assert len(out) == 1  # элемент без uuid отброшен
    v = out[0]
    assert v["url"] == "https://kick.com/video/uuid-1"
    assert v["title"] == "BONUS HUNT on STAKE $5000"
    assert v["description"] == "BONUS HUNT on STAKE $5000"
    assert v["author_handle"] == "roshtein"
    assert v["thumb_url"] == "https://kick.thumb/1.jpg"
    assert v["view_count"] == 1234


def test_fetch_twitch_videos_maps_entries(monkeypatch):
    import sys
    import types

    class _YDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"entries": [
                {"url": "https://www.twitch.tv/videos/111",
                 "title": "Slots BONUS HUNT stream",
                 "view_count": 999,
                 "thumbnails": [{"url": "https://twitch.thumb/lo.jpg"},
                                {"url": "https://twitch.thumb/hi.jpg"}]},
            ]}

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _YDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    out = streaming.fetch_twitch_videos("xposed", limit=5)
    assert len(out) == 1
    v = out[0]
    assert v["url"] == "https://www.twitch.tv/videos/111"
    assert v["title"] == "Slots BONUS HUNT stream"
    assert v["description"] == "Slots BONUS HUNT stream"
    assert v["author_handle"] == "xposed"
    assert v["thumb_url"] == "https://twitch.thumb/hi.jpg"  # последний thumbnail
    assert v["view_count"] == 999


# --- discover() branches for platform=kick / platform=twitch ---

def test_platform_kick_ingests_streamer_vods(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kick.db")
    conn = db.connect()
    db.init_db(conn)
    import app.discovery.discover as d
    monkeypatch.setattr(d, "score_post", _fake_score)
    monkeypatch.setattr(d, "_KICK_SEED_STREAMERS", ["roshtein"])  # один стример

    calls = {"streamers": []}

    def _fake_kick(channel, limit=12):
        calls["streamers"].append(channel)
        return [{"url": "https://kick.com/video/uuid-1",
                 "title": "BONUS HUNT on STAKE $5000",
                 "description": "BONUS HUNT on STAKE $5000",
                 "author_handle": channel,
                 "thumb_url": "https://kick.thumb/1.jpg",
                 "view_count": 1234}]

    monkeypatch.setattr(streaming, "fetch_kick_videos", _fake_kick)

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="kick")
    assert calls["streamers"] == ["roshtein"]
    assert res["platform"] == "kick"
    assert res["platform_added"] == 1
    assert res["youtube_added"] == 0  # ytsearch не используется для kick
    assert res["added"] == 1
    assert res["flagged"] >= 1  # казино-заголовок -> эскалация
    rows = conn.execute(
        "SELECT url, platform, source, view_count FROM posts").fetchall()
    assert rows[0]["url"] == "https://kick.com/video/uuid-1"
    assert rows[0]["platform"] == "kick"
    assert rows[0]["source"] == "discovered"
    assert rows[0]["view_count"] == 1234


def test_platform_twitch_ingests_streamer_vods(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "twitch.db")
    conn = db.connect()
    db.init_db(conn)
    import app.discovery.discover as d
    monkeypatch.setattr(d, "score_post", _fake_score)
    monkeypatch.setattr(d, "_TWITCH_SEED_STREAMERS", ["xposed", "roshtein"])

    def _fake_twitch(channel, limit=12):
        return [{"url": f"https://www.twitch.tv/videos/{channel}-1",
                 "title": "Slots BONUS HUNT stream",
                 "description": "Slots BONUS HUNT stream",
                 "author_handle": channel,
                 "thumb_url": "https://twitch.thumb/hi.jpg",
                 "view_count": 999}]

    monkeypatch.setattr(streaming, "fetch_twitch_videos", _fake_twitch)

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="twitch")
    assert res["platform"] == "twitch"
    assert res["platform_added"] == 2  # два стримера -> два разных url
    assert res["youtube_added"] == 0
    assert res["added"] == 2
    assert res["flagged"] >= 1
    rows = conn.execute(
        "SELECT url, platform, source, view_count FROM posts ORDER BY url").fetchall()
    assert all(r["platform"] == "twitch" for r in rows)
    assert all(r["source"] == "discovered" for r in rows)
    assert all(r["url"].startswith("https://www.twitch.tv/videos/") for r in rows)
    assert all(r["view_count"] == 999 for r in rows)


def test_platform_kick_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kickdup.db")
    conn = db.connect()
    db.init_db(conn)
    import app.discovery.discover as d
    monkeypatch.setattr(d, "score_post", _fake_score)
    monkeypatch.setattr(d, "_KICK_SEED_STREAMERS", ["roshtein"])
    monkeypatch.setattr(streaming, "fetch_kick_videos",
                        lambda channel, limit=12: [{
                            "url": "https://kick.com/video/dup",
                            "title": "STAKE casino занос", "description": "STAKE casino занос",
                            "author_handle": channel, "thumb_url": "",
                            "view_count": 7}])
    discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                       with_telegram=False, platform="kick")
    res2 = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                              with_telegram=False, platform="kick")
    assert res2["platform_added"] == 0  # тот же url не дублируется


def test_platform_twitch_skips_youtube_and_telegram(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tw_skip.db")
    conn = db.connect()
    db.init_db(conn)
    import app.discovery.discover as d
    monkeypatch.setattr(d, "score_post", _fake_score)
    monkeypatch.setattr(d, "_TWITCH_SEED_STREAMERS", ["xposed"])
    monkeypatch.setattr(streaming, "fetch_twitch_videos",
                        lambda channel, limit=12: [])

    def _boom_yt(q, limit):
        raise AssertionError("youtube must not run for platform=twitch")

    called = {"tg": False}

    def _boom_tg(channels, conn=None):
        called["tg"] = True
        return {"added": 5, "flagged": 5, "channels": []}

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_boom_yt,
                             with_telegram=True, scan_tg=_boom_tg, platform="twitch")
    assert called["tg"] is False
    assert res["youtube_added"] == 0
    assert res["telegram_added"] == 0
