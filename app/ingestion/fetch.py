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


def _load_cv2():
    """Лениво грузит cv2. При недоступности -> None."""
    try:
        import cv2

        return cv2
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
            cv2.imwrite(out, img)
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


def _is_safe_public_url(url: str) -> bool:
    """SSRF-защита: только http(s) к ПОДДЕРЖИВАЕМЫМ публичным платформам.

    Два контроля:
      1) жёсткий allowlist доменов площадок (_host_allowed) — атакующий не владеет
         их DNS, что снимает DNS-rebinding и redirect-на-internal как практический
         вектор (площадки не редиректят на приватные адреса);
      2) резолв хоста и отклонение приватных/loopback/link-local/reserved IP —
         defense-in-depth (напр. блок http://169.254.169.254/...).
    Полное IP-pinning несовместимо с yt-dlp (площадки активно используют
    CDN-редиректы на разные хосты), поэтому опорой служит allowlist.
    """
    try:
        p = urlparse(url or "")
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        if not _host_allowed(p.hostname):
            return False
        for _fam, _type, _proto, _canon, sockaddr in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            ):
                return False
        return True
    except Exception:
        return False


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

    Собирает текст всех блоков с классом tgme_widget_message_text. Учитывает
    вложенные <div> внутри текста сообщения, считая баланс открытий/закрытий.
    """

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0  # глубина вложенности внутри блока текста
        self._buf: list[str] = []
        self.messages: list[str] = []

    def handle_starttag(self, tag, attrs) -> None:
        cls = dict(attrs).get("class", "") or ""
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
                    self.messages.append(txt)

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
        for i, text in enumerate(parser.messages[:limit]):
            posts.append(Post(
                id=f"tglive_{channel}_{i}",
                platform="telegram",
                author_handle="@" + channel,
                url=f"https://t.me/{channel}/{i}" if i else f"https://t.me/{channel}",
                caption=normalize(text),
                posted_at="",
                media_path=None,
                thumb_url=None,
                source="live",
            ))
        return posts
    except Exception:
        # деградация: понятное пустое поведение, вызывающий код не падает
        return []
