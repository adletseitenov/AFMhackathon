"""Сборщики со стриминговых/видео-площадок (TWITCH, KICK, TikTok, Instagram) +
ДЕТЕКТ ПРЯМЫХ ЭФИРОВ для автопоиска.

Гемблинг-стримеры (slots/casino/BONUS HUNT под STAKE/roobet) — главный канал
нелегальной рекламы казино в РК. Здесь РОБАСТНЫЕ сборщики, каждый отдаёт
нормализованный список постов/VOD-ов либо dict эфира. ВСЕ проглатывают любую
ошибку и возвращают [] / None (сеть/парсинг/недоступная библиотека) — никогда не
пробрасывают исключение, чтобы прогон автопоиска не падал из-за одной
площадки/стримера/эфира.

curl_cffi и yt_dlp импортируются ЛЕНИВО ВНУТРИ функций (как в app/ingestion/fetch.py)
— импорт модуля остаётся лёгким, а тесты могут monkeypatch'ить эти sys.modules.

Kick дёргается ПРЯМО через curl_cffi (impersonate=chrome обходит Cloudflare) — НЕ
через SSRF-allowlist app/ingestion/fetch.py (тот покрывает только tiktok/instagram/
youtube/telegram). Twitch/TikTok/Instagram — через yt-dlp (extract_flat для лент,
прямой extract для эфиров).

НОРМАЛИЗОВАННЫЙ dict постов/VOD-ов/клипов/эфиров (контракт):
  {url, title, description, author_handle, thumb_url, view_count:int, live:bool}.
fetch_*_videos/posts -> list[dict] (live=False); fetch_live -> dict|None (live=True).
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


def _flat_meta_opts(limit: int) -> dict:
    """Опции yt-dlp для extract_flat по странице-плейлисту (Twitch /videos|/clips,
    Instagram-аккаунт) без скачивания. Подключаем Node как JS-runtime, если есть
    (как fetch._ydl_meta_opts).
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


def _live_meta_opts() -> dict:
    """Опции yt-dlp для извлечения МЕТА эфира (twitch:stream/tiktok:live) без
    скачивания. НЕ extract_flat — нужны title/is_live/view_count живого стрима.
    """
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    try:
        from app.ingestion.fetch import _js_runtimes

        js_rt = _js_runtimes()
        if js_rt:
            opts["js_runtimes"] = js_rt
    except Exception:
        pass
    return opts


def _int_or_zero(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _entry_thumb(e: dict) -> str:
    """Берём последний (самый качественный) thumbnail; фоллбэк — поле thumbnail."""
    thumbs = e.get("thumbnails") or []
    if thumbs and isinstance(thumbs[-1], dict):
        t = thumbs[-1].get("url") or ""
        if t:
            return t
    return e.get("thumbnail") or ""


def _map_entry(e: dict, channel: str) -> "dict | None":
    """Нормализует одну запись ленты/плейлиста yt-dlp в контрактный dict (live=False).
    Возвращает None для записи без url (её пропускаем)."""
    if not isinstance(e, dict):
        return None
    u = e.get("url") or e.get("webpage_url")
    if not u:
        return None
    title = e.get("title") or ""
    desc = e.get("description") or title
    handle = e.get("uploader") or e.get("uploader_id") or e.get("channel") or channel
    return {
        "url": u,
        "title": title,
        "description": desc,
        "author_handle": handle,
        "thumb_url": _entry_thumb(e),
        "view_count": _int_or_zero(e.get("view_count")),
        "live": False,
    }


def _ydl_entries(url: str, limit: int) -> list:
    """extract_flat по плейлисту-странице -> list нормализованных dict. Пустой
    плейлист/любая ошибка -> []. Заголовки VOD/клипов (title) идут и в description
    для скоринга по казино-терминам."""
    import yt_dlp

    with yt_dlp.YoutubeDL(_flat_meta_opts(limit)) as ydl:
        info = ydl.extract_info(url, download=False)
    out: list = []
    for e in (info.get("entries") or []):
        m = _map_entry(e, "")
        if m is not None:
            out.append(m)
    return out


def fetch_twitch_videos(channel: str, limit: int = 12) -> list:
    """VOD-ы Twitch-канала через yt-dlp extract_flat по странице /videos, с
    ФОЛЛБЭКОМ на /clips если VOD-ов нет.

    https://www.twitch.tv/<channel>/videos -> entries с title/url/view_count/
    thumbnails. Часть каналов отдаёт 0 VOD-ов (Twitch удаляет старые VOD по
    истечении срока хранения) — тогда пробуем /clips (клипы живут дольше и тоже
    содержат казино-промо). Маппинг каждого entry в контрактный dict с
    author_handle=channel (lent yt-dlp не всегда проставляет uploader для twitch).
    -> list[dict] (live=False); [] при ЛЮБОЙ ошибке (не пробрасывает).
    """
    try:
        out = _ydl_entries(f"https://www.twitch.tv/{channel}/videos", limit)
        if not out:
            # VOD истекли -> фоллбэк на клипы (живут дольше).
            out = _ydl_entries(f"https://www.twitch.tv/{channel}/clips", limit)
        # author_handle для twitch фиксируем по каналу (надёжнее, чем lent uploader).
        for v in out:
            v["author_handle"] = channel
        return out
    except Exception:
        return []


def fetch_instagram_posts(account: str, limit: int = 12) -> list:
    """BEST-EFFORT посты/reels публичного Instagram-аккаунта через yt-dlp extract_flat.

    https://www.instagram.com/<account>/ -> entries (публичные reels/посты). Instagram
    закрыт логин-волом для yt-dlp/curl_cffi на многих аккаунтах — поэтому это
    best-effort: при логин-воле/ошибке/пустом ответе возвращаем [] (вызывающий код
    покажет честную ноту, что Instagram недоступен без входа). Нормализованный dict
    как в контракте (live=False). -> list[dict]; [] при ЛЮБОЙ ошибке (не пробрасывает).
    """
    try:
        handle = (account or "").lstrip("@")
        url = f"https://www.instagram.com/{handle}/"
        out = _ydl_entries(url, limit)
        # account как фоллбэк author_handle, если lent его не дал.
        for v in out:
            if not v.get("author_handle"):
                v["author_handle"] = handle
        return out
    except Exception:
        return []


def _fetch_live_ytdlp(url: str, channel: str) -> "dict | None":
    """Детект эфира через yt-dlp (twitch:stream / tiktok:live).

    Живой эфир -> extract_info отдаёт title/is_live/view_count; оффлайн -> экстрактор
    кидает ('not currently live'/'is offline') -> ловим и возвращаем None. Если в инфо
    есть явный is_live=False — тоже None (defense). -> dict(live=True)|None.
    """
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(_live_meta_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not isinstance(info, dict) or not info:
            return None
        if info.get("is_live") is False:
            return None
        title = info.get("title") or info.get("description") or ""
        # author_handle = канал-слаг (стабильный id, как в fetch_*_videos), не
        # display-name из lent yt-dlp uploader.
        return {
            "url": info.get("webpage_url") or url,
            "title": title,
            "description": title,
            "author_handle": channel,
            "thumb_url": info.get("thumbnail") or "",
            "view_count": _int_or_zero(info.get("view_count") or info.get("concurrent_view_count")),
            "live": True,
        }
    except Exception:
        return None


def _fetch_live_kick(channel: str) -> "dict | None":
    """Детект эфира Kick через curl_cffi GET kick.com/api/v2/channels/<channel>.

    Поле livestream: null (оффлайн) -> None; непусто -> dict с title=session_title,
    view_count=viewer_count, thumb. -> dict(live=True)|None; None при ЛЮБОЙ ошибке.
    """
    try:
        from curl_cffi import requests as creq

        url = f"https://kick.com/api/v2/channels/{channel}"
        resp = creq.get(url, impersonate="chrome", timeout=15)
        data = resp.json()
        if not isinstance(data, dict):
            return None
        ls = data.get("livestream")
        if not ls or not isinstance(ls, dict):
            return None  # оффлайн
        title = ls.get("session_title") or ""
        thumb = ls.get("thumbnail") or {}
        thumb_url = thumb.get("url") if isinstance(thumb, dict) else ""
        return {
            "url": f"https://kick.com/{channel}",
            "title": title,
            "description": title,
            "author_handle": channel,
            "thumb_url": thumb_url or "",
            "view_count": _int_or_zero(ls.get("viewer_count")),
            "live": True,
        }
    except Exception:
        return None


def fetch_live(platform: str, channel: str) -> "dict | None":
    """ДЕТЕКТ ПРЯМОГО ЭФИРА для канала на площадке. РОБАСТНА: None при ЛЮБОЙ ошибке.

    twitch    -> yt-dlp 'https://www.twitch.tv/<channel>' (экстрактор twitch:stream);
                 оффлайн ('not currently live')/ошибка -> None.
    kick      -> curl_cffi GET kick.com/api/v2/channels/<channel>, поле livestream;
                 null -> None, иначе dict (title=session_title, view_count=viewer_count).
    tiktok    -> yt-dlp 'https://www.tiktok.com/@<channel>/live' (tiktok:live);
                 оффлайн/ошибка -> None.
    instagram -> best-effort через yt-dlp 'https://www.instagram.com/<channel>/live';
                 обычно логин-вол -> None.
    Прочее    -> None.

    Возврат при онлайне — нормализованный dict с live=True:
      {url, title, description, author_handle, thumb_url, view_count:int, live:True}.
    """
    p = (platform or "").lower()
    ch = (channel or "").lstrip("@")
    if not ch:
        return None
    if p == "twitch":
        return _fetch_live_ytdlp(f"https://www.twitch.tv/{ch}", ch)
    if p == "kick":
        return _fetch_live_kick(ch)
    if p == "tiktok":
        return _fetch_live_ytdlp(f"https://www.tiktok.com/@{ch}/live", ch)
    if p == "instagram":
        return _fetch_live_ytdlp(f"https://www.instagram.com/{ch}/live", ch)
    return None


# Гемблинг-запросы для динамического поиска ЖИВЫХ казино-стримов на Kick.
_KICK_LIVE_SEARCH_TERMS = ("slots", "casino", "gambling", "stake")


def search_kick_live(terms=None, per_term: int = 5, max_live: int = 6) -> list:
    """Найти Kick-каналы по гемблинг-запросам и вернуть тех, кто СЕЙЧАС в ЭФИРЕ.

    Зачем: проверка фиксированного списка сид-стримеров часто даёт 0 (никто из них
    не в эфире прямо сейчас). Kick search API (api/search?searched_word=<term>) даёт
    КАНДИДАТОВ по ключевым словам казино/слотов; для каждого проверяем эфир через
    _fetch_live_kick (livestream != null). Так находим РЕАЛЬНЫЕ живые гемблинг-эфиры,
    даже когда сид-аккаунты офлайн.

    РОБАСТНА: [] при любой ошибке. Возврат — список нормализованных dict(live=True),
    как у fetch_live: {url,title,description,author_handle,thumb_url,view_count,live}.
    Ограничения: per_term кандидатов на запрос, не более max_live живых суммарно.
    """
    terms = terms or _KICK_LIVE_SEARCH_TERMS
    out: list = []
    seen: set = set()
    try:
        from curl_cffi import requests as creq
    except Exception:
        return []
    for term in terms:
        if len(out) >= max_live:
            break
        try:
            resp = creq.get(
                f"https://kick.com/api/search?searched_word={term}&type=channels",
                impersonate="chrome", timeout=12,
            )
            chans = resp.json().get("channels") or []
        except Exception:
            continue
        for c in chans[:per_term]:
            if len(out) >= max_live:
                break
            slug = str((c or {}).get("slug") or "").strip().lstrip("@")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            info = _fetch_live_kick(slug)  # подтверждает эфир + полные данные
            if info:
                out.append(info)
    return out
