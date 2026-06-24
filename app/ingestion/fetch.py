"""Live-получение медиа: yt-dlp по ссылке + обработка загруженного файла.

Возвращает (media_path, frames, meta). Всё лениво (yt-dlp/cv2) и деградирует
мягко (A6): при отсутствии библиотек медиа="" / frames=[].

§0.5 seam: fetch_post(url=None, upload=None) -> Post — обёртка над fetch_link/
handle_upload, собирает Post. F4 /api/analyze вызывает fetch_post через атрибут
модуля, чтобы тесты могли monkeypatch'ить один seam.
"""

import ipaddress
import os
import re
import socket
import urllib.request
import uuid
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

from app.config import MEDIA_DIR
from app.extractors.text import normalize
from app.models import Post

_FRAME_COUNT = 5
_MAX_VIDEO_SECONDS = 90.0  # разбираем первые N сек видео (скорость на CPU, достаточно для рекламы)


def _load_cv2():
    """Лениво грузит cv2. При недоступности -> None."""
    try:
        import cv2

        return cv2
    except Exception:
        return None


def _ffmpeg_location() -> "str | None":
    """Каталог с ffmpeg+ffprobe для yt-dlp (нарезка/склейка реально требуют ОБА бинаря).

    Порядок: env KOZ_FFMPEG_DIR -> ffmpeg в PATH -> AppData\\Programs\\ffmpeg\\bin ->
    bundled imageio_ffmpeg (только ffmpeg, без ffprobe — крайний фолбэк). Возвращает
    КАТАЛОГ (чтобы yt-dlp нашёл и ffmpeg, и ffprobe) либо путь к bundled-бинарю, либо None.
    """
    import shutil

    candidates = []
    env_dir = os.environ.get("KOZ_FFMPEG_DIR")
    if env_dir:
        candidates.append(env_dir)
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(os.path.dirname(which))
    candidates.append(
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "ffmpeg", "bin")
    )
    for d in candidates:
        if not d:
            continue
        win = os.path.exists(os.path.join(d, "ffmpeg.exe")) and os.path.exists(
            os.path.join(d, "ffprobe.exe")
        )
        nix = os.path.exists(os.path.join(d, "ffmpeg")) and os.path.exists(
            os.path.join(d, "ffprobe")
        )
        if win or nix:
            return d
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and os.path.exists(exe) else None
    except Exception:
        return None


def sample_frames(media_path: str) -> list:
    """Возвращает до _FRAME_COUNT путей-кадров (PNG). [] если cv2/видео недоступны."""
    if not media_path or not os.path.exists(media_path):
        return []
    cv2 = _load_cv2()
    if cv2 is None:
        return []
    try:
        cap = cv2.VideoCapture(media_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        if total <= 0:
            cap.release()
            return []
        step = max(total // _FRAME_COUNT, 1)
        frames: list = []
        base = os.path.splitext(media_path)[0]
        for i in range(_FRAME_COUNT):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i * step)
            ok, img = cap.read()
            if not ok:
                break
            out = f"{base}_frame{i}.png"
            # Unicode-safe запись: cv2.imwrite НЕ умеет пути с кириллицей на Windows
            # (а репозиторий лежит в …\Документы\…). imencode+tofile пишет через Python.
            ok2, buf = cv2.imencode(".png", img)
            if not ok2:
                continue
            buf.tofile(out)
            frames.append(out)
        cap.release()
        return frames
    except Exception:
        return []


# Поддерживаемые публичные платформы — ЖЁСТКИЙ allowlist хостов. Это главный
# SSRF-контроль: атакующий не владеет DNS этих доменов, поэтому DNS-rebinding на
# внутренний IP и redirect на internal-хост практически исключены (площадки не
# редиректят на приватные адреса). IP-проверка ниже — defense-in-depth.
_ALLOWED_SUFFIXES = (
    "tiktok.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "t.me",
    "telegram.me",
)


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in _ALLOWED_SUFFIXES)


def _is_public_ip(ip_str: str) -> bool:
    """True только для маршрутизируемых публичных адресов.

    Разворачивает IPv4-mapped IPv6 (напр. ::ffff:127.0.0.1 -> 127.0.0.1), чтобы
    их нельзя было использовать для обхода проверки на приватные/loopback адреса.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _is_safe_public_url(url: str) -> bool:
    """SSRF-защита: только http(s) к ПОДДЕРЖИВАЕМЫМ публичным платформам.

    Два контроля:
      1) жёсткий allowlist доменов площадок (_host_allowed) — главный контроль:
         атакующий не владеет DNS tiktok/instagram/youtube/t.me, поэтому
         DNS-rebinding на внутренний IP практически невозможен (хост обязан быть
         реальной площадкой);
      2) резолв хоста и отклонение приватных/loopback/link-local/reserved IP, в т.ч.
         IPv4-mapped IPv6 (_is_public_ip) — defense-in-depth (блок 169.254.169.254 и т.п.).

    ОСТАТОЧНЫЙ РИСК (принят для MVP): yt-dlp сам резолвит хост и следует CDN-редиректам,
    которые здесь повторно не валидируются — полная защита требует egress-proxy с
    проверкой на connect или ре-валидации каждого redirect-хопа (вне рамок MVP).
    Allowlist делает это непрактичным, а сам live-fetch — ОПЦИОНАЛЬНЫЙ путь (оцениваемое
    демо работает на закэшированных признаках без исходящих запросов).
    """
    try:
        p = urlparse(url or "")
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        if not _host_allowed(p.hostname):
            return False
        infos = socket.getaddrinfo(p.hostname, None)
        if not infos:
            return False
        return all(_is_public_ip(sockaddr[0]) for *_rest, sockaddr in infos)
    except Exception:
        return False


_NODE_BIN_CACHE = None


def _js_runtimes():
    """yt-dlp 2026+ требует JS-runtime для полноценной экстракции YouTube — без него
    выгрузка деградирует («No supported JavaScript runtime could be found»). Node в
    системе уже есть (фронт его использует), подключаем его как runtime.
    env KOZ_NODE_BIN переопределяет путь. -> dict для ydl_opts['js_runtimes'] или None.
    """
    global _NODE_BIN_CACHE
    if _NODE_BIN_CACHE is None:
        import shutil

        _NODE_BIN_CACHE = os.environ.get("KOZ_NODE_BIN") or shutil.which("node") or ""
    if not _NODE_BIN_CACHE:
        return None
    return {"deno": {"path": None}, "node": {"path": _NODE_BIN_CACHE}}


def _ytdlp_download(url: str):
    """Лениво грузит yt-dlp и скачивает медиа. -> (media_path, meta) или ("", {})."""
    if not _is_safe_public_url(url):
        return "", {}  # SSRF-guard: не делаем исходящий запрос на приватный/невалидный URL
    try:
        import yt_dlp

        os.makedirs(MEDIA_DIR, exist_ok=True)
        out_tmpl = os.path.join(str(MEDIA_DIR), f"{uuid.uuid4().hex}.%(ext)s")
        opts = {
            "outtmpl": out_tmpl,
            "quiet": True,
            "noplaylist": True,
            "format": "mp4/best",
        }
        js_rt = _js_runtimes()
        if js_rt:
            opts["js_runtimes"] = js_rt
        opts.update(_cookie_opts())  # cookies для Instagram/закрытых площадок (если заданы в env)
        ffmpeg_loc = _ffmpeg_location()
        if ffmpeg_loc:
            # yt-dlp использует ffmpeg/ffprobe для нарезки/склейки.
            opts["ffmpeg_location"] = ffmpeg_loc
            # Надёжно: добавляем каталог ffmpeg в PATH (yt-dlp не всегда подхватывает
            # ffmpeg_location-каталог для проверки доступности при нарезке).
            if os.path.isdir(ffmpeg_loc) and ffmpeg_loc not in os.environ.get("PATH", ""):
                os.environ["PATH"] = ffmpeg_loc + os.pathsep + os.environ.get("PATH", "")
            # ПРОДУКТ: разбираем первые ~90 сек (достаточно для рекламы/скам-воронки) —
            # кратно быстрее на CPU. Нарезка требует ffmpeg, поэтому только при наличии.
            try:
                opts["download_ranges"] = yt_dlp.utils.download_range_func(
                    None, [(0.0, _MAX_VIDEO_SECONDS)]
                )
                opts["force_keyframes_at_cuts"] = True
            except Exception:
                pass
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            media_path = ydl.prepare_filename(info)
        meta = {
            "caption": info.get("description") or info.get("title") or "",
            "author_handle": info.get("uploader") or info.get("channel") or "",
            "platform": (info.get("extractor_key") or "").lower(),
            "thumb_url": info.get("thumbnail") or "",
        }
        return media_path, meta
    except Exception:
        return "", {}


def fetch_link(url: str):
    """Скачивает медиа по ссылке, сэмплит кадры. -> (media_path, frames, meta)."""
    media_path, meta = _ytdlp_download(url)
    if not media_path:
        return "", [], {
            "caption": "",
            "author_handle": "",
            "platform": "",
            "thumb_url": "",
        }
    frames = sample_frames(media_path)
    meta.setdefault("caption", "")
    meta.setdefault("author_handle", "")
    meta.setdefault("platform", "")
    meta.setdefault("thumb_url", "")
    meta["url"] = url
    return media_path, frames, meta


_COOKIE_BROWSERS = {
    "chrome", "chromium", "firefox", "edge", "brave", "opera", "vivaldi", "safari",
}


def _cookie_opts() -> dict:
    """yt-dlp cookie-опции для авторизованных площадок (Instagram режет аноним).

    Instagram (и местами TikTok) НЕ отдают публичный автопоиск без входа. Чтобы
    автопоиск по ним заработал, нужны cookies авторизованной сессии. Источник —
    ТОЛЬКО переменные окружения (никаких секретов в репозитории):
      KOZ_COOKIES_FROM_BROWSER=chrome|firefox|edge|brave|...  -> yt-dlp читает cookies
        прямо из браузера (достаточно быть залогиненным в Instagram в этом браузере);
      KOZ_COOKIES_FILE=<путь к cookies.txt>  -> файл cookies в формате Netscape
        (экспорт расширением «Get cookies.txt» из залогиненного браузера).
    Если ничего не задано -> {} (аноним -> логин-вол -> честная нота, как раньше).
    cookiesfrombrowser имеет приоритет над cookiefile.
    """
    browser = os.environ.get("KOZ_COOKIES_FROM_BROWSER", "").strip().lower()
    if browser in _COOKIE_BROWSERS:
        return {"cookiesfrombrowser": (browser,)}
    cfile = os.environ.get("KOZ_COOKIES_FILE", "").strip()
    if cfile and os.path.isfile(cfile):
        return {"cookiefile": cfile}
    return {}


def cookies_configured() -> bool:
    """True, если задан источник cookies (вход для Instagram/закрытых площадок)."""
    return bool(_cookie_opts())


def _ydl_meta_opts() -> dict:
    """Опции yt-dlp для извлечения МЕТАДАННЫХ без скачивания медиа."""
    opts = {"quiet": True, "noplaylist": True, "no_warnings": True, "skip_download": True}
    js_rt = _js_runtimes()
    if js_rt:
        opts["js_runtimes"] = js_rt
    opts.update(_cookie_opts())  # cookies для Instagram/закрытых площадок (если заданы в env)
    return opts


def extract_meta(url: str) -> dict:
    """Метаданные поста БЕЗ скачивания медиа — для дешёвого скоринга ленты.

    Используется автопоиском по площадкам (tiktok/instagram): сначала скорим по
    описанию/автору, тяжёлую загрузку (Whisper/OCR/CLIP) делаем только для top-N в
    deep-режиме. -> {caption, author_handle, platform, thumb_url, url} или {} при
    SSRF-блоке/сбое (НЕ пробрасывает исключение).
    """
    if not _is_safe_public_url(url):
        return {}
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(_ydl_meta_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        return {
            "caption": info.get("description") or info.get("title") or "",
            "author_handle": (
                info.get("uploader") or info.get("uploader_id") or info.get("channel") or ""
            ),
            "platform": (info.get("extractor_key") or "").lower(),
            "thumb_url": info.get("thumbnail") or "",
            "url": info.get("webpage_url") or url,
        }
    except Exception:
        return {}


def list_account_videos(account_url: str, limit: int = 5) -> list:
    """URL последних видео аккаунта (extract_flat, без скачивания). [] при сбое.

    Надёжный путь автопоиска по TikTok: поисковики не индексируют отдельные ролики,
    а yt-dlp по странице аккаунта (`tiktok.com/@handle`) отдаёт ленту реальных видео.
    """
    return [p["url"] for p in list_account_posts(account_url, limit) if p.get("url")]


def list_account_posts(account_url: str, limit: int = 12) -> list:
    """Последние посты аккаунта с МЕТАДАННЫМИ за ОДИН запрос (extract_flat).

    Ключевое: extract_flat у TikTok отдаёт title/description (С ХЭШТЕГАМИ),
    uploader, thumbnail и счётчики прямо в записях ленты — без поштучных запросов
    к каждому видео (это и обходит IP-rate-limit, и даёт текст #тегов для скоринга
    по тегам/ключевым словам). -> list[dict{url,title,description,author_handle,
    thumb_url,view_count}], [] при сбое.
    """
    if not _is_safe_public_url(account_url):
        return []
    try:
        import yt_dlp

        opts = _ydl_meta_opts()
        opts["extract_flat"] = True
        opts["playlistend"] = max(1, int(limit))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(account_url, download=False)
        out = []
        for e in info.get("entries") or []:
            u = e.get("url") or e.get("webpage_url")
            if not u:
                continue
            thumbs = e.get("thumbnails") or []
            thumb = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else "")
            out.append({
                "url": u,
                "title": e.get("title") or "",
                "description": e.get("description") or "",
                "author_handle": e.get("uploader") or e.get("uploader_id") or e.get("channel") or "",
                "thumb_url": thumb or "",
                "view_count": e.get("view_count") or 0,
            })
        return out
    except Exception:
        return []


def handle_upload(file_bytes: bytes, filename: str):
    """Сохраняет загруженный файл, сэмплит кадры. -> (media_path, frames, meta)."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    ext = os.path.splitext(filename or "")[1] or ".mp4"
    media_path = os.path.join(str(MEDIA_DIR), f"{uuid.uuid4().hex}{ext}")
    with open(media_path, "wb") as f:
        f.write(file_bytes)
    frames = sample_frames(media_path)
    meta = {
        "caption": "",
        "author_handle": "",
        "platform": "upload",
        "thumb_url": "",
        "filename": filename or "",
    }
    return media_path, frames, meta


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_post(
    url: "str | None" = None,
    upload: "tuple | None" = None,
) -> Post:
    """§0.5 seam: собирает Post из ссылки (yt-dlp) ИЛИ загруженного файла.

    upload = (file_bytes, filename). Если ни url, ни upload не переданы -> ValueError.
    media_path сохраняется в Post; кадры в pipeline пересэмплируются по media_path.
    """
    if url:
        media_path, _frames, meta = fetch_link(url)
        platform = meta.get("platform") or "link"
        post_url = meta.get("url") or url
    elif upload is not None:
        file_bytes, filename = upload
        media_path, _frames, meta = handle_upload(file_bytes, filename)
        platform = meta.get("platform") or "upload"
        post_url = f"upload://{meta.get('filename') or filename or ''}"
    else:
        raise ValueError("fetch_post requires either url or upload")

    return Post(
        id=uuid.uuid4().hex,
        platform=platform,
        author_handle=meta.get("author_handle") or "",
        url=post_url,
        caption=normalize(meta.get("caption") or ""),
        posted_at=_now_iso(),
        media_path=media_path or None,
        thumb_url=meta.get("thumb_url") or None,
        source="live",
    )


# ---------------------------------------------------------------------------
# F8 — ОПЦИОНАЛЬНЫЙ best-effort live-fetch публичного Telegram-канала.
#
# Использует публичный web-preview t.me/s/<channel> (без авторизации, без новых
# зависимостей: html.parser + urllib). Весь путь за try/except — при любой ошибке
# возвращает [] и НЕ ломает вызывающий код. Для демо НИКОГДА не требуется:
# ядро КӨЗ работает на кэш-датасете (demo_posts.jsonl). §0.5: telegram-сущности
# извлекаются единственной реализацией app.extractors.text.extract_entities —
# здесь мы их не дублируем, только собираем Post из публичного текста.
# ---------------------------------------------------------------------------

_TGME_PREVIEW = "https://t.me/s/{channel}"
_CHANNEL_RE = re.compile(r"t\.me/(?:s/)?(?P<chan>[A-Za-z0-9_]+)", re.IGNORECASE)


def _http_get(url: str) -> str:
    """GET публичной страницы. Вынесен отдельно — тесты его monkeypatch'ат."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (KOZ media-watch)"}
    )
    with urllib.request.urlopen(req, timeout=8) as resp:  # nosec - публичный web-preview
        return resp.read().decode("utf-8", errors="replace")


class _TgPreviewParser(HTMLParser):
    """Best-effort парсер публичного t.me/s/<channel> превью.

    Для каждого сообщения собирает: текст (tgme_widget_message_text), РЕАЛЬНЫЙ id
    (data-post="channel/123" -> точная ссылка t.me/channel/123) и превью-картинку
    (background-image у *_photo_wrap). messages: list[{"id","text","thumb"}].
    """

    _PHOTO_RE = re.compile(r"url\(['\"]?([^'\")]+)")

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0  # глубина вложенности внутри блока текста
        self._buf: list = []
        self._cur_post = ""   # data-post текущего сообщения ("channel/123")
        self._cur_photo = ""  # фон-картинка текущего сообщения
        self.messages: list = []

    def handle_starttag(self, tag, attrs) -> None:
        a = dict(attrs)
        dp = a.get("data-post")
        if dp:
            self._cur_post = dp
            self._cur_photo = ""
        cls = a.get("class", "") or ""
        style = a.get("style", "") or ""
        if "_photo" in cls and "background-image" in style:
            m = self._PHOTO_RE.search(style)
            if m:
                self._cur_photo = m.group(1)
        if self._depth:
            # уже внутри текстового блока — считаем вложенные div'ы
            if tag == "div":
                self._depth += 1
        elif "tgme_widget_message_text" in cls:
            self._depth = 1
            self._buf = []

    def handle_endtag(self, tag) -> None:
        if self._depth and tag == "div":
            self._depth -= 1
            if self._depth == 0:
                txt = unescape("".join(self._buf)).strip()
                if txt:
                    self.messages.append(
                        {"id": self._cur_post, "text": txt, "thumb": self._cur_photo}
                    )

    def handle_data(self, data) -> None:
        if self._depth:
            self._buf.append(data)


def _channel_from_url(url: str) -> str:
    """Имя канала из любой t.me-ссылки (с /s/ или без). Фоллбэк — хвост URL."""
    m = _CHANNEL_RE.search(url or "")
    if m:
        return m.group("chan")
    return (url or "").rstrip("/").split("/")[-1]


def fetch_telegram_channel(url: str, limit: int = 10) -> list:
    """ОПЦИОНАЛЬНЫЙ live-путь: публичный web-preview Telegram, без авторизации.

    Best-effort: при ЛЮБОЙ ошибке (сеть/парсинг/битый URL) возвращает [] и НЕ
    пробрасывает исключение. Для демо не требуется — ядро работает на кэш-датасете.
    Возвращает list[Post] с platform="telegram", source="live".
    """
    try:
        channel = _channel_from_url(url)
        if not channel:
            return []
        html = _http_get(_TGME_PREVIEW.format(channel=channel))
        parser = _TgPreviewParser()
        parser.feed(html)
        posts: list = []
        for idx, msg in enumerate(parser.messages[:limit]):
            # поддержка старого формата (строка) и нового (dict)
            if isinstance(msg, dict):
                post_path = msg.get("id") or ""
                text = msg.get("text") or ""
                thumb = msg.get("thumb") or None
            else:
                post_path, text, thumb = "", msg, None
            msg_id = post_path.split("/")[-1] if "/" in post_path else ""
            real_url = (
                f"https://t.me/{post_path}" if "/" in post_path
                else f"https://t.me/{channel}"
            )
            posts.append(Post(
                id=f"tglive_{channel}_{msg_id or idx}",
                platform="telegram",
                author_handle="@" + channel,
                url=real_url,                     # РЕАЛЬНАЯ ссылка на конкретный пост
                caption=normalize(text),
                posted_at="",
                media_path=None,
                thumb_url=thumb,                  # РЕАЛЬНОЕ превью (если есть)
                source="live",
            ))
        return posts
    except Exception:
        # деградация: понятное пустое поведение, вызывающий код не падает
        return []


# Все t.me-ссылки на странице web-preview (посты, КНОПКИ, описание) — для снежного кома.
_TME_PAGE_RE = re.compile(
    r"t\.me/(s/)?(\+[A-Za-z0-9_\-]{5,}|joinchat/[A-Za-z0-9_\-]+|[A-Za-z0-9_]{4,32})",
    re.I,
)


def fetch_telegram_links(url: str) -> list:
    """Все t.me-ссылки со страницы web-preview канала (включая КНОПКИ и описание, где
    казино-каналы дают ссылки на ЧАТЫ/боты/инвайты) — для снежного кома по чатам.

    Возвращает сырые refs (имя канала/чата, '+hash' или 'joinchat/hash'), дедуп;
    [] при ошибке (не пробрасывает исключение).
    """
    try:
        channel = _channel_from_url(url)
        if not channel:
            return []
        html = _http_get(_TGME_PREVIEW.format(channel=channel))
        out, seen = [], set()
        for _s, ref in _TME_PAGE_RE.findall(html):
            low = ref.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(ref)
        return out
    except Exception:
        return []
