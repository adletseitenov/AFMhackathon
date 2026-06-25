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
                {"id": "abcdefghijk", "title": "t1", "view_count": 12345},
                {"id": "abcdefghijk", "title": "dup"},  # дубль того же id
                {"id": "lmnopqrstuv", "title": "t2"},   # без view_count -> 0
            ]}
    import sys, types
    fake = types.ModuleType("yt_dlp"); fake.YoutubeDL = _FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    out = search_youtube("q", 5)
    vids = [o["video_id"] for o in out]
    assert vids == ["abcdefghijk", "lmnopqrstuv"]  # дубль отброшен
    assert all("i.ytimg.com" in o["thumb_url"] for o in out)
    # популярность: capture view_count (отсутствует -> 0)
    assert out[0]["view_count"] == 12345
    assert out[1]["view_count"] == 0


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
    rows = conn.execute("SELECT url, platform, source, view_count FROM posts").fetchall()
    assert rows[0]["url"] == "https://www.tiktok.com/@promo/video/123"
    assert rows[0]["platform"] == "tiktok"
    assert rows[0]["source"] == "discovered"
    assert rows[0]["view_count"] == 1000  # популярность из list_account_posts
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


# --- Configurable autodiscovery: catalog registry + country/categories/live/sort ---

import app.discovery.catalog as catalog  # noqa: E402
import app.discovery.streaming as streaming  # noqa: E402


def test_catalog_registry_shape():
    """Реестр содержит обязательные id и форму {id:{label,..}}; build_queries есть."""
    assert {"all", "kz", "ru"} <= set(catalog.COUNTRIES)
    assert {"all", "casino", "pyramid", "fraud", "crypto"} <= set(catalog.CATEGORIES)
    assert {"all", "video", "live"} <= set(catalog.CONTENT_TYPES)
    assert {"relevance", "recent", "popular"} <= set(catalog.SORTS)
    for plat in ("tiktok", "twitch", "kick", "instagram"):
        assert plat in catalog.SEED_ACCOUNTS and catalog.SEED_ACCOUNTS[plat]
    # каждая запись несёт человекочитаемый label
    assert all("label" in v for v in catalog.COUNTRIES.values())
    assert all("queries" in v for v in catalog.CATEGORIES.values())


def test_catalog_payload_shape_matches_route_contract():
    """catalog_payload() = тело GET /api/discover/catalog: списки {id,label}."""
    p = catalog.catalog_payload()
    assert set(p) == {"countries", "categories", "content_types", "sorts"}
    for key in p:
        assert isinstance(p[key], list) and p[key]
        for item in p[key]:
            assert set(item) == {"id", "label"}
            assert isinstance(item["id"], str) and isinstance(item["label"], str)
    ids = {i["id"] for i in p["categories"]}
    assert {"all", "casino", "pyramid", "fraud", "crypto"} <= ids


def test_build_queries_casino_only_is_subset_and_localized():
    """categories=['casino'] -> только casino-запросы; country='kz' локализует их."""
    casino = catalog.build_queries(country="all", categories=["casino"])
    assert casino == catalog.CATEGORIES["casino"]["queries"]  # ровно casino, в порядке реестра
    # 'all'-категория = объединение всех (casino входит подмножеством)
    all_q = catalog.build_queries(country="all", categories=None)
    assert set(casino) <= set(all_q)
    assert set(catalog.CATEGORIES["pyramid"]["queries"]) <= set(all_q)
    # gz-локализация добавляет гео-суффикс к каждому запросу
    kz = catalog.build_queries(country="kz", categories=["casino"])
    geo = catalog.COUNTRIES["kz"]["suffixes"][0]
    assert len(kz) == len(casino)
    assert all(geo in q.lower() for q in kz)


def test_build_queries_unknown_category_falls_back_not_empty():
    out = catalog.build_queries(country="all", categories=["does_not_exist"])
    assert out == catalog.build_queries(country="all", categories=None)  # фоллбэк на все
    assert out  # никогда не пусто


def test_discover_categories_runs_only_casino_queries(tmp_path, monkeypatch):
    """discover(categories=['casino']) гоняет ТОЛЬКО casino-запросы (проверяем q,
    переданные в search_yt)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "cat.db")
    conn = db.connect(); db.init_db(conn)
    seen_q = []

    def _spy_yt(q, limit):
        seen_q.append(q)
        return []  # ингест не важен — проверяем именно набор запросов

    discovery.discover(conn, per_query=1, search_yt=_spy_yt, with_telegram=False,
                       platform="youtube", categories=["casino"])
    assert seen_q == catalog.CATEGORIES["casino"]["queries"]
    # pyramid-запрос НЕ должен попасть в прогон
    assert "пирамида заработок без вложений" not in seen_q


def test_discover_country_localizes_queries(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "geo.db")
    conn = db.connect(); db.init_db(conn)
    seen_q = []
    monkeypatch.setattr(discovery, "score_post", _fake_score)

    def _spy_yt(q, limit):
        seen_q.append(q)
        return []

    discovery.discover(conn, per_query=1, search_yt=_spy_yt, with_telegram=False,
                       platform="youtube", country="kz", categories=["casino"])
    geo = catalog.COUNTRIES["kz"]["suffixes"][0]
    assert seen_q and all(geo in q.lower() for q in seen_q)


def test_discover_explicit_queries_override_catalog(tmp_path, monkeypatch):
    """Явный queries имеет приоритет над country/categories."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ovr.db")
    conn = db.connect(); db.init_db(conn)
    seen_q = []

    def _spy_yt(q, limit):
        seen_q.append(q)
        return []

    discovery.discover(conn, queries=["МОЙ_ЗАПРОС"], per_query=1, search_yt=_spy_yt,
                       with_telegram=False, platform="youtube", categories=["casino"])
    assert seen_q == ["МОЙ_ЗАПРОС"]


def test_discover_live_ingests_only_in_air_with_flag(tmp_path, monkeypatch):
    """content_type='live' зовёт streaming_mod.fetch_live по сид-аккаунтам и
    ингестит ТОЛЬКО тех, кто СЕЙЧАС в эфире (live=True), пост помечен флагом live."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "live.db")
    conn = db.connect(); db.init_db(conn)
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    # три стримера: один в эфире, один оффлайн (None), один без флага live
    monkeypatch.setattr(discovery, "_TWITCH_SEED_STREAMERS",
                        ["live_one", "offline_two", "stale_three"])

    calls = {"args": []}

    def _fake_live(platform, account):
        calls["args"].append((platform, account))
        if account == "live_one":
            return {"url": "https://www.twitch.tv/live_one",
                    "title": "Казино занос прямо сейчас",
                    "description": "Казино занос прямо сейчас",
                    "author_handle": account, "thumb_url": "https://t/x.jpg",
                    "view_count": 4321, "live": True}
        if account == "offline_two":
            return None  # оффлайн -> не ингестим
        return {"url": "https://www.twitch.tv/stale_three", "title": "x",
                "author_handle": account, "live": False}  # не в эфире -> пропуск

    monkeypatch.setattr(streaming, "fetch_live", _fake_live)
    # динамический поиск живых Twitch-эфиров пуст в этом тесте (проверяем только сиды)
    monkeypatch.setattr(streaming, "search_twitch_live", lambda *a, **k: [])

    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="twitch",
                             content_type="live")
    # fetch_live спрошен по всем трём аккаунтам, на эфирной площадке twitch
    assert calls["args"] == [("twitch", "live_one"),
                             ("twitch", "offline_two"),
                             ("twitch", "stale_three")]
    assert res["content_type"] == "live"
    assert res["live_added"] == 1  # только тот, кто в эфире
    assert res["platform_added"] == 1
    assert res["youtube_added"] == 0  # ytsearch для live не используется
    # в ленту попал ровно один live-пост с правильным url
    rows = conn.execute("SELECT url, platform, source FROM posts").fetchall()
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.twitch.tv/live_one"
    assert rows[0]["platform"] == "twitch"
    # sample несёт флаг live=True
    live_samples = [s for s in res["samples"] if s.get("live")]
    assert len(live_samples) == 1 and live_samples[0]["url"] == "https://www.twitch.tv/live_one"


def test_discover_live_none_when_nobody_in_air(tmp_path, monkeypatch):
    """Эфирный режим: никто не в эфире -> live_added=0 + честная нота (не сбой)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "liveempty.db")
    conn = db.connect(); db.init_db(conn)
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    monkeypatch.setattr(discovery, "_KICK_SEED_STREAMERS", ["a", "b"])
    monkeypatch.setattr(streaming, "fetch_live", lambda platform, account: None)
    # новый динамический источник живых Kick-эфиров тоже пуст (никто не в эфире)
    monkeypatch.setattr(streaming, "search_kick_live", lambda *a, **k: [])

    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="kick",
                             content_type="live")
    assert res["live_added"] == 0
    assert "note" in res and "эфир" in res["note"].lower()


def test_discover_sort_popular_orders_by_view_count(tmp_path, monkeypatch):
    """sort='popular' ингестит элементы по убыванию view_count (порядок появления
    в ленте/samples). Проверяем на tiktok-аккаунте (list_account_posts)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sortpop.db")
    conn = db.connect(); db.init_db(conn)
    import app.ingestion.fetch as fetch_mod
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    monkeypatch.setattr(discovery, "_TIKTOK_SEED_ACCOUNTS", ["promo"])

    def _fake_posts(account_url, limit=12):
        # намеренно НЕ по убыванию просмотров — sort='popular' должен переставить
        return [
            {"url": "https://www.tiktok.com/@promo/video/low",
             "title": "казино занос", "description": "казино занос",
             "author_handle": "promo", "thumb_url": "", "view_count": 10},
            {"url": "https://www.tiktok.com/@promo/video/high",
             "title": "казино занос", "description": "казино занос",
             "author_handle": "promo", "thumb_url": "", "view_count": 9000},
            {"url": "https://www.tiktok.com/@promo/video/mid",
             "title": "казино занос", "description": "казино занос",
             "author_handle": "promo", "thumb_url": "", "view_count": 500},
        ]

    monkeypatch.setattr(fetch_mod, "list_account_posts", _fake_posts)

    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="tiktok",
                             sort="popular")
    assert res["sort"] == "popular"
    # порядок samples = порядок ингеста = по убыванию view_count
    order = [s["url"] for s in res["samples"]]
    assert order == [
        "https://www.tiktok.com/@promo/video/high",
        "https://www.tiktok.com/@promo/video/mid",
        "https://www.tiktok.com/@promo/video/low",
    ]


def test_discover_sort_relevance_keeps_source_order(tmp_path, monkeypatch):
    """sort='relevance' (по умолчанию) сохраняет порядок выдачи источника."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sortrel.db")
    conn = db.connect(); db.init_db(conn)
    import app.ingestion.fetch as fetch_mod
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    monkeypatch.setattr(discovery, "_TIKTOK_SEED_ACCOUNTS", ["promo"])

    def _fake_posts(account_url, limit=12):
        return [
            {"url": "https://www.tiktok.com/@promo/video/a", "title": "казино",
             "description": "казино", "author_handle": "promo", "thumb_url": "",
             "view_count": 1},
            {"url": "https://www.tiktok.com/@promo/video/b", "title": "казино",
             "description": "казино", "author_handle": "promo", "thumb_url": "",
             "view_count": 999},
        ]

    monkeypatch.setattr(fetch_mod, "list_account_posts", _fake_posts)
    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="tiktok", sort="relevance")
    order = [s["url"] for s in res["samples"]]
    assert order == ["https://www.tiktok.com/@promo/video/a",
                     "https://www.tiktok.com/@promo/video/b"]


def test_discover_danger_band_filters_ingestion(tmp_path, monkeypatch):
    """Фильтр УРОВНЯ ОПАСНОСТИ: min_risk/max_risk показывает ТОЛЬКО посты в полосе.

    Высокорисковый казино-пост (risk 90) и низкорисковый (risk 10): при danger
    high (70-100) в результаты/ленту попадает только казино-пост; низкорисковый
    сохранён в БД, но НЕ раскрыт (не в ленте/результатах)."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "danger.db")
    conn = db.connect(); db.init_db(conn)
    monkeypatch.setattr(discovery, "score_post", _fake_score)

    def _mixed_yt(q, limit):
        return [
            {"url": "http://yt/high", "caption": "лучшее казино занос", "platform": "youtube"},
            {"url": "http://yt/low", "caption": "обычные новости погоды", "platform": "youtube"},
        ]

    res = discovery.discover(conn, queries=["q1"], per_query=2, search_yt=_mixed_yt,
                             with_telegram=False, platform="youtube",
                             min_risk=70, max_risk=100)
    assert res["youtube_added"] == 1
    assert [s["risk"] for s in res["samples"]] == [90]
    assert res["min_risk"] == 70 and res["max_risk"] == 100
    revealed_urls = {p.url for p in db.get_revealed_posts(conn)}
    assert "http://yt/high" in revealed_urls
    assert "http://yt/low" not in revealed_urls  # вне полосы -> не раскрыт


def test_discover_kick_search_ingests_live_streams(tmp_path, monkeypatch):
    """Динамический поиск живых Kick-эфиров: сид-аккаунты офлайн, но search_kick_live
    находит живой казино-стрим -> он ингестится с флагом live и раскрывается."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "kicklive.db")
    conn = db.connect(); db.init_db(conn)
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    monkeypatch.setattr(discovery, "_KICK_SEED_STREAMERS", ["seed_off"])
    monkeypatch.setattr(streaming, "fetch_live", lambda platform, account: None)
    live_info = {
        "url": "https://kick.com/casinoexile", "title": "casino slots big win",
        "description": "casino slots big win", "author_handle": "casinoexile",
        "thumb_url": "", "view_count": 173, "live": True,
    }
    monkeypatch.setattr(streaming, "search_kick_live", lambda *a, **k: [live_info])

    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="kick", content_type="live")
    assert res["live_added"] == 1
    live_samples = [s for s in res["samples"] if s.get("live")]
    assert len(live_samples) == 1
    assert live_samples[0]["url"] == "https://kick.com/casinoexile"
    revealed = db.get_revealed_posts(conn)
    assert any(p.url == "https://kick.com/casinoexile" and p.live for p in revealed)


def test_discover_twitch_search_ingests_live_streams(tmp_path, monkeypatch):
    """Динамический поиск живых Twitch-эфиров: сид-стримеры офлайн, но
    search_twitch_live находит живой казино-стрим -> ингест с флагом live."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "twlive.db")
    conn = db.connect(); db.init_db(conn)
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    monkeypatch.setattr(discovery, "_TWITCH_SEED_STREAMERS", ["seed_off"])
    monkeypatch.setattr(streaming, "fetch_live", lambda platform, account: None)
    live_info = {
        "url": "https://www.twitch.tv/hunterowner", "title": "casino wager rewards",
        "description": "casino wager rewards", "author_handle": "hunterowner",
        "thumb_url": "", "view_count": 1868, "live": True,
    }
    monkeypatch.setattr(streaming, "search_twitch_live", lambda *a, **k: [live_info])

    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt,
                             with_telegram=False, platform="twitch", content_type="live")
    assert res["live_added"] == 1
    live_samples = [s for s in res["samples"] if s.get("live")]
    assert len(live_samples) == 1
    assert live_samples[0]["url"] == "https://www.twitch.tv/hunterowner"
    assert any(p.url == "https://www.twitch.tv/hunterowner" and p.live
               for p in db.get_revealed_posts(conn))


def test_live_marks_no_streamers_from_search_country(tmp_path, monkeypatch):
    """Страновая привязка эфиров: ищем по Казахстану, но Twitch отдаёт только
    эфир на английском -> честная пометка, что стримеров из страны не найдено."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tw_country.db")
    conn = db.connect(); db.init_db(conn)
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    monkeypatch.setattr(discovery, "_TWITCH_SEED_STREAMERS", ["seed_off"])
    monkeypatch.setattr(streaming, "fetch_live", lambda platform, account: None)
    monkeypatch.setattr(streaming, "search_kick_live", lambda *a, **k: [])
    en_stream = {
        "url": "https://www.twitch.tv/globalcasino", "title": "casino wager rewards",
        "description": "casino wager rewards", "author_handle": "globalcasino",
        "thumb_url": "", "view_count": 1500, "live": True, "language": "en",
    }
    monkeypatch.setattr(streaming, "search_twitch_live", lambda *a, **k: [en_stream])

    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt, with_telegram=False,
                             platform="twitch", content_type="live", country="kz")
    assert res["live_added"] == 1
    assert "live_country_note" in res
    assert "не найден" in res["live_country_note"].lower()
    assert res.get("note") == res["live_country_note"]


def test_live_no_country_note_when_streamer_from_search_country(tmp_path, monkeypatch):
    """Если есть эфир на языке выбранной страны (ru/kk для КЗ) — пометки нет."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tw_country2.db")
    conn = db.connect(); db.init_db(conn)
    monkeypatch.setattr(discovery, "score_post", _fake_score)
    monkeypatch.setattr(discovery, "_TWITCH_SEED_STREAMERS", ["seed_off"])
    monkeypatch.setattr(streaming, "fetch_live", lambda platform, account: None)
    monkeypatch.setattr(streaming, "search_kick_live", lambda *a, **k: [])
    ru_stream = {
        "url": "https://www.twitch.tv/kzcasino", "title": "казино занос слоты",
        "description": "казино занос слоты", "author_handle": "kzcasino",
        "thumb_url": "", "view_count": 900, "live": True, "language": "ru",
    }
    monkeypatch.setattr(streaming, "search_twitch_live", lambda *a, **k: [ru_stream])

    res = discovery.discover(conn, per_query=1, search_yt=_fake_yt, with_telegram=False,
                             platform="twitch", content_type="live", country="kz")
    assert res["live_added"] == 1
    assert "live_country_note" not in res
