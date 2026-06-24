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
import re
from datetime import datetime, timezone

from app import config, db
from app.decision.scoring import score_post
from app.extractors.text import extract_entities
from app.ingestion.fetch import (
    _channel_from_url,
    fetch_telegram_channel,
    fetch_telegram_links,
)
from app.models import Extracted

# t.me-ссылки в тексте сообщений: каналы/чаты по имени (t.me/<name>, t.me/s/<name>)
# и приватные ИНВАЙТЫ в чаты (t.me/+<hash>, t.me/joinchat/<hash>) — для снежного кома.
_TME_LINK_RE = re.compile(r"t\.me/(s/)?(\+?[A-Za-z0-9_]{3,40}|joinchat/[A-Za-z0-9_\-]+)", re.I)
# Служебные пути t.me, которые не являются каналом/чатом.
_TME_STOP = {"s", "share", "addstickers", "addemoji", "proxy", "iv", "setlanguage",
             "socks", "login", "confirmphone", "bg", "contact"}
# Признаки ЧАТА/группы в username (казино-каналы линкуют свои чаты: @x_chat,
# @x_chatters, @x_obsuzhdenie). Такой username — и сканируем, и помечаем как чат-лид.
_CHAT_HINT = re.compile(r"(chat|chatter|group|talk|discus|обсужд|чат|болтал|flud|флуд)", re.I)


def _links_from_text(text: str, channels: set, chats: set) -> None:
    """Вытаскивает из текста t.me-ссылки. Приватные инвайты (+hash / joinchat) и
    username с признаком чата -> `chats` (лиды ЧАТОВ). Все публичные username ->
    `channels` (пробуем сканировать one-hop). Это и есть «не только каналы, но и чаты»."""
    for _s, raw in _TME_LINK_RE.findall(text or ""):
        low = raw.lower()
        if low.startswith("+") or low.startswith("joinchat/"):
            chats.add(raw)  # приватный чат-инвайт — лид (без входа не сканируется)
            continue
        name = raw.lstrip("@")
        if name.lower() in _TME_STOP:
            continue
        channels.add(name)
        if _CHAT_HINT.search(name):
            chats.add(name)  # публичный чат по username — лид (и его тоже сканируем)


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
        seen_channels = {_clean_channel(c).lower() for c in channels}
        found_channels: set = set()  # новые каналы/чаты-по-имени из сообщений (снежный ком)
        found_chats: set = set()     # приватные чат-инвайты (+hash/joinchat) — лиды
        for raw in channels:
            channel = _clean_channel(raw)
            if not channel:
                continue
            posts = fetch_telegram_channel(f"https://t.me/{channel}", limit=limit)
            # снежный ком по СТРАНИЦЕ канала (кнопки/описание — там ссылки на чаты/боты)
            _links_from_text(
                " ".join(f"t.me/{r}" for r in fetch_telegram_links(f"https://t.me/{channel}")),
                found_channels, found_chats,
            )
            added = 0
            flagged = 0
            for post in posts:
                cap = (post.caption or "").strip()
                if not cap:
                    continue
                # снежный ком: t.me-ссылки на другие каналы/чаты внутри сообщений
                _links_from_text(cap, found_channels, found_chats)
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
        new_channels = sorted(c for c in found_channels if c.lower() not in seen_channels)
        return {"added": total_added, "flagged": total_flagged, "channels": per_channel,
                "discovered_channels": new_channels, "discovered_chats": sorted(found_chats)}
    finally:
        if own:
            conn.close()
