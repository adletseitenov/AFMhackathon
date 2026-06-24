"""Движок АВТОНОМНОГО поиска опасных постов.

Сам ищет в интернете НАСТОЯЩИЕ посты (YouTube через yt-dlp ytsearch + публичные
Telegram-каналы через веб-поиск), скорит собственной моделью и кладёт их в ленту с
РЕАЛЬНЫМИ кликабельными ссылками (source="discovered"). Идемпотентно по url/контенту.
Без заглушек: ничего синтетического здесь не создаётся.
"""

import hashlib
from datetime import datetime, timezone

import app.extractors.pipeline as pipeline_mod
import app.ingestion.fetch as fetch_mod
from app import config, db
from app.decision.scoring import score_post
from app.discovery import web as web_mod
from app.discovery.queries import DISCOVERY_QUERIES
from app.discovery.web import discover_telegram_channels
from app.discovery.youtube import search_youtube
from app.extractors.text import extract_entities, normalize
from app.models import Extracted, Post

# Площадки коротких видео для платформенного поиска.
_VIDEO_PLATFORMS = {"tiktok", "instagram"}

# Поисковики НЕ индексируют отдельные tiktok/instagram-видео (и DDG-выдача
# часто недоступна), поэтому надёжный путь — нативный yt-dlp по странице
# АККАУНТА (как watchlist у Telegram). Курируемый список аккаунтов казино/
# букмекеров/HYIP, чья реклама в РК нелегальна (проверено: yt-dlp отдаёт их ленту).
_TIKTOK_SEED_ACCOUNTS = [
    "mostbet_official", "1win", "parimatch", "olimpbet", "betboom",
    "1xbet_global", "melbet_official", "pin_up_global", "mostbet",
]
# Instagram через yt-dlp закрыт логин-волом (extract data fails) — публичный
# автопоиск без входа невозможен; список оставлен как best-effort на будущее.
_INSTAGRAM_SEED_ACCOUNTS = [
    "1xbet", "mostbet", "parimatch",
]


def _account_url(platform: str, handle: str) -> str:
    h = handle.lstrip("@")
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{h}"
    return f"https://www.instagram.com/{h}/"


def _pid(prefix: str, key: str) -> str:
    return f"{prefix}_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _ingest(conn, post: Post, cap: str, score_text: "str | None" = None):
    """Ингест+скоринг поста. `cap` — отображаемый текст (caption), `score_text` —
    текст для скоринга (если None — равен cap). Для tiktok score_text включает
    хэндл аккаунта, чтобы сработал сигнал бренда казино/букмекера."""
    st = score_text if score_text is not None else cap
    ents = extract_entities(st)
    ex = Extracted(
        post_id=post.id, caption=cap, transcript="", ocr_text="",
        visual_concepts=[], combined_text=st, entities=ents,
    )
    db.insert_post(conn, post)
    db.upsert_extracted(conn, ex)
    sc = score_post(post, ex, conn=conn)
    db.reveal_post(conn, post.id)
    return sc


def _platform_accounts(platform: str) -> list:
    return _TIKTOK_SEED_ACCOUNTS if platform == "tiktok" else _INSTAGRAM_SEED_ACCOUNTS


def _ingest_video_platform(conn, platform: str, accounts: list, per_account: int,
                           seen_urls: set, by_category: dict, samples: list,
                           fresh_out: list, report=None) -> dict:
    """Автопоиск опасных видео на tiktok/instagram через РЕАЛЬНЫЙ yt-dlp по
    курируемым аккаунтам казино/букмекеров (поисковики такие видео не индексируют).

    Для каждого аккаунта: fetch_mod.list_account_videos (лента, без скачивания),
    далее по каждому видео fetch_mod.extract_meta (метаданные, без скачивания) ->
    скорим по «<хэндл> <описание>» (хэндл бренда поднимает сигнал казино/букмекера),
    ингестим. Свежие посты кладём в fresh_out для опционального deep-разбора. Любой
    сбой по аккаунту/видео проглатывается. -> {"added", "flagged"}.
    """
    added = flagged = 0
    n = max(1, len(accounts))
    for i, handle in enumerate(accounts):
        if report:
            report(f"{platform}: @{handle}", 5 + int(70 * i / n))
        try:
            vids = fetch_mod.list_account_videos(_account_url(platform, handle), per_account)
        except Exception:
            vids = []
        for url in vids:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            pid = _pid(platform, url)
            if db.get_post(conn, pid) is not None:
                continue
            try:
                meta = fetch_mod.extract_meta(url)
            except Exception:
                meta = {}
            if not meta:
                continue
            author = (meta.get("author_handle") or handle).lstrip("@")
            cap = normalize(meta.get("caption") or "")
            # Скоринг по «<хэндл> <описание>»: имя бренд-аккаунта (mostbet/1xbet/…)
            # само по себе — сильный сигнал нелегальной рекламы казино/букмекера.
            score_text = normalize(f"{author} {meta.get('caption') or ''}".strip())
            post = Post(
                id=pid, platform=platform, author_handle=author,
                url=meta.get("url") or url, caption=cap,
                posted_at=datetime.now(timezone.utc).isoformat(),
                media_path=None, thumb_url=meta.get("thumb_url") or None,
                source="discovered",
            )
            try:
                sc = _ingest(conn, post, cap, score_text=score_text)
            except Exception:
                continue
            added += 1
            if sc.category in by_category:
                by_category[sc.category] += 1
            if sc.risk >= config.ESCALATE_THRESHOLD:
                flagged += 1
            fresh_out.append({"post": post, "risk": sc.risk})
            samples.append({"url": post.url, "platform": platform,
                            "risk": sc.risk, "category": sc.category})
    return {"added": added, "flagged": flagged}


def _deep_analyze(conn, fresh: list, deep_top: int, report=None) -> dict:
    """ГЛУБОКИЙ мультимодальный разбор top-N свежедобавленных постов по риску.

    Для каждого: качаем видео (fetch_mod.fetch_link), и если медиа пришло —
    прогоняем pipeline_mod.extract на Post с этим media_path (РЕАЛЬНЫЕ
    Whisper-транскрипт + EasyOCR таблички + open-CLIP визуал), затем пере-скорим
    score_post и обновляем БД (upsert_extracted + новый скор). Ловит опасный
    контент, чей ЗАГОЛОВОК невинен, а аудио/видео/текст-на-экране — промо казино.

    Робастно: любой сбой загрузки/разбора по одному видео проглатывается (остаётся
    исходный текстовый скор). -> {"deep_analyzed": int, "deep_flagged_delta": int}.
    """
    analyzed = 0
    flagged_delta = 0
    top = sorted(fresh, key=lambda x: x["risk"], reverse=True)[:max(0, int(deep_top))]
    total = len(top)
    for i, item in enumerate(top):
        post = item["post"]
        prev_flagged = item["risk"] >= config.ESCALATE_THRESHOLD
        if report:
            report(f"глубокий разбор {i + 1}/{total}: {post.url[:40]}",
                   90 + int(8 * i / max(total, 1)))
        try:
            media_path, _frames, _meta = fetch_mod.fetch_link(post.url)
        except Exception:
            media_path = ""
        if not media_path:
            continue
        try:
            media_post = Post(
                id=post.id, platform=post.platform, author_handle=post.author_handle,
                url=post.url, caption=post.caption, posted_at=post.posted_at,
                media_path=media_path, thumb_url=post.thumb_url, source=post.source,
            )
            ex = pipeline_mod.extract(media_post, use_cache=False)
            db.upsert_extracted(conn, ex)
            sc = score_post(media_post, ex, conn=conn)
            db.reveal_post(conn, post.id)
            analyzed += 1
            now_flagged = sc.risk >= config.ESCALATE_THRESHOLD
            if now_flagged and not prev_flagged:
                flagged_delta += 1
        except Exception:
            # сбой разбора одного видео не ломает прогон — сохраняем текстовый скор
            continue
    return {"deep_analyzed": analyzed, "deep_flagged_delta": flagged_delta}


def discover(conn, queries=None, per_query: int = 4, report=None,
             search_yt=None, with_telegram: bool = True, scan_tg=None,
             platform: str = "all", deep: bool = False, deep_top: int = 3) -> dict:
    """Автономный поиск. search_yt/scan_tg инъектируются в тестах (без сети).

    platform: "all" (youtube+telegram), "youtube" (только ytsearch),
      "telegram" (только поиск+скан каналов), "tiktok"/"instagram"
      (best-effort site:<host> поиск видео-ссылок -> fetch_post -> ingest+score).
    deep: при True после YouTube-ингестии берёт top-`deep_top` свежих постов по
      риску, реально качает видео и прогоняет мультимодальный разбор
      (Whisper+EasyOCR+CLIP) с пере-скорингом — ловит промо, спрятанное в
      аудио/видео при невинном заголовке.
    """
    search_yt = search_yt or search_youtube
    queries = queries or DISCOVERY_QUERIES
    platform = (platform or "all").lower().strip()
    added = flagged = tg_added = platform_added = 0
    samples: list = []
    fresh_yt: list = []  # свежедобавленные YouTube-посты для глубокого разбора
    seen_urls: set = set()  # кросс-запросный дедуп url в пределах одного прогона
    by_category: dict = {c: 0 for c in config.CATEGORIES if c != "clean"}
    n = len(queries)

    do_youtube = platform in ("all", "youtube")
    do_telegram = with_telegram and platform in ("all", "telegram")
    do_video_platform = platform in _VIDEO_PLATFORMS

    # 1) YouTube — реальные ролики с реальными ссылками.
    if do_youtube:
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
                if not url or not cap or url in seen_urls:
                    continue
                seen_urls.add(url)
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
                if sc.category in by_category:
                    by_category[sc.category] += 1
                if sc.risk >= config.ESCALATE_THRESHOLD:
                    flagged += 1
                fresh_yt.append({"post": post, "risk": sc.risk})
                samples.append({"url": url, "platform": "youtube", "risk": sc.risk, "category": sc.category})

    # 1b) TikTok/Instagram — РЕАЛЬНЫЙ yt-dlp по курируемым аккаунтам казино/букмекеров
    #     (поисковики такие видео не индексируют; instagram закрыт логин-волом).
    fresh_platform: list = []
    if do_video_platform:
        accounts = _platform_accounts(platform)
        per_account = max(2, min(6, per_query + 1))
        res = _ingest_video_platform(
            conn, platform, accounts, per_account,
            seen_urls, by_category, samples, fresh_platform, report=report,
        )
        platform_added += res["added"]
        flagged += res["flagged"]

    # 2) Telegram — найти публичные каналы по запросам и реально их просканировать.
    if do_telegram:
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

    # 3) ГЛУБОКИЙ мультимодальный разбор top-постов (опционально) — youtube + tiktok.
    deep_analyzed = 0
    deep_pool = fresh_yt + fresh_platform
    if deep and deep_pool:
        try:
            dres = _deep_analyze(conn, deep_pool, deep_top, report=report)
            deep_analyzed = dres["deep_analyzed"]
            flagged += dres["deep_flagged_delta"]
        except Exception:
            pass

    # Честная нота, если по площадке ничего не нашли (instagram закрыт логин-волом,
    # либо аккаунты временно недоступны) — чтобы UI не показывал немой «0».
    note = ""
    if do_video_platform and platform_added == 0:
        if platform == "instagram":
            note = ("Instagram не отдаёт публичный автопоиск без входа. "
                    "Используйте «Живую проверку» ссылки на reel, либо TikTok/YouTube/Telegram.")
        else:
            note = (f"{platform}: аккаунты сейчас недоступны для извлечения. "
                    "Попробуйте позже или используйте «Живую проверку» ссылки.")

    if report:
        report("готово", 100)
    result = {
        "added": added + tg_added + platform_added,
        "youtube_added": added, "telegram_added": tg_added,
        "platform_added": platform_added, "platform": platform,
        "deep_analyzed": deep_analyzed,
        "flagged": flagged, "queries": n, "samples": samples[:10],
        "by_category": by_category,
    }
    if note:
        result["note"] = note
    return result
