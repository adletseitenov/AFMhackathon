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
    monkeypatch.setattr(scan, "fetch_telegram_links", lambda *a, **k: [])
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
    monkeypatch.setattr(scan, "fetch_telegram_links", lambda *a, **k: [])
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
    monkeypatch.setattr(scan, "fetch_telegram_links", lambda *a, **k: [])
    conn = db.connect()
    db.init_db(conn)
    res = scan.scan_telegram(["@nonexistent_channel_xyz"], conn=conn)
    assert res["added"] == 0 and res["flagged"] == 0


def test_scan_snowballs_channels_and_chats_from_links(tmp_path, monkeypatch):
    # Снежный ком: t.me-ссылки в сообщениях И на странице (кнопки) -> новые каналы и ЧАТЫ.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "scan4.db")

    def _posts(url, limit=12):
        return [Post(id="x", platform="telegram", author_handle="@seed",
                     url="https://t.me/seed",
                     caption="Лучший чат: t.me/casino_chat_kz и наш бот t.me/+AbCdEf12345",
                     posted_at="", media_path=None, thumb_url=None, source="live")]

    # на странице канала (кнопки/описание) — ещё один канал и приватный инвайт в чат
    monkeypatch.setattr(scan, "fetch_telegram_channel", _posts)
    monkeypatch.setattr(scan, "fetch_telegram_links",
                        lambda *a, **k: ["promo_channel_2", "joinchat/XyZ987"])
    conn = db.connect()
    db.init_db(conn)
    res = scan.scan_telegram(["@seed"], conn=conn)
    # новые КАНАЛЫ (по имени) — для снежного скана; ЧАТЫ (инвайты) — как лиды
    assert "casino_chat_kz" in res["discovered_channels"]
    assert "promo_channel_2" in res["discovered_channels"]
    assert any("+" in c or "joinchat/" in c for c in res["discovered_chats"])
    assert "seed" not in res["discovered_channels"]  # сам сид не возвращаем


def test_links_from_text_classifies_chats_channels_and_invites():
    """Определение TG-чатов: приватные инвайты (+hash / joinchat) и username с
    чат-признаком (RU/KZ/EN: chat / беседка / общалка / форум / флудилка / курилка /
    sohbet) -> чаты-лиды; обычные каналы -> channels; служебные t.me-пути отсеяны."""
    channels, chats = set(), set()
    text = (
        "Канал t.me/casino_promo_kz, обсуждение t.me/casino_chat_kz, "
        "беседка t.me/slots_besedka, общалка t.me/win_obshalka, "
        "форум t.me/invest_forum, флудилка t.me/bonus_fludilka, "
        "курилка t.me/ludoman_kurilka, sohbet t.me/casino_sohbet, "
        "приватный t.me/+AbCdEf12345 и t.me/joinchat/XyZ987, "
        "служебное t.me/share/url и t.me/addstickers/pack"
    )
    scan._links_from_text(text, channels, chats)
    assert "+AbCdEf12345" in chats
    assert any("joinchat/" in c for c in chats)
    for c in ("casino_chat_kz", "slots_besedka", "win_obshalka", "invest_forum",
              "bonus_fludilka", "ludoman_kurilka", "casino_sohbet"):
        assert c in chats, f"{c} должен быть распознан как TG-чат"
    assert "casino_promo_kz" in channels and "casino_promo_kz" not in chats
    assert "share" not in channels and "addstickers" not in channels
