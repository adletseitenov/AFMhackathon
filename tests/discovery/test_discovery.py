"""Тесты движка автономного поиска (сеть замокана). Проверяем: реальные ссылки в
ленте, скоринг своей моделью, идемпотентность, парс YouTube video id."""

import app.discovery.discover as discovery
from app import config, db
from app.discovery.youtube import _video_id


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
