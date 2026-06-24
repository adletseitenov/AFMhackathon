"""Тесты движка автономного поиска (сеть замокана). Проверяем: реальные ссылки в
ленте, скоринг своей моделью, идемпотентность, парс YouTube video id."""

import urllib.parse

import app.discovery.discover as discovery
import app.discovery.web as web
from app import config, db
from app.discovery.youtube import _video_id, search_youtube


def _fake_yt(q, limit):
    # реальный по форме результат: настоящий watch?v= url + промо-казино заголовок
    vid = ("ID%011d" % (abs(hash(q)) % 10**11))[:11]
    return [{
        "platform": "youtube",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "video_id": vid,
        "author_handle": "@casino_promo",
        "caption": "Промокод казино 1xBet занос! Гарантированный бонус сегодня, забери вдвое",
        "thumb_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
    }]


def test_discover_ingests_real_links_and_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "d.db")
    conn = db.connect(); db.init_db(conn)
    res = discovery.discover(conn, queries=["q1", "q2"], per_query=1,
                             search_yt=_fake_yt, with_telegram=False)
    assert res["youtube_added"] == 2  # два разных запроса -> два разных url
    rows = conn.execute("SELECT url, source, thumb_url FROM posts").fetchall()
    assert all(r["url"].startswith("https://www.youtube.com/watch?v=") for r in rows)
    assert all(r["source"] == "discovered" for r in rows)
    assert all(r["thumb_url"] and "i.ytimg.com" in r["thumb_url"] for r in rows)
    assert res["flagged"] >= 1  # промо-казино -> эскалация


def test_discover_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "d2.db")
    conn = db.connect(); db.init_db(conn)
    discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt, with_telegram=False)
    res2 = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt, with_telegram=False)
    assert res2["youtube_added"] == 0  # тот же url не дублируется


def test_discover_telegram_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "d3.db")
    conn = db.connect(); db.init_db(conn)
    import app.discovery.discover as d
    monkeypatch.setattr(d, "discover_telegram_channels", lambda q, limit=3: ["chan_a"])
    fake_scan = lambda channels, conn=None: {"added": 3, "flagged": 2, "channels": []}
    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                             with_telegram=True, scan_tg=fake_scan)
    assert res["telegram_added"] == 3 and res["added"] >= 4


def test_youtube_video_id_parse():
    assert _video_id({"url": "https://www.youtube.com/watch?v=abcdefghijk"}) == "abcdefghijk"
    assert _video_id({"id": "abcdefghijk"}) == "abcdefghijk"
    assert _video_id({"url": "x", "id": "short"}) == ""


# --- New: queries breadth, cross-query dedup, by_category, web parse ---

def _fixed_yt(q, limit):
    # Все запросы возвращают ОДИН и тот же url -> проверяем кросс-запросный дедуп.
    return [{
        "platform": "youtube",
        "url": "https://www.youtube.com/watch?v=DUP00000001",
        "video_id": "DUP00000001",
        "author_handle": "@casino_promo",
        "caption": "Промокод казино 1xBet занос! Гарантированный бонус, забери вдвое",
        "thumb_url": "https://i.ytimg.com/vi/DUP00000001/hqdefault.jpg",
    }]


def test_queries_breadth_and_uniqueness():
    from app.discovery.queries import DISCOVERY_QUERIES
    assert 15 <= len(DISCOVERY_QUERIES) <= 25
    assert len(set(DISCOVERY_QUERIES)) == len(DISCOVERY_QUERIES)  # без дублей
    blob = " ".join(DISCOVERY_QUERIES).lower()
    for brand in ("1xbet", "mostbet", "melbet", "pinup", "vavada", "1win"):
        assert brand in blob
    assert "бездепозитный" in blob and "пассивный доход" in blob


def test_discover_cross_query_dedup_and_by_category(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "dc.db")
    conn = db.connect(); db.init_db(conn)
    res = discovery.discover(conn, queries=["q1", "q2", "q3"], per_query=1,
                             search_yt=_fixed_yt, with_telegram=False)
    # один и тот же url по трём запросам -> добавлен ровно один раз
    assert res["youtube_added"] == 1
    assert "by_category" in res and isinstance(res["by_category"], dict)
    assert set(res["by_category"]) == {"gambling", "pyramid", "fraud"}
    # промо-казино заголовок -> категория gambling
    assert res["by_category"]["gambling"] == 1
    assert sum(res["by_category"].values()) == res["youtube_added"]


def test_youtube_dedup_by_video_id(monkeypatch):
    class _FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, q, download=False):
            return {"entries": [
                {"id": "abcdefghijk", "title": "t1"},
                {"id": "abcdefghijk", "title": "dup"},  # дубль того же id
                {"id": "lmnopqrstuv", "title": "t2"},
            ]}
    import sys, types
    fake = types.ModuleType("yt_dlp"); fake.YoutubeDL = _FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    out = search_youtube("q", 5)
    vids = [o["video_id"] for o in out]
    assert vids == ["abcdefghijk", "lmnopqrstuv"]  # дубль отброшен
    assert all("i.ytimg.com" in o["thumb_url"] for o in out)


def _ddg_html(names):
    """Сэмпл выдачи DuckDuckGo: t.me/<name> внутри uddg-редиректа (как в реале)."""
    links = []
    for nm in names:
        target = urllib.parse.quote(f"https://t.me/{nm}", safe="")
        links.append(
            f'<a class="result__a" href="//duckduckgo.com/l/?uddg={target}&rut=xyz">{nm}</a>'
        )
    return "<html><body>" + "".join(links) + "</body></html>"


def test_web_parse_extracts_channels_from_uddg(monkeypatch):
    html = _ddg_html(["scam_casino_kz", "pyramid_invest", "scam_casino_kz", "share"])
    monkeypatch.setattr(web, "_http_get", lambda url: html)
    chans = web.discover_telegram_channels("казино", limit=10)
    assert "scam_casino_kz" in chans
    assert "pyramid_invest" in chans
    assert chans.count("scam_casino_kz") == 1  # дедуп
    assert "share" not in chans  # служебный путь отсеян


def test_web_parse_raw_fallback_and_cap(monkeypatch):
    # Прямые (не-редирект) ссылки t.me — должны ловиться fallback-регуляркой.
    raw = "see https://t.me/alpha_chan and t.me/beta_chan and t.me/gamma_chan today"
    monkeypatch.setattr(web, "_http_get", lambda url: raw)
    chans = web.discover_telegram_channels("крипто", limit=2)
    assert len(chans) == 2  # per-call cap соблюдён
    assert set(chans) <= {"alpha_chan", "beta_chan", "gamma_chan"}


def test_web_parse_empty_on_failure(monkeypatch):
    def _boom(url):
        raise RuntimeError("network down")
    monkeypatch.setattr(web, "_http_get", _boom)
    assert web.discover_telegram_channels("q", limit=5) == []


# --- Platform scope + deep multimodal analysis ---

from app.models import Extracted, Post, Score, VisualConcept  # noqa: E402


def _fake_score(post, extracted, conn=None):
    """Скор по тексту: казино/занос -> высокий риск gambling, иначе низкий fraud.

    Мокаем единственный seam (классификатор/персист), но СОХРАНЯЕМ скор в БД ровно
    как настоящий score_post — иначе тесты на содержимое таблицы scores бессмысленны.
    """
    blob = (extracted.combined_text or "").lower()
    if any(w in blob for w in ("казино", "занос", "ставк", "1xbet", "casino")):
        sc = Score(post_id=post.id, risk=90, category="gambling",
                   class_probs={}, top_features=[])
    else:
        sc = Score(post_id=post.id, risk=10, category="fraud",
                   class_probs={}, top_features=[])
    if conn is not None:
        db.upsert_score(conn, sc, config.action_for_risk(sc.risk), "")
    return sc


def test_platform_youtube_skips_telegram(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "py.db")
    conn = db.connect(); db.init_db(conn)
    import app.discovery.discover as d
    monkeypatch.setattr(d, "score_post", _fake_score)
    called = {"tg": False}

    def _boom_tg(channels, conn=None):
        called["tg"] = True
        return {"added": 5, "flagged": 5, "channels": []}

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                             with_telegram=True, scan_tg=_boom_tg, platform="youtube")
    assert called["tg"] is False  # telegram-ветка не запускалась
    assert res["telegram_added"] == 0
    assert res["youtube_added"] == 1
    assert res["platform"] == "youtube"


def test_platform_telegram_skips_youtube(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pt.db")
    conn = db.connect(); db.init_db(conn)
    import app.discovery.discover as d
    monkeypatch.setattr(d, "discover_telegram_channels", lambda q, limit=3: ["chan_a"])
    fake_scan = lambda channels, conn=None: {"added": 4, "flagged": 1, "channels": []}

    def _boom_yt(q, limit):
        raise AssertionError("youtube must not be called for platform=telegram")

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_boom_yt,
                             with_telegram=True, scan_tg=fake_scan, platform="telegram")
    assert res["youtube_added"] == 0
    assert res["telegram_added"] == 4


def test_platform_tiktok_uses_account_listing_and_meta(tmp_path, monkeypatch):
    # tiktok-поиск идёт через РЕАЛЬНЫЙ yt-dlp по курируемым аккаунтам:
    # list_account_videos (лента) -> extract_meta (метаданные) -> скор по «<хэндл> <описание>».
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tt.db")
    conn = db.connect(); db.init_db(conn)
    import app.discovery.discover as d
    import app.ingestion.fetch as fetch_mod
    monkeypatch.setattr(d, "score_post", _fake_score)
    monkeypatch.setattr(d, "_TIKTOK_SEED_ACCOUNTS", ["promo"])  # один аккаунт — детерминизм

    calls = {"accounts": []}

    def _fake_posts(account_url, limit=12):
        calls["accounts"].append(account_url)
        return [{"url": "https://www.tiktok.com/@promo/video/123",
                 "title": "Казино занос #промокод", "description": "Казино занос #промокод",
                 "author_handle": "promo", "thumb_url": "https://thumb/x.jpg",
                 "view_count": 1000}]

    monkeypatch.setattr(fetch_mod, "list_account_posts", _fake_posts)

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="tiktok")
    assert calls["accounts"] == ["https://www.tiktok.com/@promo"]
    assert res["platform_added"] == 1
    assert res["youtube_added"] == 0  # ytsearch не используется для tiktok
    assert res["added"] == 1
    rows = conn.execute("SELECT url, platform, source FROM posts").fetchall()
    assert rows[0]["url"] == "https://www.tiktok.com/@promo/video/123"
    assert rows[0]["platform"] == "tiktok"
    assert rows[0]["source"] == "discovered"
    assert res["flagged"] >= 1  # казино-подпись + хэндл -> эскалация


def test_deep_triggers_fetch_extract_and_rescore(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "deep.db")
    conn = db.connect(); db.init_db(conn)
    import app.discovery.discover as d
    import app.extractors.pipeline as pipeline_mod
    import app.ingestion.fetch as fetch_mod

    # innocent-looking title -> low risk on text; deep audio/ocr reveals casino promo
    def _innocent_yt(q, limit):
        return [{
            "platform": "youtube",
            "url": "https://www.youtube.com/watch?v=INNOCENT001",
            "video_id": "INNOCENT001",
            "author_handle": "@vlog",
            "caption": "Мой обычный влог про природу",
            "thumb_url": "https://i.ytimg.com/vi/INNOCENT001/hqdefault.jpg",
        }]

    monkeypatch.setattr(d, "score_post", _fake_score)

    fetch_calls = {"link": []}

    def _fake_fetch_link(url):
        fetch_calls["link"].append(url)
        return ("/tmp/media.mp4", [], {"caption": ""})

    extract_calls = {"n": 0}

    def _fake_extract(post, use_cache=True, progress=None):
        extract_calls["n"] += 1
        assert post.media_path == "/tmp/media.mp4"  # реально качали видео
        # аудио/экран раскрывают казино-промо, которого нет в заголовке
        combined = post.caption + " занос казино промокод 1xbet"
        return Extracted(
            post_id=post.id, caption=post.caption,
            transcript="забери занос казино промокод", ocr_text="1xbet бонус",
            visual_concepts=[VisualConcept(label="casino", score=0.9)],
            combined_text=combined, entities=[],
        )

    monkeypatch.setattr(fetch_mod, "fetch_link", _fake_fetch_link)
    monkeypatch.setattr(pipeline_mod, "extract", _fake_extract)

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_innocent_yt,
                             with_telegram=False, platform="youtube",
                             deep=True, deep_top=3)
    assert fetch_calls["link"] == ["https://www.youtube.com/watch?v=INNOCENT001"]
    assert extract_calls["n"] == 1
    assert res["deep_analyzed"] == 1
    # пере-скор поднял риск -> пост теперь во flagged (раньше был чистым по заголовку)
    assert res["flagged"] >= 1
    score_row = conn.execute("SELECT risk, category FROM scores").fetchone()
    assert score_row["risk"] == 90 and score_row["category"] == "gambling"
    # combined_text с транскриптом/OCR сохранён в extracted
    ex_row = conn.execute("SELECT combined_text FROM extracted").fetchone()
    assert "занос" in ex_row["combined_text"] and "казино" in ex_row["combined_text"]


def test_deep_swallows_download_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "deepfail.db")
    conn = db.connect(); db.init_db(conn)
    import app.discovery.discover as d
    import app.extractors.pipeline as pipeline_mod
    import app.ingestion.fetch as fetch_mod
    monkeypatch.setattr(d, "score_post", _fake_score)

    def _boom_link(url):
        raise RuntimeError("download failed")

    def _must_not_extract(post, use_cache=True, progress=None):
        raise AssertionError("extract must not run when download fails")

    monkeypatch.setattr(fetch_mod, "fetch_link", _boom_link)
    monkeypatch.setattr(pipeline_mod, "extract", _must_not_extract)

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="youtube",
                             deep=True, deep_top=3)
    assert res["deep_analyzed"] == 0  # ни один не разобран
    assert res["youtube_added"] == 1  # текстовый скор сохранён


def test_deep_no_media_keeps_title_score(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "deepnomedia.db")
    conn = db.connect(); db.init_db(conn)
    import app.discovery.discover as d
    import app.extractors.pipeline as pipeline_mod
    import app.ingestion.fetch as fetch_mod
    monkeypatch.setattr(d, "score_post", _fake_score)
    monkeypatch.setattr(fetch_mod, "fetch_link", lambda url: ("", [], {}))
    monkeypatch.setattr(pipeline_mod, "extract",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no media")))

    res = discovery.discover(conn, queries=["q1"], per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="youtube",
                             deep=True, deep_top=3)
    assert res["deep_analyzed"] == 0


def _ddg_video_html(urls):
    """Сэмпл выдачи DDG: целевой видео-URL внутри uddg-редиректа (как в реале)."""
    links = []
    for u in urls:
        target = urllib.parse.quote(u, safe="")
        links.append(f'<a class="result__a" href="//duckduckgo.com/l/?uddg={target}&rut=z">x</a>')
    return "<html><body>" + "".join(links) + "</body></html>"


def test_discover_video_links_parses_tiktok_urls(monkeypatch):
    html = _ddg_video_html([
        "https://www.tiktok.com/@casino_promo/video/7300000000000000001",
        "https://www.tiktok.com/@casino_promo",            # профиль -> отсев
        "https://www.tiktok.com/login",                    # служебный -> отсев
        "https://www.tiktok.com/@x/video/7300000000000000002",
        "https://example.com/not-tiktok",                  # чужой хост -> отсев
    ])
    monkeypatch.setattr(web, "_http_get", lambda url: html)
    links = web.discover_video_links("казино", "tiktok", limit=10)
    assert "https://www.tiktok.com/@casino_promo/video/7300000000000000001" in links
    assert "https://www.tiktok.com/@x/video/7300000000000000002" in links
    assert all("/video/" in u for u in links)
    assert "https://www.tiktok.com/login" not in links
    assert all("example.com" not in u for u in links)


def test_discover_video_links_cap_and_empty_on_failure(monkeypatch):
    html = _ddg_video_html([
        "https://www.instagram.com/reel/AAA111/",
        "https://www.instagram.com/reel/BBB222/",
        "https://www.instagram.com/reel/CCC333/",
    ])
    monkeypatch.setattr(web, "_http_get", lambda url: html)
    links = web.discover_video_links("инвестиции", "instagram", limit=2)
    assert len(links) == 2  # per-call cap соблюдён

    def _boom(url):
        raise RuntimeError("network down")
    monkeypatch.setattr(web, "_http_get", _boom)
    assert web.discover_video_links("q", "tiktok", limit=5) == []
