"""Веб-поиск публичных Telegram-каналов по скам-запросам (без API-ключа, best-effort).

Дёргает HTML-выдачу DuckDuckGo и вытаскивает любые ссылки t.me/<channel>, чтобы
АВТОНОМНО находить публичные каналы для последующего реального скана. При
блокировке/ошибке -> []. Сетевой вызов вынесен в _http_get (тесты его monkeypatch'ят).
"""

import re
import urllib.parse
import urllib.request

_TME_RE = re.compile(r"t\.me/(?:s/)?([A-Za-z0-9_]{4,32})", re.IGNORECASE)
_STOP = {"s", "share", "joinchat", "addstickers", "proxy", "iv"}


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (KOZ media-watch)"})
    with urllib.request.urlopen(req, timeout=15) as r:  # nosec - публичная выдача
        return r.read().decode("utf-8", "replace")


def discover_telegram_channels(query: str, limit: int = 6) -> list:
    """Имена публичных Telegram-каналов из веб-выдачи по запросу (дедуп, без мусора)."""
    found: list = []
    seen: set = set()
    for endpoint in (
        "https://html.duckduckgo.com/html/?q=",
        "https://lite.duckduckgo.com/lite/?q=",
    ):
        try:
            html = _http_get(endpoint + urllib.parse.quote(f"site:t.me {query}"))
            html = urllib.parse.unquote(html)
            for ch in _TME_RE.findall(html):
                low = ch.lower()
                if low in _STOP or low in seen:
                    continue
                seen.add(low)
                found.append(ch)
                if len(found) >= limit:
                    return found
        except Exception:
            continue
    return found
