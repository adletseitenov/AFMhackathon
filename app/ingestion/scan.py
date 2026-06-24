"""Реальный live-сканер публичных Telegram-каналов.

Тянет НАСТОЯЩИЕ посты публичного канала (через app.ingestion.fetch.fetch_telegram_channel —
web-preview t.me/s/<channel>, без авторизации), прогоняет их через ЕДИНСТВЕННУЮ реализацию
извлечения сущностей (app.extractors.text.extract_entities) и собственную модель
(app.decision.scoring.score_post), сохраняет как раскрытые посты в БД — они сразу
появляются в ленте/графе/трендах. Идемпотентно по содержимому (повторный скан того же
текста не дублирует).

Это «реально работает на реальных данных»: текст — основной сигнал скама (реклама казино,
обещания дохода, реф-схемы, t.me-воронки), и он анализируется по-настоящему, без тяжёлых
видео-моделей.
"""

import hashlib
from datetime import datetime, timezone

from app import config, db
from app.decision.scoring import score_post
from app.extractors.text import extract_entities
from app.ingestion.fetch import _channel_from_url, fetch_telegram_channel
from app.models import Extracted


def _clean_channel(raw: str) -> str:
    raw = (raw or "").strip()
    if "t.me" in raw.lower():
        return _channel_from_url(raw)
    return raw.lstrip("@").rstrip("/").split("/")[-1]


def _post_id(channel: str, caption: str) -> str:
    h = hashlib.sha1((caption or "").encode("utf-8")).hexdigest()[:10]
    return f"tg_{channel}_{h}"


def scan_telegram(channels, conn=None, limit: int = 12) -> dict:
    """Сканирует список публичных Telegram-каналов вживую.

    channels: список имён/ссылок ("@name", "name", "https://t.me/name").
    Возвращает {added, flagged, channels:[{channel, fetched, added, flagged}]}.
    Каждый добавленный пост реально заскорен собственной моделью и раскрыт (revealed=1).
    """
    own = conn is None
    if own:
        conn = db.connect()
    try:
        per_channel = []
        total_added = 0
        total_flagged = 0
        for raw in channels:
            channel = _clean_channel(raw)
            if not channel:
                continue
            posts = fetch_telegram_channel(f"https://t.me/{channel}", limit=limit)
            added = 0
            flagged = 0
            for post in posts:
                cap = (post.caption or "").strip()
                if not cap:
                    continue
                post.id = _post_id(channel, cap)
                if db.get_post(conn, post.id) is not None:
                    continue  # идемпотентно: этот текст уже сканировали
                if not post.posted_at:
                    post.posted_at = datetime.now(timezone.utc).isoformat()
                ents = extract_entities(cap)
                ex = Extracted(
                    post_id=post.id,
                    caption=cap,
                    transcript="",
                    ocr_text="",
                    visual_concepts=[],
                    combined_text=cap,
                    entities=ents,
                )
                db.insert_post(conn, post)
                db.upsert_extracted(conn, ex)
                score = score_post(post, ex, conn=conn)
                db.reveal_post(conn, post.id)
                added += 1
                if score.risk >= config.ESCALATE_THRESHOLD:
                    flagged += 1
            total_added += added
            total_flagged += flagged
            per_channel.append(
                {"channel": channel, "fetched": len(posts), "added": added, "flagged": flagged}
            )
        return {"added": total_added, "flagged": total_flagged, "channels": per_channel}
    finally:
        if own:
            conn.close()
