"""Веб-поиск публичных Telegram-каналов по скам-запросам (без API-ключа, best-effort).

Дёргает HTML-выдачу DuckDuckGo (html + lite endpoints) и вытаскивает ссылки на
публичные каналы t.me/<channel>, чтобы АВТОНОМНО находить каналы для последующего
реального скана.

DuckDuckGo заворачивает результаты в редирект-ссылки вида
``//duckduckgo.com/l/?uddg=<urlencoded target>&rut=...`` — без декодирования uddg
сырого ``t.me/<name>`` в HTML нет, и наивный парс возвращал 0. Поэтому:
  1) собираем все href результатов (несколько паттернов разметки html/lite),
  2) декодируем параметр uddg каждого редиректа -> реальный URL,
  3) ищем t.me/<name> и в декодированных URL, и в сыром HTML (fallback),
  4) дедуп + отсев служебных путей + кап на число результатов.

Сетевой вызов вынесен в _http_get (тесты его monkeypatch'ят). [] при ошибке.
"""

import re
import urllib.parse
import urllib.request

# Имя публичного канала: t.me/<name> или t.me/s/<name> (предпросмотр).
_TME_RE = re.compile(r"t\.me/(?:s/)?(@?[A-Za-z0-9_]{4,32})", re.IGNORECASE)
# Любые href в выдаче (html и lite используют разную разметку, но href общий).
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
# Параметр uddg в редирект-ссылке DuckDuckGo.
_UDDG_RE = re.compile(r"[?&]uddg=([^&\"']+)", re.IGNORECASE)

# Служебные/не-канальные пути t.me, которые нельзя сканировать как канал.
_STOP = {
    "s", "share", "joinchat", "addstickers", "addemoji", "proxy", "iv",
    "setlanguage", "socks", "login", "confirmphone", "bg", "contact",
}

_DEFAULT_CAP = 6

# Канонические хосты площадок коротких видео для поиска по site:<host>.
# Ключ — короткое имя платформы (как в параметре discover()), значение — домен.
_VIDEO_SITES = {
    "tiktok": "tiktok.com",
    "instagram": "instagram.com",
    "youtube": "youtube.com",
}
# Путь-сегмент, по которому распознаём именно ссылку на ВИДЕО (а не профиль/тег).
_VIDEO_PATH_HINTS = {
    "tiktok.com": ("/video/",),          # tiktok.com/@user/video/123 (не профиль /@user)
    "instagram.com": ("/reel/", "/p/", "/tv/"),
    "youtube.com": ("/watch", "/shorts/"),
}
# Служебные/не-видео пути, которые отсеиваем даже если совпали по хосту.
_VIDEO_STOP_PATHS = (
    "/login", "/signup", "/about", "/legal", "/help", "/explore",
    "/privacy", "/terms", "/developers", "/business",
)


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (KOZ media-watch)"})
    with urllib.request.urlopen(req, timeout=15) as r:  # nosec - публичная выдача
        return r.read().decode("utf-8", "replace")


def _decoded_targets(html: str) -> list:
    """Реальные целевые URL из редирект-ссылок DuckDuckGo (декод uddg)."""
    targets: list = []
    for href in _HREF_RE.findall(html):
        m = _UDDG_RE.search(href)
        if m:
            try:
                targets.append(urllib.parse.unquote(m.group(1)))
            except Exception:
                continue
        elif "t.me" in href:
            # прямой (не-редирект) href на t.me — тоже годится
            targets.append(href)
    return targets


def _channels_from_text(text: str, seen: set, found: list, cap: int) -> bool:
    """Вытащить t.me/<name> из строки в found (с дедупом). True если достигнут cap."""
    for raw in _TME_RE.findall(text):
        name = raw.lstrip("@")
        low = name.lower()
        if low in _STOP or low in seen:
            continue
        seen.add(low)
        found.append(name)
        if len(found) >= cap:
            return True
    return False


def discover_telegram_channels(query: str, limit: int = _DEFAULT_CAP) -> list:
    """Имена публичных Telegram-каналов из веб-выдачи по запросу (дедуп, без мусора).

    limit — кап на число каналов за вызов (защита от разрастания скана).
    """
    cap = max(1, int(limit))
    found: list = []
    seen: set = set()
    for endpoint in (
        "https://html.duckduckgo.com/html/?q=",
        "https://lite.duckduckgo.com/lite/?q=",
    ):
        try:
            html = _http_get(endpoint + urllib.parse.quote(f"site:t.me {query}"))
        except Exception:
            continue
        if not html:
            continue
        # 1) Декодируем редирект-ссылки -> реальные t.me URL.
        for target in _decoded_targets(html):
            if _channels_from_text(target, seen, found, cap):
                return found
        # 2) Fallback: сырой t.me/<name> прямо в HTML (на случай прямых ссылок).
        if _channels_from_text(urllib.parse.unquote(html), seen, found, cap):
            return found
    return found


def _is_video_url(url: str, host: str) -> bool:
    """True если url — ссылка на КОНКРЕТНОЕ видео на площадке host (не профиль/мусор)."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    netloc = (p.hostname or "").lower().rstrip(".")
    if not (netloc == host or netloc.endswith("." + host)):
        return False
    path = (p.path or "").lower()
    if any(path.startswith(stop) for stop in _VIDEO_STOP_PATHS):
        return False
    hints = _VIDEO_PATH_HINTS.get(host, ())
    if not hints:
        return True
    full = path + ("?" + p.query if p.query else "")
    return any(h in full for h in hints)


def discover_video_links(query: str, site: str, limit: int = _DEFAULT_CAP) -> list:
    """РЕАЛЬНЫЕ ссылки на видео площадки `site` из веб-выдачи DuckDuckGo (best-effort).

    site — короткое имя платформы ("tiktok"/"instagram"/"youtube") ИЛИ сам домен
    ("tiktok.com"). Запрос строится как ``site:<host> <query>``; результаты —
    редирект-ссылки DuckDuckGo, у которых декодируется параметр uddg в реальный
    целевой URL (тот же подход, что и в discover_telegram_channels). Возвращает
    дедуплицированный список URL конкретных видео (профили/служебные пути отсеяны),
    капнутый на limit. [] при ошибке/недоступности (НЕ пробрасывает исключение).
    """
    host = _VIDEO_SITES.get((site or "").lower().strip(), (site or "").lower().strip())
    if not host:
        return []
    cap = max(1, int(limit))
    found: list = []
    seen: set = set()
    for endpoint in (
        "https://html.duckduckgo.com/html/?q=",
        "https://lite.duckduckgo.com/lite/?q=",
    ):
        try:
            html = _http_get(endpoint + urllib.parse.quote(f"site:{host} {query}"))
        except Exception:
            continue
        if not html:
            continue
        for target in _decoded_targets(html):
            if not _is_video_url(target, host):
                continue
            key = target.split("?")[0].rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(target)
            if len(found) >= cap:
                return found
    return found
