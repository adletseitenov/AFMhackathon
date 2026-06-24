"""Непрерывный скан: прогон ВСЕХ записей watchlist через соответствующие платформенные сборщики.

АРХИТЕКТУРА (мультиплатформенная):
  • telegram  → scan_telegram([все_telegram_таргеты], conn) ОДНИМ вызовом (совместимость тестов)
  • tiktok    → fetch_mod.list_account_posts(tiktok_url, limit) → _ingest_post(...)
  • instagram → fetch_mod.list_account_posts(instagram_url, limit) → _ingest_post(...)
  • twitch    → streaming_mod.fetch_twitch_videos(target, limit) → _ingest_post(...)
  • kick      → streaming_mod.fetch_kick_videos(target, limit) → _ingest_post(...)
  • youtube   → youtube_mod.search_youtube(target, limit) → _ingest_post(...)
  • operator  → youtube_mod.search_youtube(target) + fetch_mod.list_account_posts(tiktok_url)
                 + licensed_mod.licensed_operators(target) для флага licensed

КОНТРАКТ ВОЗВРАТА scan_watchlist():
  {
    "added": int,      # суммарно (telegram + non-telegram)
    "flagged": int,    # суммарно
    "channels": [...], # per-channel строки TELEGRAM (совместимость со старыми тестами)
    "entries": [       # сводка по ВСЕМ записям (новый ключ)
      {"target": str, "platform": str, "collected": int, "flagged": int, ?error: str}
    ]
  }

Module-attribute seams для monkeypatch в тестах:
  service.scan_telegram   — как раньше (тесты monkeypatch именно этот атрибут)
  service.fetch_mod       — app.ingestion.fetch
  service.streaming_mod   — app.discovery.streaming
  service.youtube_mod     — app.discovery.youtube
  service.licensed_mod    — app.decision.licensed
  service._ingest_post    — вспомогательная функция ингеста поста (monkeypatch в тестах)

ПРАВИЛА:
  • Никогда не бросает наружу
  • Telegram: record_scan_result (legacy stats) + record_entry_scan (per-entry stats)
  • Non-telegram: только record_entry_scan
  • Сбой stats никогда не ломает скан
  • Дедупликация постов по детерминированному post-id (sha1 platform+url) через db.get_post
"""

import hashlib
from datetime import datetime, timezone

import app.decision.licensed as licensed_mod
import app.discovery.streaming as streaming_mod
import app.discovery.youtube as youtube_mod
import app.ingestion.fetch as fetch_mod
from app.ingestion.scan import scan_telegram
from app.watchlist import stats, store

_EMPTY = {"added": 0, "flagged": 0, "channels": [], "entries": []}

# Лимит постов на одну запись за скан
_FETCH_LIMIT = 12


# --------------------------------------------------------------------------- #
# Вспомогательные функции                                                      #
# --------------------------------------------------------------------------- #

def _pid(platform: str, url: str) -> str:
    """Детерминированный post-id для дедупликации (sha1 платформа+url)."""
    key = f"{platform}:{url}"
    return "wl_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _account_url(platform: str, handle: str) -> str:
    """URL аккаунта для tiktok/instagram."""
    h = (handle or "").lstrip("@")
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{h}"
    return f"https://www.instagram.com/{h}/"


def _ingest_post(conn, post, cap, score_text=None):
    """Ингест + скоринг одного поста через стандартный пайплайн КӨЗ.

    Монкипатч-сим: тесты заменяют service._ingest_post на заглушку.
    Возвращает Score (с полями risk, category). При conn=None или ошибке БД
    возвращает фиктивный Score(risk=0, category="clean").
    """
    from app import db
    from app.decision.scoring import score_post
    from app.extractors.text import extract_entities
    from app.models import Extracted, Score

    try:
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
    except Exception:
        # Если БД недоступна (conn=None или сломана) — возвращаем нейтральный Score
        return Score(post_id=post.id, risk=0, category="clean",
                     class_probs={}, top_features=[])


def _ingest_posts_list(conn, platform: str, posts: list, author_fallback: str) -> dict:
    """Ингестирует список постов (формат list_account_posts или fetch_*_videos).

    Дедупликация по _pid + db.get_post. Считает added/flagged.
    Возвращает {"added": int, "flagged": int}.
    """
    from app import config, db
    from app.extractors.text import normalize
    from app.models import Post

    added = flagged = 0
    for p in posts:
        url = p.get("url")
        if not url:
            continue
        pid = _pid(platform, url)
        # дедупликация: если conn доступен — проверяем по БД
        if conn is not None:
            try:
                if db.get_post(conn, pid) is not None:
                    continue
            except Exception:
                pass
        # строим caption и score_text
        text = p.get("description") or p.get("title") or p.get("caption") or ""
        cap = normalize(text) if text else ""
        author = (p.get("author_handle") or author_fallback).lstrip("@")
        score_text = normalize(f"{author} {text}".strip()) if text else cap

        post = Post(
            id=pid, platform=platform, author_handle=author,
            url=url, caption=cap,
            posted_at=_now_iso(),
            media_path=None,
            thumb_url=p.get("thumb_url") or None,
            source="watchlist",
            view_count=p.get("view_count") or 0,
        )
        try:
            sc = _ingest_post(conn, post, cap, score_text=score_text)
        except Exception:
            continue
        added += 1
        if sc.risk >= config.ESCALATE_THRESHOLD:
            flagged += 1
    return {"added": added, "flagged": flagged}


# --------------------------------------------------------------------------- #
# Per-platform сборщики                                                        #
# --------------------------------------------------------------------------- #

def _scan_tiktok(target: str, conn) -> dict:
    """Сканирует TikTok-аккаунт через fetch_mod.list_account_posts."""
    url = _account_url("tiktok", target)
    posts = fetch_mod.list_account_posts(url, _FETCH_LIMIT)
    return _ingest_posts_list(conn, "tiktok", posts, target)


def _scan_instagram(target: str, conn) -> dict:
    """Сканирует Instagram-аккаунт через fetch_mod.list_account_posts."""
    url = _account_url("instagram", target)
    posts = fetch_mod.list_account_posts(url, _FETCH_LIMIT)
    return _ingest_posts_list(conn, "instagram", posts, target)


def _scan_youtube(target: str, conn) -> dict:
    """Ищет YouTube-видео по целевому запросу через youtube_mod.search_youtube."""
    items = youtube_mod.search_youtube(target, _FETCH_LIMIT)
    # search_youtube возвращает {caption, url, platform, ...}; приводим к общему формату
    posts = [
        {
            "url": it.get("url"),
            "title": it.get("caption") or it.get("title") or "",
            "description": it.get("caption") or "",
            "author_handle": it.get("author_handle") or "",
            "thumb_url": it.get("thumb_url") or "",
            "view_count": it.get("view_count") or 0,
        }
        for it in items
    ]
    return _ingest_posts_list(conn, "youtube", posts, target)


def _scan_twitch(target: str, conn) -> dict:
    """Сканирует Twitch-канал через streaming_mod.fetch_twitch_videos."""
    videos = streaming_mod.fetch_twitch_videos(target, _FETCH_LIMIT)
    return _ingest_posts_list(conn, "twitch", videos, target)


def _scan_kick(target: str, conn) -> dict:
    """Сканирует Kick-канал через streaming_mod.fetch_kick_videos."""
    videos = streaming_mod.fetch_kick_videos(target, _FETCH_LIMIT)
    return _ingest_posts_list(conn, "kick", videos, target)


def _scan_operator(target: str, conn) -> dict:
    """Сканирует оператора (бренд) на YouTube + TikTok; определяет licensed-флаг.

    Возвращает {"added", "flagged", "licensed": bool}.
    """
    added = flagged = 0

    # YouTube: ищем по имени бренда
    try:
        yt_res = _scan_youtube(target, conn)
        added += yt_res["added"]
        flagged += yt_res["flagged"]
    except Exception:
        pass

    # TikTok: best-effort по @brand
    try:
        tt_url = _account_url("tiktok", target)
        tt_posts = fetch_mod.list_account_posts(tt_url, _FETCH_LIMIT)
        tt_res = _ingest_posts_list(conn, "tiktok", tt_posts, target)
        added += tt_res["added"]
        flagged += tt_res["flagged"]
    except Exception:
        pass

    # Лицензионный статус: проверяем имя бренда через реестр АФМ
    try:
        is_licensed = bool(licensed_mod.licensed_operators(target))
    except Exception:
        is_licensed = False

    return {"added": added, "flagged": flagged, "licensed": is_licensed}


# --------------------------------------------------------------------------- #
# Главная функция                                                              #
# --------------------------------------------------------------------------- #

def scan_watchlist(conn=None) -> dict:
    """Сканирует ВСЕ записи watchlist по их платформе.

    Telegram-записи передаются в scan_telegram ОДНИМ батч-вызовом (совместимость тестов).
    Non-telegram записи сканируются поштучно через платформенные сборщики.

    Возврат: {added, flagged, channels (telegram per-channel rows), entries (все записи)}.
    Никогда не бросает исключение.
    """
    try:
        entries_list = store.list_entries()
    except Exception as exc:
        return {**_EMPTY, "error": f"watchlist read failed: {exc}"}

    if not entries_list:
        return dict(_EMPTY)

    # Разделяем telegram-записи (batched) и остальные (поштучно)
    telegram_targets = [e["target"] for e in entries_list if e["platform"] == "telegram"]
    non_telegram_entries = [e for e in entries_list if e["platform"] != "telegram"]

    total_added = 0
    total_flagged = 0
    telegram_channels_rows = []  # per-channel строки из scan_telegram (для ключа "channels")
    entry_summaries = []  # сводка для ключа "entries"
    scan_error: "str | None" = None  # ошибка telegram-скана (для совместимости с тестом never_raises)

    # --- TELEGRAM: один батч-вызов ---
    if telegram_targets:
        try:
            tg_res = scan_telegram(telegram_targets, conn=conn)
        except Exception as exc:
            scan_error = f"scan failed: {exc}"
            tg_res = {"added": 0, "flagged": 0, "channels": []}

        if not isinstance(tg_res, dict):
            tg_res = {"added": 0, "flagged": 0, "channels": []}
        tg_res.setdefault("added", 0)
        tg_res.setdefault("flagged", 0)
        tg_res.setdefault("channels", [])

        telegram_channels_rows = tg_res.get("channels", [])
        total_added += tg_res["added"]
        total_flagged += tg_res["flagged"]

        # Записываем legacy stats (record_scan_result) — для совместимости test_stats.py
        try:
            stats.record_scan_result(tg_res)
        except Exception:
            pass

        # Строим per-channel словарь для per-entry stats
        tg_by_channel = {
            row["channel"]: row
            for row in tg_res.get("channels", [])
            if isinstance(row, dict) and row.get("channel")
        }
        for target in telegram_targets:
            ch_row = tg_by_channel.get(target, {})
            ch_added = ch_row.get("added", 0) if ch_row else 0
            ch_flagged = ch_row.get("flagged", 0) if ch_row else 0
            # Per-entry stats для telegram
            try:
                stats.record_entry_scan(target, "telegram", ch_added, ch_flagged)
            except Exception:
                pass
            entry_summaries.append({
                "target": target,
                "platform": "telegram",
                "collected": ch_added,
                "flagged": ch_flagged,
            })

    # --- NON-TELEGRAM: поштучно ---
    for entry in non_telegram_entries:
        target = entry["target"]
        platform = entry["platform"]
        entry_summary = {"target": target, "platform": platform, "collected": 0, "flagged": 0}
        licensed = None

        try:
            if platform == "tiktok":
                res = _scan_tiktok(target, conn)
            elif platform == "instagram":
                res = _scan_instagram(target, conn)
            elif platform == "youtube":
                res = _scan_youtube(target, conn)
            elif platform == "twitch":
                res = _scan_twitch(target, conn)
            elif platform == "kick":
                res = _scan_kick(target, conn)
            elif platform == "operator":
                res = _scan_operator(target, conn)
                licensed = res.get("licensed", None)
            else:
                # неизвестная платформа — пропускаем тихо
                entry_summary["error"] = f"неизвестная платформа: {platform}"
                entry_summaries.append(entry_summary)
                continue

            e_added = res.get("added", 0)
            e_flagged = res.get("flagged", 0)
            total_added += e_added
            total_flagged += e_flagged
            entry_summary["collected"] = e_added
            entry_summary["flagged"] = e_flagged

            # Per-entry stats
            try:
                stats.record_entry_scan(target, platform, e_added, e_flagged, licensed=licensed)
            except Exception:
                pass

        except Exception as exc:
            entry_summary["error"] = str(exc)

        entry_summaries.append(entry_summary)

    result = {
        "added": total_added,
        "flagged": total_flagged,
        "channels": telegram_channels_rows,
        "entries": entry_summaries,
    }
    # Если telegram-скан упал — пробрасываем ошибку в результат (тест never_raises
    # проверяет "error" in result; остальные ключи при этом корректны).
    if scan_error is not None:
        result["error"] = scan_error
    return result
