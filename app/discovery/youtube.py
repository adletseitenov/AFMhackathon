"""Автономный поиск опасных видео на YouTube через yt-dlp ytsearch (РЕАЛЬНЫЕ ссылки).

Без API-ключа. Возвращает настоящие ролики: url `watch?v=<id>`, заголовок, канал и
реальное превью `i.ytimg.com/vi/<id>/hqdefault.jpg`. Заголовки промо-казино/пирамид
очень показательны — их достаточно для скоринга текстовой моделью. Ленивая загрузка
yt-dlp; при ошибке/блокировке -> [].
"""

import re

_ID_RE = re.compile(r"(?:v=|/)([A-Za-z0-9_-]{11})")


def _video_id(entry: dict) -> str:
    url = entry.get("url") or ""
    m = _ID_RE.search(url)
    if m:
        return m.group(1)
    vid = entry.get("id") or ""
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else ""


def search_youtube(query: str, limit: int = 5) -> list:
    """`ytsearch{limit}:query` -> list[dict] настоящих видео (дедуп по video_id).

    Реальные превью i.ytimg.com сохраняются. [] при ошибке/недоступности.
    """
    out: list = []
    seen: set = set()
    try:
        import yt_dlp

        opts = {"quiet": True, "extract_flat": True, "skip_download": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(f"ytsearch{int(limit)}:{query}", download=False)
        for e in (info.get("entries") or []):
            if not e:
                continue
            vid = _video_id(e)
            if not vid or vid in seen:
                continue
            seen.add(vid)
            out.append({
                "platform": "youtube",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "video_id": vid,
                "author_handle": e.get("uploader") or e.get("channel") or e.get("uploader_id") or "",
                "caption": e.get("title") or "",
                "thumb_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            })
    except Exception:
        return out
    return out
