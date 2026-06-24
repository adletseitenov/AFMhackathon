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
