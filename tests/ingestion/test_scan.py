"""Тесты реального Telegram-сканера (сеть замокана; проверяем интеграцию:
fetch -> extract_entities -> score_post -> revealed-пост в БД, идемпотентность)."""

import app.ingestion.scan as scan
from app import config, db
from app.models import Post


def _fake_posts(url, limit=12):
    return [
        Post(
            id="ignored",  # scan переопределит id по хэшу caption
            platform="telegram",
            author_handle="@casino_promo_kz",
            url="https://t.me/casino_promo_kz",
            caption="Заноси на 1xBet, промокод WIN500, гарантированный доход 30% в месяц!",
            posted_at="",
            media_path=None,
            thumb_url=None,
            source="live",
        ),
        Post(
            id="ignored2",
            platform="telegram",
            author_handle="@casino_promo_kz",
            url="https://t.me/casino_promo_kz",
            caption="Доброе утро, друзья!",
            posted_at="",
            media_path=None,
            thumb_url=None,
            source="live",
        ),
    ]


def test_scan_inserts_scores_and_reveals_real_posts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "scan.db")
    monkeypatch.setattr(scan, "fetch_telegram_channel", _fake_posts)
    conn = db.connect()
    db.init_db(conn)

    res = scan.scan_telegram(["@casino_promo_kz"], conn=conn)
    assert res["added"] == 2
    # оба поста заскорены и раскрыты
    n_posts = conn.execute("SELECT COUNT(*) FROM posts WHERE revealed=1").fetchone()[0]
    n_scores = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert n_posts == 2 and n_scores == 2
    # очевидный скам-текст уходит в высокий риск -> хотя бы один flagged
    assert res["flagged"] >= 1


def test_scan_is_idempotent_on_rescan(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "scan2.db")
    monkeypatch.setattr(scan, "fetch_telegram_channel", _fake_posts)
    conn = db.connect()
    db.init_db(conn)

    first = scan.scan_telegram(["@casino_promo_kz"], conn=conn)
    second = scan.scan_telegram(["@casino_promo_kz"], conn=conn)
    assert first["added"] == 2
    assert second["added"] == 0  # тот же контент не дублируется
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2


def test_scan_handles_empty_or_failed_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "scan3.db")
    monkeypatch.setattr(scan, "fetch_telegram_channel", lambda *a, **k: [])
    conn = db.connect()
    db.init_db(conn)
    res = scan.scan_telegram(["@nonexistent_channel_xyz"], conn=conn)
    assert res["added"] == 0 and res["flagged"] == 0
