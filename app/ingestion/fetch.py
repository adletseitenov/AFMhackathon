"""Live-получение медиа: yt-dlp по ссылке + обработка загруженного файла.

Возвращает (media_path, frames, meta). Всё лениво (yt-dlp/cv2) и деградирует
мягко (A6): при отсутствии библиотек медиа="" / frames=[].

§0.5 seam: fetch_post(url=None, upload=None) -> Post — обёртка над fetch_link/
handle_upload, собирает Post. F4 /api/analyze вызывает fetch_post через атрибут
модуля, чтобы тесты могли monkeypatch'ить один seam.
"""

import os
import uuid
from datetime import datetime, timezone

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


def _ytdlp_download(url: str):
    """Лениво грузит yt-dlp и скачивает медиа. -> (media_path, meta) или ("", {})."""
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
