"""Тесты cookie-авторизации yt-dlp (вход для Instagram/закрытых площадок).

Cookies включают автопоиск по Instagram (аноним -> логин-вол). Источник — ТОЛЬКО
env: KOZ_COOKIES_FROM_BROWSER (приоритет) или KOZ_COOKIES_FILE. Без env — поведение
не меняется (опции пустые), поэтому остальной автопоиск работает как раньше.
"""

from app.ingestion import fetch


def test_cookie_opts_empty_by_default(monkeypatch):
    monkeypatch.delenv("KOZ_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.delenv("KOZ_COOKIES_FILE", raising=False)
    assert fetch._cookie_opts() == {}
    assert fetch.cookies_configured() is False
    # meta-опции без cookies (поведение по умолчанию не изменилось)
    assert "cookiesfrombrowser" not in fetch._ydl_meta_opts()
    assert "cookiefile" not in fetch._ydl_meta_opts()


def test_cookie_opts_from_browser(monkeypatch):
    monkeypatch.delenv("KOZ_COOKIES_FILE", raising=False)
    monkeypatch.setenv("KOZ_COOKIES_FROM_BROWSER", "firefox")
    assert fetch._cookie_opts() == {"cookiesfrombrowser": ("firefox",)}
    assert fetch.cookies_configured() is True
    assert fetch._ydl_meta_opts().get("cookiesfrombrowser") == ("firefox",)


def test_cookie_opts_unknown_browser_ignored(monkeypatch):
    monkeypatch.delenv("KOZ_COOKIES_FILE", raising=False)
    monkeypatch.setenv("KOZ_COOKIES_FROM_BROWSER", "netscape-navigator")
    assert fetch._cookie_opts() == {}  # неизвестный браузер -> игнор, не падаем


def test_cookie_opts_file(monkeypatch, tmp_path):
    monkeypatch.delenv("KOZ_COOKIES_FROM_BROWSER", raising=False)
    cfile = tmp_path / "cookies.txt"
    cfile.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("KOZ_COOKIES_FILE", str(cfile))
    assert fetch._cookie_opts() == {"cookiefile": str(cfile)}
    assert fetch.cookies_configured() is True


def test_cookie_file_missing_path_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("KOZ_COOKIES_FROM_BROWSER", raising=False)
    monkeypatch.setenv("KOZ_COOKIES_FILE", str(tmp_path / "nope.txt"))
    assert fetch._cookie_opts() == {}  # несуществующий файл -> игнор


def test_browser_takes_priority_over_file(monkeypatch, tmp_path):
    cfile = tmp_path / "cookies.txt"
    cfile.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("KOZ_COOKIES_FILE", str(cfile))
    monkeypatch.setenv("KOZ_COOKIES_FROM_BROWSER", "chrome")
    assert fetch._cookie_opts() == {"cookiesfrombrowser": ("chrome",)}
