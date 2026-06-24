"""Сборщики VOD-ов со стриминговых площадок TWITCH и KICK для автопоиска.

Гемблинг-стримеры (slots/casino/BONUS HUNT под STAKE/roobet) — главный канал
нелегальной рекламы казино в РК. Здесь два РОБАСТНЫХ сборщика, каждый отдаёт
нормализованный список VOD-ов канала. ОБА проглатывают любую ошибку и возвращают
[] (сеть/парсинг/недоступная библиотека) — никогда не пробрасывают исключение,
чтобы прогон автопоиска не падал из-за одной площадки/стримера.

curl_cffi и yt_dlp импортируются ЛЕНИВО ВНУТРИ функций (как в app/ingestion/fetch.py)
— импорт модуля остаётся лёгким, а тесты могут monkeypatch'ить эти sys.modules.

Kick дёргается ПРЯМО через curl_cffi (impersonate=chrome обходит Cloudflare) — НЕ
через SSRF-allowlist app/ingestion/fetch.py (тот покрывает только tiktok/instagram/
youtube/telegram). Twitch — через yt-dlp extract_flat по странице /videos канала.
"""


def fetch_kick_videos(channel: str, limit: int = 12) -> list:
    """VOD-ы Kick-канала через curl_cffi (Kick API v2, impersonate=chrome).

    GET https://kick.com/api/v2/channels/<channel>/videos -> СПИСОК VOD-ов. Для
    каждого элемента с video.uuid строим нормализованный dict:
      {url, title, description, author_handle, thumb_url, view_count:int}.
    Заголовок стрима (session_title) часто содержит казино-термины (STAKE/roobet/
    BONUS HUNT/$-суммы) — он идёт и в title, и в description для скоринга. Элементы
    без video.uuid пропускаем. -> list[dict]; [] при ЛЮБОЙ ошибке (не пробрасывает).
    """
    try:
        from curl_cffi import requests as creq

        url = f"https://kick.com/api/v2/channels/{channel}/videos"
        resp = creq.get(url, impersonate="chrome", timeout=15)
        data = resp.json()
        if not isinstance(data, list):
            return []
        out: list = []
        for v in data[: max(0, int(limit))]:
            if not isinstance(v, dict):
                continue
            video = v.get("video") or {}
            uuid = video.get("uuid") if isinstance(video, dict) else None
            if not uuid:
                continue
            title = v.get("session_title") or ""
            thumb = v.get("thumbnail") or {}
            thumb_url = thumb.get("src") if isinstance(thumb, dict) else ""
            try:
                views = int(v.get("views") or 0)
            except (TypeError, ValueError):
                views = 0
            out.append({
                "url": "https://kick.com/video/" + str(uuid),
                "title": title,
                "description": title,
                "author_handle": channel,
                "thumb_url": thumb_url or "",
                "view_count": views,
            })
        return out
    except Exception:
        return []


def _twitch_meta_opts(limit: int) -> dict:
    """Опции yt-dlp для extract_flat по странице /videos Twitch-канала (без
    скачивания). Подключаем Node как JS-runtime, если он есть (как fetch._ydl_meta_opts).
    """
    opts = {
        "quiet": True,
        "extract_flat": True,
        "playlistend": max(1, int(limit)),
        "skip_download": True,
        "no_warnings": True,
    }
    try:
        from app.ingestion.fetch import _js_runtimes

        js_rt = _js_runtimes()
        if js_rt:
            opts["js_runtimes"] = js_rt
    except Exception:
        pass
    return opts


def fetch_twitch_videos(channel: str, limit: int = 12) -> list:
    """VOD-ы Twitch-канала через yt-dlp extract_flat по странице /videos.

    https://www.twitch.tv/<channel>/videos -> entries с title/url/view_count/
    thumbnails. Часть каналов отдаёт 0 VOD-ов (Twitch удаляет старые) — это норма,
    проглатываем. Маппинг каждого entry:
      {url, title, description, author_handle, thumb_url, view_count:int}.
    -> list[dict]; [] при ЛЮБОЙ ошибке (не пробрасывает).
    """
    try:
        import yt_dlp

        url = f"https://www.twitch.tv/{channel}/videos"
        with yt_dlp.YoutubeDL(_twitch_meta_opts(limit)) as ydl:
            info = ydl.extract_info(url, download=False)
        out: list = []
        for e in (info.get("entries") or []):
            if not isinstance(e, dict):
                continue
            u = e.get("url") or e.get("webpage_url")
            if not u:
                continue
            thumbs = e.get("thumbnails") or []
            thumb = ""
            if thumbs and isinstance(thumbs[-1], dict):
                thumb = thumbs[-1].get("url") or ""
            if not thumb:
                thumb = e.get("thumbnail") or ""
            title = e.get("title") or ""
            try:
                views = int(e.get("view_count") or 0)
            except (TypeError, ValueError):
                views = 0
            out.append({
                "url": u,
                "title": title,
                "description": title,
                "author_handle": channel,
                "thumb_url": thumb or "",
                "view_count": views,
            })
        return out
    except Exception:
        return []
