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


# --- Twitch: фоллбэк на КЛИПЫ когда /videos пуст (VOD истекли) ---

def _ydl_factory(routes):
    """Строит фейковый yt_dlp-модуль: routes = {substr_in_url: info_dict|Exception}.
    extract_info матчит по подстроке url; нет совпадения -> {} (пустой плейлист)."""
    import types

    class _YDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            for sub, info in routes.items():
                if sub in url:
                    if isinstance(info, Exception):
                        raise info
                    return info
            return {}

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _YDL
    return fake


def test_fetch_twitch_videos_falls_back_to_clips(monkeypatch):
    """Когда /videos пуст (entries=[]), сборщик пробует /clips и маппит их."""
    import sys

    routes = {
        "/videos": {"entries": []},  # VOD истекли
        "/clips": {"entries": [
            {"url": "https://clips.twitch.tv/AbcClip",
             "title": "Big slots win STAKE",
             "view_count": 321,
             "thumbnails": [{"url": "https://twitch.thumb/clip.jpg"}]},
        ]},
    }
    monkeypatch.setitem(sys.modules, "yt_dlp", _ydl_factory(routes))
    out = streaming.fetch_twitch_videos("xposed", limit=5)
    assert len(out) == 1
    v = out[0]
    assert v["url"] == "https://clips.twitch.tv/AbcClip"
    assert v["title"] == "Big slots win STAKE"
    assert v["author_handle"] == "xposed"
    assert v["thumb_url"] == "https://twitch.thumb/clip.jpg"
    assert v["view_count"] == 321
    assert v.get("live") in (False, None)


def test_fetch_twitch_videos_prefers_vods_over_clips(monkeypatch):
    """Когда /videos НЕпуст, клипы не запрашиваются (VOD приоритетнее)."""
    import sys

    routes = {
        "/videos": {"entries": [
            {"url": "https://www.twitch.tv/videos/777",
             "title": "VOD slots", "view_count": 10, "thumbnails": []},
        ]},
        "/clips": {"entries": [
            {"url": "https://clips.twitch.tv/SHOULDNOTSHOW",
             "title": "clip", "view_count": 1, "thumbnails": []},
        ]},
    }
    monkeypatch.setitem(sys.modules, "yt_dlp", _ydl_factory(routes))
    out = streaming.fetch_twitch_videos("xposed", limit=5)
    assert len(out) == 1
    assert out[0]["url"] == "https://www.twitch.tv/videos/777"


# --- fetch_instagram_posts (НОВ, best-effort) ---

def test_fetch_instagram_posts_empty_on_failure(monkeypatch):
    """Логин-вол/ошибка yt-dlp -> [] (вызывающий покажет честную ноту)."""
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
            raise RuntimeError("login required")

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _BoomYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    assert streaming.fetch_instagram_posts("nasa", limit=5) == []


def test_fetch_instagram_posts_maps_entries(monkeypatch):
    """Публичные reels/посты -> нормализованный dict (live=False)."""
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
                {"url": "https://www.instagram.com/reel/AAA/",
                 "title": "casino promo reel",
                 "description": "занос казино 1xbet",
                 "uploader": "scam_acc",
                 "view_count": 555,
                 "thumbnails": [{"url": "https://ig.thumb/a.jpg"}]},
                {"webpage_url": "https://www.instagram.com/p/BBB/",
                 "title": "", "description": "",
                 "view_count": None, "thumbnails": []},
            ]}

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _YDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    out = streaming.fetch_instagram_posts("scam_acc", limit=5)
    assert len(out) == 2
    v = out[0]
    assert v["url"] == "https://www.instagram.com/reel/AAA/"
    assert v["title"] == "casino promo reel"
    assert v["description"] == "занос казино 1xbet"
    assert v["author_handle"] == "scam_acc"
    assert v["thumb_url"] == "https://ig.thumb/a.jpg"
    assert v["view_count"] == 555
    assert v["live"] is False
    # второй пост: пустой view_count -> 0, url из webpage_url
    assert out[1]["url"] == "https://www.instagram.com/p/BBB/"
    assert out[1]["view_count"] == 0


# --- fetch_live (НОВ): детект прямых эфиров по площадкам ---

def test_fetch_live_twitch_online(monkeypatch):
    """twitch:stream отдаёт инфо живого эфира -> dict с live=True."""
    import sys

    routes = {"twitch.tv/roshtein": {
        "title": "LIVE BONUS HUNT $10000",
        "is_live": True,
        "view_count": 4200,
        "uploader": "Roshtein",
        "thumbnail": "https://twitch.thumb/live.jpg",
        "webpage_url": "https://www.twitch.tv/roshtein",
    }}
    monkeypatch.setitem(sys.modules, "yt_dlp", _ydl_factory(routes))
    d = streaming.fetch_live("twitch", "roshtein")
    assert d is not None
    assert d["live"] is True
    assert d["title"] == "LIVE BONUS HUNT $10000"
    assert d["author_handle"] == "roshtein"
    assert d["view_count"] == 4200
    assert d["url"] == "https://www.twitch.tv/roshtein"
    assert d["thumb_url"] == "https://twitch.thumb/live.jpg"


def test_fetch_live_twitch_offline_returns_none(monkeypatch):
    """Оффлайн ('not currently live')/ошибка -> None."""
    import sys

    routes = {"twitch.tv/xposed": RuntimeError(
        "xposed is offline: The channel is not currently live")}
    monkeypatch.setitem(sys.modules, "yt_dlp", _ydl_factory(routes))
    assert streaming.fetch_live("twitch", "xposed") is None


def test_fetch_live_kick_online(monkeypatch):
    """Kick API: поле livestream непусто -> dict live=True (title=session_title)."""
    import sys
    import types

    class _Resp:
        def json(self):
            return {"slug": "roshtein", "livestream": {
                "session_title": "BONUS HUNT live STAKE",
                "viewer_count": 8800,
                "thumbnail": {"url": "https://kick.thumb/live.jpg"},
            }}

    fake = types.ModuleType("curl_cffi")
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.get = lambda url, **k: _Resp()
    fake.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    d = streaming.fetch_live("kick", "roshtein")
    assert d is not None
    assert d["live"] is True
    assert d["title"] == "BONUS HUNT live STAKE"
    assert d["view_count"] == 8800
    assert d["author_handle"] == "roshtein"
    assert d["url"] == "https://kick.com/roshtein"
    assert d["thumb_url"] == "https://kick.thumb/live.jpg"


def test_fetch_live_kick_offline_returns_none(monkeypatch):
    """Kick: livestream=null (оффлайн) -> None."""
    import sys
    import types

    class _Resp:
        def json(self):
            return {"slug": "xposed", "livestream": None}

    fake = types.ModuleType("curl_cffi")
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.get = lambda url, **k: _Resp()
    fake.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    assert streaming.fetch_live("kick", "xposed") is None


def test_fetch_live_kick_error_returns_none(monkeypatch):
    """Kick: сеть упала -> None (не пробрасывает)."""
    import sys
    import types

    def _boom(*a, **k):
        raise RuntimeError("network down")

    fake = types.ModuleType("curl_cffi")
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.get = _boom
    fake.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    assert streaming.fetch_live("kick", "roshtein") is None


def test_fetch_live_tiktok_online(monkeypatch):
    """tiktok:live отдаёт инфо эфира -> dict live=True."""
    import sys

    routes = {"tiktok.com/@casinoman/live": {
        "title": "Казино прямой эфир занос",
        "view_count": 1200,
        "uploader": "casinoman",
        "thumbnail": "https://tt.thumb/live.jpg",
        "webpage_url": "https://www.tiktok.com/@casinoman/live",
    }}
    monkeypatch.setitem(sys.modules, "yt_dlp", _ydl_factory(routes))
    d = streaming.fetch_live("tiktok", "casinoman")
    assert d is not None
    assert d["live"] is True
    assert d["title"] == "Казино прямой эфир занос"
    assert d["author_handle"] == "casinoman"
    assert d["view_count"] == 1200


def test_fetch_live_tiktok_offline_returns_none(monkeypatch):
    """TikTok оффлайн -> None."""
    import sys

    routes = {"tiktok.com/@x/live": RuntimeError(
        "The user is not currently live")}
    monkeypatch.setitem(sys.modules, "yt_dlp", _ydl_factory(routes))
    assert streaming.fetch_live("tiktok", "x") is None


def test_fetch_live_instagram_best_effort_none(monkeypatch):
    """Instagram закрыт логин-волом -> обычно None (best-effort)."""
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
            raise RuntimeError("login required")

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _BoomYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    assert streaming.fetch_live("instagram", "someacc") is None


def test_fetch_live_unknown_platform_returns_none():
    """Неизвестная площадка -> None (не падает)."""
    assert streaming.fetch_live("myspace", "anyone") is None


def test_search_twitch_live_parses_gql_streams(monkeypatch):
    """search_twitch_live: публичный Twitch GraphQL отдаёт live-стримы категории ->
    нормализованные dict(live=True). curl_cffi.requests.post замокан."""
    import sys
    import types

    class _Resp:
        def json(self):
            return {"data": {"game": {"streams": {"edges": [
                {"node": {"title": "BONUS HUNT !stake", "viewersCount": 1500,
                          "broadcaster": {"login": "casinoguy", "displayName": "CasinoGuy"}}},
                {"node": {"title": "slots night", "viewersCount": 80,
                          "broadcaster": {"login": "slotsdude", "displayName": "SlotsDude"}}},
            ]}}}}

    fake = types.ModuleType("curl_cffi")
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.post = lambda url, **k: _Resp()
    fake.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    out = streaming.search_twitch_live(categories=["Slots"], per_category=5, max_live=6)
    assert len(out) == 2
    s = out[0]
    assert s["url"] == "https://www.twitch.tv/casinoguy"
    assert s["author_handle"] == "casinoguy"
    assert s["title"] == "BONUS HUNT !stake"
    assert s["view_count"] == 1500
    assert s["live"] is True


def test_search_twitch_live_empty_on_failure(monkeypatch):
    import sys
    import types

    def _boom(*a, **k):
        raise RuntimeError("net down")

    fake = types.ModuleType("curl_cffi")
    fake_requests = types.ModuleType("curl_cffi.requests")
    fake_requests.post = _boom
    fake.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)
    assert streaming.search_twitch_live() == []
