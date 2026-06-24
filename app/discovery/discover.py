"""Движок АВТОНОМНОГО поиска опасных постов.

Сам ищет в интернете НАСТОЯЩИЕ посты (YouTube через yt-dlp ytsearch + публичные
Telegram-каналы через веб-поиск), скорит собственной моделью и кладёт их в ленту с
РЕАЛЬНЫМИ кликабельными ссылками (source="discovered"). Идемпотентно по url/контенту.
Без заглушек: ничего синтетического здесь не создаётся.
"""

import hashlib
from datetime import datetime, timezone

from app import config, db
from app.decision.scoring import score_post
from app.discovery.queries import DISCOVERY_QUERIES
from app.discovery.web import discover_telegram_channels
from app.discovery.youtube import search_youtube
from app.extractors.text import extract_entities, normalize
from app.models import Extracted, Post


def _pid(prefix: str, key: str) -> str:
    return f"{prefix}_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _ingest(conn, post: Post, cap: str):
    ents = extract_entities(cap)
    ex = Extracted(
        post_id=post.id, caption=cap, transcript="", ocr_text="",
        visual_concepts=[], combined_text=cap, entities=ents,
    )
    db.insert_post(conn, post)
    db.upsert_extracted(conn, ex)
    sc = score_post(post, ex, conn=conn)
    db.reveal_post(conn, post.id)
    return sc


def discover(conn, queries=None, per_query: int = 4, report=None,
             search_yt=None, with_telegram: bool = True, scan_tg=None) -> dict:
    """Автономный поиск. search_yt/scan_tg инъектируются в тестах (без сети)."""
    search_yt = search_yt or search_youtube
    queries = queries or DISCOVERY_QUERIES
    added = flagged = tg_added = 0
    samples: list = []
    n = len(queries)

    # 1) YouTube — реальные ролики с реальными ссылками.
    for i, q in enumerate(queries):
        if report:
            report(f"YouTube: {q[:38]}", 5 + int(70 * i / max(n, 1)))
        try:
            items = search_yt(q, per_query)
        except Exception:
            items = []
        for it in items:
            url = it.get("url")
            cap = normalize(it.get("caption") or "")
            if not url or not cap:
                continue
            pid = _pid("yt", url)
            if db.get_post(conn, pid) is not None:
                continue
            post = Post(
                id=pid, platform=it.get("platform", "youtube"),
                author_handle=it.get("author_handle") or "", url=url, caption=cap,
                posted_at=datetime.now(timezone.utc).isoformat(),
                media_path=None, thumb_url=it.get("thumb_url") or None, source="discovered",
            )
            sc = _ingest(conn, post, cap)
            added += 1
            if sc.risk >= config.ESCALATE_THRESHOLD:
                flagged += 1
            samples.append({"url": url, "platform": "youtube", "risk": sc.risk, "category": sc.category})

    # 2) Telegram — найти публичные каналы по запросам и реально их просканировать.
    if with_telegram:
        if report:
            report("поиск Telegram-каналов", 80)
        try:
            from app.ingestion.scan import scan_telegram as _scan
            scan_tg = scan_tg or _scan
            channels: list = []
            for q in queries[:3]:
                for ch in discover_telegram_channels(q, limit=3):
                    if ch not in channels:
                        channels.append(ch)
            if channels:
                if report:
                    report(f"скан {len(channels)} Telegram-каналов", 88)
                tg = scan_tg(channels[:8], conn=conn)
                tg_added = tg.get("added", 0)
                flagged += tg.get("flagged", 0)
        except Exception:
            pass

    if report:
        report("готово", 100)
    return {
        "added": added + tg_added, "youtube_added": added, "telegram_added": tg_added,
        "flagged": flagged, "queries": n, "samples": samples[:10],
    }
