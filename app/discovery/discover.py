"""Движок АВТОНОМНОГО поиска опасных постов.

Сам ищет в интернете НАСТОЯЩИЕ посты (YouTube через yt-dlp ytsearch + публичные
Telegram-каналы через веб-поиск), скорит собственной моделью и кладёт их в ленту с
РЕАЛЬНЫМИ кликабельными ссылками (source="discovered"). Идемпотентно по url/контенту.
Без заглушек: ничего синтетического здесь не создаётся.
"""

import hashlib
from datetime import datetime, timezone

import app.discovery.catalog as catalog
import app.discovery.streaming as streaming_mod
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

# Стриминговые площадки (гемблинг-стримы slots/casino/BONUS HUNT) — отдельный
# источник: VOD-ы курируемых стримеров казино/слотов через app.discovery.streaming.
_STREAMING_PLATFORMS = {"twitch", "kick"}

# Площадки, поддерживающие проверку ПРЯМОГО ЭФИРА (content_type="live") через
# streaming_mod.fetch_live(platform, account) — ингестим только тех, кто СЕЙЧАС в эфире.
_LIVE_PLATFORMS = {"tiktok", "twitch", "kick", "instagram"}

# Сид-аккаунты/стримеры — ЕДИНЫЙ источник истины: app/discovery/catalog.py.
# Дублируем в module-level имена, чтобы остались монкипатч-семы существующих тестов
# (tests monkeypatch d._TIKTOK_SEED_ACCOUNTS / d._KICK_SEED_STREAMERS / ...).
# Имя стримера + заголовок дают сигнал казино при скоринге.
_TWITCH_SEED_STREAMERS = list(catalog.SEED_ACCOUNTS["twitch"])
_KICK_SEED_STREAMERS = list(catalog.SEED_ACCOUNTS["kick"])
_TIKTOK_SEED_ACCOUNTS = list(catalog.SEED_ACCOUNTS["tiktok"])
_INSTAGRAM_SEED_ACCOUNTS = list(catalog.SEED_ACCOUNTS["instagram"])

# Telegram: DDG-поиск каналов часто недоступен (таймаут), поэтому база — курируемые
# публичные казино/букмекер-каналы (скан через t.me/s/ web-preview), а ДАЛЬШЕ
# снежный ком по t.me-ссылкам в сообщениях находит НОВЫЕ каналы и ЧАТЫ.
_TELEGRAM_SEED_CHANNELS = [
    "MelBet_official", "mostbet_casino", "azino777", "slottica", "melbet",
    "joycasino", "riobet",
]


def _account_url(platform: str, handle: str) -> str:
    h = handle.lstrip("@")
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{h}"
    return f"https://www.instagram.com/{h}/"


def _pid(prefix: str, key: str) -> str:
    return f"{prefix}_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def _sort_key_recent(item: dict):
    """Ключ свежести: дата публикации (timestamp/upload_date/posted_at), новее
    раньше. Отсутствие даты -> в конец. Строки сравнимы лексикографически
    (ISO-дата) — берём сырое значение, чтобы не падать на форматах."""
    for k in ("timestamp", "upload_date", "posted_at", "date"):
        v = item.get(k)
        if v:
            return (1, str(v))
    return (0, "")


def _sort_items(items: list, sort: str) -> list:
    """Упорядочивает найденные элементы перед ингестом.

    relevance -> как есть (порядок выдачи источника). recent -> сначала свежие
    (по дате публикации). popular -> по убыванию view_count. Сортировка
    СТАБИЛЬНА (сохраняет исходный порядок при равенстве)."""
    if not items:
        return items
    s = (sort or "relevance").lower().strip()
    if s == "popular":
        return sorted(items, key=lambda it: int(it.get("view_count") or 0), reverse=True)
    if s == "recent":
        return sorted(items, key=_sort_key_recent, reverse=True)
    return items


def _ingest(conn, post: Post, cap: str, score_text: "str | None" = None,
            min_risk: int = 0, max_risk: int = 100):
    """Ингест+скоринг поста. `cap` — отображаемый текст (caption), `score_text` —
    текст для скоринга (если None — равен cap). Для tiktok score_text включает
    хэндл аккаунта, чтобы сработал сигнал бренда казино/букмекера.

    Фильтр УРОВНЯ ОПАСНОСТИ: пост раскрывается (reveal -> попадает в ленту и в
    результаты поиска) и возвращается его Score ТОЛЬКО если риск в выбранной полосе
    [min_risk, max_risk]. Иначе пост сохранён в БД (фон его зафиксировал), но НЕ
    раскрыт и функция возвращает None — вызывающий такой пост пропускает."""
    st = score_text if score_text is not None else cap
    ents = extract_entities(st)
    ex = Extracted(
        post_id=post.id, caption=cap, transcript="", ocr_text="",
        visual_concepts=[], combined_text=st, entities=ents,
    )
    db.insert_post(conn, post)
    db.upsert_extracted(conn, ex)
    sc = score_post(post, ex, conn=conn)
    if not (min_risk <= sc.risk <= max_risk):
        return None  # вне выбранной полосы опасности — не раскрываем, не показываем
    db.reveal_post(conn, post.id)
    return sc


def _platform_accounts(platform: str) -> list:
    return _TIKTOK_SEED_ACCOUNTS if platform == "tiktok" else _INSTAGRAM_SEED_ACCOUNTS


def _ingest_video_platform(conn, platform: str, accounts: list, per_account: int,
                           seen_urls: set, by_category: dict, samples: list,
                           fresh_out: list, report=None, sort: str = "relevance",
                           min_risk: int = 0, max_risk: int = 100) -> dict:
    """Автопоиск опасных видео на tiktok/instagram через РЕАЛЬНЫЙ yt-dlp по
    курируемым аккаунтам казино/букмекеров (поисковики такие видео не индексируют).

    Для каждого аккаунта: fetch_mod.list_account_posts отдаёт ленту с МЕТАДАННЫМИ
    (title/описание С ХЭШТЕГАМИ, uploader, превью) за ОДИН запрос — без поштучных
    обращений к каждому видео (это обходит IP-rate-limit и даёт текст #тегов).
    Скорим по «<хэндл> <описание+теги>» (бренд-хэндл + #хэштеги ловят казино/
    букмекера — поиск идёт не только по имени, но и по тегам/ключевым словам).
    Свежие посты кладём в fresh_out для опционального deep-разбора. Любой сбой по
    аккаунту проглатывается. -> {"added","flagged","found","accounts_ok"}.
    """
    added = flagged = found = accounts_ok = 0
    n = max(1, len(accounts))
    for i, handle in enumerate(accounts):
        if report:
            report(f"{platform}: @{handle}", 5 + int(70 * i / n))
        try:
            posts = fetch_mod.list_account_posts(_account_url(platform, handle), per_account)
        except Exception:
            posts = []
        if posts:
            accounts_ok += 1
            found += len(posts)
        posts = _sort_items(posts, sort)
        for p in posts:
            url = p.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            pid = _pid(platform, url)
            if db.get_post(conn, pid) is not None:
                continue
            author = (p.get("author_handle") or handle).lstrip("@")
            text = p.get("description") or p.get("title") or ""
            cap = normalize(text)
            # «<хэндл> <описание+#теги>»: имя бренд-аккаунта и хэштеги (#1xbet, #casino,
            # #ставки) — сильные сигналы нелегальной рекламы казино/букмекера.
            score_text = normalize(f"{author} {text}".strip())
            post = Post(
                id=pid, platform=platform, author_handle=author,
                url=url, caption=cap,
                posted_at=datetime.now(timezone.utc).isoformat(),
                media_path=None, thumb_url=p.get("thumb_url") or None,
                source="discovered", view_count=p.get("view_count") or 0,
            )
            try:
                sc = _ingest(conn, post, cap, score_text=score_text,
                             min_risk=min_risk, max_risk=max_risk)
            except Exception:
                continue
            if sc is None:
                continue  # вне выбранной полосы опасности
            added += 1
            if sc.category in by_category:
                by_category[sc.category] += 1
            if sc.risk >= config.ESCALATE_THRESHOLD:
                flagged += 1
            fresh_out.append({"post": post, "risk": sc.risk})
            samples.append({"id": post.id, "url": post.url, "platform": platform,
                            "risk": sc.risk, "category": sc.category})
    return {"added": added, "flagged": flagged, "found": found,
            "accounts_ok": accounts_ok}


def _streaming_streamers(platform: str) -> list:
    return _KICK_SEED_STREAMERS if platform == "kick" else _TWITCH_SEED_STREAMERS


def _fetch_streaming(platform: str, streamer: str, per_streamer: int) -> list:
    """РОБАСТНЫЙ вызов сборщика VOD-ов нужной площадки (через module-attribute, чтобы
    тесты monkeypatch'или streaming_mod.fetch_*). [] при сбое (не пробрасывает)."""
    try:
        if platform == "kick":
            return streaming_mod.fetch_kick_videos(streamer, per_streamer)
        return streaming_mod.fetch_twitch_videos(streamer, per_streamer)
    except Exception:
        return []


def _ingest_streaming(conn, platform: str, streamers: list, per_streamer: int,
                      seen_urls: set, by_category: dict, samples: list,
                      fresh_out: list, report=None, sort: str = "relevance",
                      min_risk: int = 0, max_risk: int = 100) -> dict:
    """Автопоиск гемблинг-контента на TWITCH/KICK по курируемым стримерам казино/слотов.

    Для каждого стримера тянем VOD-ы (streaming_mod.fetch_kick_videos /
    fetch_twitch_videos — РОБАСТНЫ, [] при сбое), строим Post(platform=<twitch|kick>,
    source="discovered") и скорим по «<стример> <заголовок>» (имя гемблинг-стримера
    + казино-термины заголовка дают сигнал казино). Дедуп по seen_urls + _pid +
    db.get_post. Свежие посты кладём в fresh_out для опционального deep-разбора. Любой
    сбой по стримеру/видео проглатывается. -> {"added","flagged"}.
    """
    added = flagged = 0
    n = max(1, len(streamers))
    for i, streamer in enumerate(streamers):
        if report:
            report(f"{platform}: {streamer}", 5 + int(70 * i / n))
        videos = _sort_items(_fetch_streaming(platform, streamer, per_streamer), sort)
        for v in videos:
            url = v.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            pid = _pid(platform, url)
            if db.get_post(conn, pid) is not None:
                continue
            author = (v.get("author_handle") or streamer).lstrip("@")
            title = v.get("title") or v.get("description") or ""
            cap = normalize(title)
            # «<стример> <заголовок>»: имя гемблинг-стримера + казино-термины
            # (STAKE/roobet/BONUS HUNT/слоты) — сильные сигналы рекламы казино.
            score_text = normalize(f"{author} {title}".strip())
            post = Post(
                id=pid, platform=platform, author_handle=author,
                url=url, caption=cap,
                posted_at=datetime.now(timezone.utc).isoformat(),
                media_path=None, thumb_url=v.get("thumb_url") or None,
                source="discovered", view_count=v.get("view_count") or 0,
            )
            try:
                sc = _ingest(conn, post, cap, score_text=score_text,
                             min_risk=min_risk, max_risk=max_risk)
            except Exception:
                continue
            if sc is None:
                continue  # вне выбранной полосы опасности
            added += 1
            if sc.category in by_category:
                by_category[sc.category] += 1
            if sc.risk >= config.ESCALATE_THRESHOLD:
                flagged += 1
            fresh_out.append({"post": post, "risk": sc.risk})
            samples.append({"id": post.id, "url": post.url, "platform": platform,
                            "risk": sc.risk, "category": sc.category})
    return {"added": added, "flagged": flagged}


def _live_accounts(platform: str) -> list:
    """Сид-аккаунты/стримеры площадки для проверки ПРЯМОГО ЭФИРА (берём те же
    module-level списки, что и для VOD/постов — они монкипатчатся в тестах)."""
    if platform == "tiktok":
        return _TIKTOK_SEED_ACCOUNTS
    if platform == "instagram":
        return _INSTAGRAM_SEED_ACCOUNTS
    if platform == "kick":
        return _KICK_SEED_STREAMERS
    return _TWITCH_SEED_STREAMERS  # twitch


def _fetch_live(platform: str, account: str):
    """РОБАСТНЫЙ вызов проверки эфира через module-attribute (тесты monkeypatch'ят
    streaming_mod.fetch_live). Возвращает dict с live=True или None/[]/исключение
    -> None (сборщик ещё может отсутствовать, пока его делает streaming-агент)."""
    fn = getattr(streaming_mod, "fetch_live", None)
    if fn is None:
        return None
    try:
        return fn(platform, account)
    except Exception:
        return None


def _ingest_live(conn, platform: str, accounts: list, seen_urls: set,
                 by_category: dict, samples: list, fresh_out: list,
                 report=None, min_risk: int = 0, max_risk: int = 100) -> dict:
    """Автопоиск ПРЯМЫХ ЭФИРОВ: для каждого сид-аккаунта зовём
    streaming_mod.fetch_live(platform, account); ингестим ТОЛЬКО тех, кто СЕЙЧАС в
    эфире (live=True). Пост помечается флагом live (в samples и fresh_out — модель
    Post его не несёт). Скорим по «<аккаунт> <заголовок эфира>». Дедуп по
    seen_urls + _pid + db.get_post. Любой сбой по аккаунту проглатывается.
    -> {"added","flagged"}.
    """
    added = flagged = 0
    n = max(1, len(accounts))
    for i, account in enumerate(accounts):
        if report:
            report(f"{platform} эфир: {account}", 5 + int(70 * i / n))
        info = _fetch_live(platform, account)
        r = _ingest_one_live(conn, platform, info, account, seen_urls,
                             by_category, samples, fresh_out, min_risk, max_risk)
        added += r["added"]
        flagged += r["flagged"]
    return {"added": added, "flagged": flagged}


def _ingest_one_live(conn, platform: str, info, fallback_handle: str, seen_urls: set,
                     by_category: dict, samples: list, fresh_out: list,
                     min_risk: int = 0, max_risk: int = 100) -> dict:
    """Ингест ОДНОГО live-инфо dict (от fetch_live ИЛИ search_kick_live).

    Ингестим только тех, кто СЕЙЧАС в эфире (live=True) и у кого есть url; дедуп по
    seen_urls + _pid + db.get_post; фильтр полосы опасности через _ingest. Пост
    помечается флагом live. -> {"added","flagged"} (0/0 если офлайн/дубль/вне полосы)."""
    if not isinstance(info, dict) or not info.get("live"):
        return {"added": 0, "flagged": 0}
    url = info.get("url")
    if not url or url in seen_urls:
        return {"added": 0, "flagged": 0}
    seen_urls.add(url)
    pid = _pid(platform, url)
    if db.get_post(conn, pid) is not None:
        return {"added": 0, "flagged": 0}
    author = (info.get("author_handle") or fallback_handle or "").lstrip("@")
    title = info.get("title") or info.get("description") or ""
    cap = normalize(title)
    # Контекст категории: стрим найден в ГЕМБЛИНГ-категории площадки (Twitch Slots/
    # Virtual Casino, Kick «слоты/казино») — это фактический сигнал, что контент
    # азартный, даже если в заголовке нет явных ключевых слов. Добавляем к скорингу,
    # чтобы анализатор корректно квалифицировал live-стрим как гемблинг.
    cat_ctx = info.get("category_context") or ""
    score_text = normalize(f"{author} {title} {cat_ctx}".strip())
    post = Post(
        id=pid, platform=platform, author_handle=author,
        url=url, caption=cap,
        posted_at=datetime.now(timezone.utc).isoformat(),
        media_path=None, thumb_url=info.get("thumb_url") or None,
        source="discovered", view_count=info.get("view_count") or 0,
        live=True,
    )
    try:
        sc = _ingest(conn, post, cap, score_text=score_text,
                     min_risk=min_risk, max_risk=max_risk)
    except Exception:
        return {"added": 0, "flagged": 0}
    if sc is None:
        return {"added": 0, "flagged": 0}  # вне выбранной полосы опасности
    flagged = 1 if sc.risk >= config.ESCALATE_THRESHOLD else 0
    if sc.category in by_category:
        by_category[sc.category] += 1
    fresh_out.append({"post": post, "risk": sc.risk, "live": True})
    samples.append({"id": post.id, "url": post.url, "platform": platform, "risk": sc.risk,
                    "category": sc.category, "live": True})
    return {"added": 1, "flagged": flagged}


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
                view_count=post.view_count,
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
             platform: str = "all", deep: bool = False, deep_top: int = 3,
             country: str = "all", categories=None,
             content_type: str = "all", sort: str = "relevance",
             min_risk: int = 0, max_risk: int = 100) -> dict:
    """Автономный НАСТРАИВАЕМЫЙ поиск. search_yt/scan_tg инъектируются в тестах (без сети).

    platform: "all" (youtube+telegram), "youtube" (только ytsearch),
      "telegram" (только поиск+скан каналов), "tiktok"/"instagram"
      (yt-dlp по курируемым аккаунтам -> ingest+score),
      "twitch"/"kick" (VOD-ы курируемых гемблинг-стримеров казино/слотов через
      app.discovery.streaming -> ingest+score).
    country/categories: задают НАБОР запросов через catalog.build_queries(country,
      categories) — локализованные запросы по стране (kz/ru/all) и категориям
      (all/casino/pyramid/fraud/crypto). Применяется ТОЛЬКО когда явный `queries`
      не передан; иначе используется переданный список (фоллбэк — DISCOVERY_QUERIES).
    content_type: "all"/"video" — VOD/посты (как раньше); "live" — для
      tiktok/twitch/kick/instagram проверяем ПРЯМОЙ ЭФИР через
      streaming_mod.fetch_live и ингестим только тех, кто СЕЙЧАС в эфире (флаг live).
    sort: "relevance" (как есть), "recent" (свежее раньше), "popular"
      (по view_count) — порядок элементов перед ингестом.
    deep: при True после YouTube-ингестии берёт top-`deep_top` свежих постов по
      риску, реально качает видео и прогоняет мультимодальный разбор
      (Whisper+EasyOCR+CLIP) с пере-скорингом — ловит промо, спрятанное в
      аудио/видео при невинном заголовке.
    """
    search_yt = search_yt or search_youtube
    # Источник запросов: явный queries > реестр (страна+категории) > фоллбэк-список.
    if queries is None:
        queries = catalog.build_queries(country, categories) or DISCOVERY_QUERIES
    platform = (platform or "all").lower().strip()
    content_type = (content_type or "all").lower().strip()
    sort = (sort or "relevance").lower().strip()
    # полоса УРОВНЯ ОПАСНОСТИ [min_risk, max_risk]: клампим в [0,100] и упорядочиваем.
    # По умолчанию [0,100] -> фильтр выключен (поведение не меняется).
    try:
        min_risk = max(0, min(100, int(min_risk)))
    except (TypeError, ValueError):
        min_risk = 0
    try:
        max_risk = max(0, min(100, int(max_risk)))
    except (TypeError, ValueError):
        max_risk = 100
    if min_risk > max_risk:
        min_risk, max_risk = max_risk, min_risk
    added = flagged = tg_added = platform_added = 0
    samples: list = []
    fresh_yt: list = []  # свежедобавленные YouTube-посты для глубокого разбора
    seen_urls: set = set()  # кросс-запросный дедуп url в пределах одного прогона
    by_category: dict = {c: 0 for c in config.CATEGORIES if c != "clean"}
    n = len(queries)

    # content_type="live" -> для tiktok/twitch/kick/instagram ищем ПРЯМЫЕ ЭФИРЫ
    # (а не VOD/посты). Для youtube/telegram «эфирного» сид-режима нет — там работаем
    # как обычно (VOD/посты), но при платформенном live VOD-ветку отключаем.
    want_live = content_type == "live"
    # Какие площадки проверять на ПРЯМОЙ ЭФИР: конкретная live-площадка -> только она;
    # «all»/пусто -> ВСЕ live-площадки (иначе live при выбранном «Все» давал 0 находок).
    # youtube/telegram эфирного сид-режима не имеют -> [] (честный 0 + нота).
    if want_live:
        _LIVE_ORDER = [p for p in ("twitch", "kick", "tiktok", "instagram") if p in _LIVE_PLATFORMS]
        if platform in _LIVE_PLATFORMS:
            live_platforms = [platform]
        elif platform in ("all", ""):
            live_platforms = _LIVE_ORDER
        else:
            live_platforms = []
    else:
        live_platforms = []
    do_live = bool(live_platforms)

    do_youtube = platform in ("all", "youtube") and not want_live
    do_telegram = (with_telegram and platform in ("all", "telegram") and not want_live)
    do_video_platform = platform in _VIDEO_PLATFORMS and not want_live
    do_streaming = platform in _STREAMING_PLATFORMS and not want_live

    # 1) YouTube — реальные ролики с реальными ссылками.
    if do_youtube:
        for i, q in enumerate(queries):
            if report:
                report(f"YouTube: {q[:38]}", 5 + int(70 * i / max(n, 1)))
            try:
                items = _sort_items(search_yt(q, per_query), sort)
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
                    view_count=it.get("view_count") or 0,
                )
                sc = _ingest(conn, post, cap, min_risk=min_risk, max_risk=max_risk)
                if sc is None:
                    continue  # вне выбранной полосы опасности
                added += 1
                if sc.category in by_category:
                    by_category[sc.category] += 1
                if sc.risk >= config.ESCALATE_THRESHOLD:
                    flagged += 1
                fresh_yt.append({"post": post, "risk": sc.risk})
                samples.append({"id": post.id, "url": url, "platform": "youtube", "risk": sc.risk, "category": sc.category})

    # 1b) TikTok/Instagram — РЕАЛЬНЫЙ yt-dlp по курируемым аккаунтам казино/букмекеров
    #     (поисковики такие видео не индексируют; instagram закрыт логин-волом).
    fresh_platform: list = []
    platform_found = platform_accounts_ok = 0
    if do_video_platform:
        accounts = _platform_accounts(platform)
        # Тянем поглубже в ленту каждого аккаунта (extract_flat = 1 запрос/аккаунт,
        # так что это дёшево) — повторные прогоны находят НОВЫЕ ролики.
        per_account = max(10, min(20, per_query * 4))
        res = _ingest_video_platform(
            conn, platform, accounts, per_account,
            seen_urls, by_category, samples, fresh_platform, report=report, sort=sort,
            min_risk=min_risk, max_risk=max_risk,
        )
        platform_added += res["added"]
        flagged += res["flagged"]
        platform_found = res.get("found", 0)
        platform_accounts_ok = res.get("accounts_ok", 0)

    # 1c) Twitch/Kick — VOD-ы курируемых гемблинг-стримеров казино/слотов
    #     (slots/casino/BONUS HUNT под STAKE/roobet — нелегальная реклама казино).
    fresh_streaming: list = []
    if do_streaming:
        streamers = _streaming_streamers(platform)
        # Тянем поглубже в архив каждого стримера (1 запрос/стример) — повторные
        # прогоны находят НОВЫЕ VOD-ы. Тот же кап, что и для tiktok.
        per_streamer = max(10, min(20, per_query * 4))
        sres = _ingest_streaming(
            conn, platform, streamers, per_streamer,
            seen_urls, by_category, samples, fresh_streaming, report=report, sort=sort,
            min_risk=min_risk, max_risk=max_risk,
        )
        platform_added += sres["added"]
        flagged += sres["flagged"]

    # 1d) ПРЯМЫЕ ЭФИРЫ (content_type="live") — tiktok/twitch/kick/instagram:
    #     проверяем эфир сид-аккаунтов через streaming_mod.fetch_live, ингестим
    #     только тех, кто СЕЙЧАС в эфире (пост помечен флагом live).
    fresh_live: list = []
    live_added = 0
    if do_live:
        for lp in live_platforms:
            lres = _ingest_live(
                conn, lp, _live_accounts(lp),
                seen_urls, by_category, samples, fresh_live, report=report,
                min_risk=min_risk, max_risk=max_risk,
            )
            live_added += lres["added"]
            platform_added += lres["added"]
            flagged += lres["flagged"]
        # ДИНАМИЧЕСКИЙ поиск ЖИВЫХ гемблинг-эфиров на Kick: сид-стримеры часто офлайн,
        # а поиск по «слоты/казино/stake» находит тех, кто СЕЙЧАС в эфире (надёжный
        # источник живых находок). Best-effort: любой сбой не валит автопоиск.
        if "kick" in live_platforms:
            if report:
                report("kick: поиск живых казино-эфиров", 90)
            try:
                search_live = getattr(streaming_mod, "search_kick_live", None)
                for info in (search_live() if search_live else []) or []:
                    r = _ingest_one_live(
                        conn, "kick", info, (info or {}).get("author_handle", ""),
                        seen_urls, by_category, samples, fresh_live, min_risk, max_risk,
                    )
                    live_added += r["added"]
                    platform_added += r["added"]
                    flagged += r["flagged"]
            except Exception:
                pass
        # ДИНАМИЧЕСКИЙ поиск ЖИВЫХ Twitch-эфиров в гемблинг-категориях (Slots/Virtual
        # Casino) через публичный Twitch GraphQL — сид-стримеры часто офлайн. Best-effort.
        if "twitch" in live_platforms:
            if report:
                report("twitch: поиск живых казино-эфиров", 92)
            try:
                search_tw = getattr(streaming_mod, "search_twitch_live", None)
                for info in (search_tw() if search_tw else []) or []:
                    r = _ingest_one_live(
                        conn, "twitch", info, (info or {}).get("author_handle", ""),
                        seen_urls, by_category, samples, fresh_live, min_risk, max_risk,
                    )
                    live_added += r["added"]
                    platform_added += r["added"]
                    flagged += r["flagged"]
            except Exception:
                pass

    # 2) Telegram — курируемые казино-каналы (надёжно) + DDG best-effort + СНЕЖНЫЙ КОМ
    #    по t.me-ссылкам в сообщениях: находит НОВЫЕ каналы И ЧАТЫ (не только каналы).
    tg_chats: list = []
    if do_telegram:
        if report:
            report("скан Telegram-каналов", 80)
        try:
            from app.ingestion.scan import scan_telegram as _scan
            scan_tg = scan_tg or _scan
            channels: list = list(_TELEGRAM_SEED_CHANNELS)
            # DDG-поиск каналов часто недоступен (таймаут) — best-effort, не критично.
            try:
                for q in queries[:3]:
                    for ch in discover_telegram_channels(q, limit=3):
                        if ch not in channels:
                            channels.append(ch)
            except Exception:
                pass
            tg = scan_tg(channels[:12], conn=conn)
            tg_added = tg.get("added", 0)
            flagged += tg.get("flagged", 0)
            tg_chats = list(tg.get("discovered_chats", []))
            # снежный ком: сканируем НОВЫЕ каналы/чаты, найденные в сообщениях (один хоп).
            seen_ch = {c.lower() for c in channels}
            new_ch = [c for c in tg.get("discovered_channels", []) if c.lower() not in seen_ch][:8]
            if new_ch:
                if report:
                    report(f"снежный ком: +{len(new_ch)} Telegram", 88)
                tg2 = scan_tg(new_ch, conn=conn)
                tg_added += tg2.get("added", 0)
                flagged += tg2.get("flagged", 0)
                for c in tg2.get("discovered_chats", []):
                    if c not in tg_chats:
                        tg_chats.append(c)
        except Exception:
            pass

    # 3) ГЛУБОКИЙ мультимодальный разбор top-постов (опц.) — youtube + tiktok + twitch/kick + live.
    deep_analyzed = 0
    deep_pool = fresh_yt + fresh_platform + fresh_streaming + fresh_live
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
    if do_live and live_added == 0:
        # Эфирный режим: никто из сид-аккаунтов сейчас не в эфире (норма) либо
        # проверка эфира недоступна — честно сообщаем, что это НЕ сбой.
        note = ("Сейчас никто из отслеживаемых аккаунтов не в прямом эфире. "
                "Это нормально — попробуйте позже либо переключитесь на VOD/посты.")
    elif do_video_platform and platform_added == 0:
        if platform == "instagram":
            if getattr(fetch_mod, "cookies_configured", lambda: False)():
                note = ("Вход в Instagram (cookies) включён, но листинг профилей не отдаётся: Instagram "
                        "АКТИВНО блокирует автоматический сбор по аккаунтам (403/«invalid request») даже "
                        "для залогиненной сессии. Для Instagram используйте «Живую проверку» отдельной "
                        "ссылки на reel/пост (одиночные ссылки менее агрессивно блокируются), а автопоиск "
                        "ведите по TikTok / YouTube / Telegram / Twitch / Kick.")
            else:
                note = ("Instagram не отдаёт публичный автопоиск без входа. Чтобы включить — добавьте "
                        "cookies авторизованной сессии: экспортируйте cookies.txt (расширение «Get "
                        "cookies.txt LOCALLY», будучи залогиненным в Instagram) и задайте "
                        "KOZ_COOKIES_FILE=<путь>; либо KOZ_COOKIES_FROM_BROWSER=firefox. Затем "
                        "перезапустите сервер. (Вход через Chrome на Windows недоступен — App-Bound "
                        "шифрование cookies.) Либо «Живая проверка» ссылки на reel / TikTok / YouTube.")
        elif platform_accounts_ok > 0:
            # Видео нашлись, но все уже в ленте — это НЕ сбой, а дедуп.
            note = (f"Новых постов нет: все {platform_found} найденных роликов уже в ленте — "
                    "выберите «TikTok» в фильтре очереди, чтобы их увидеть. Фон ловит новые сам.")
        else:
            note = ("TikTok сейчас ограничивает извлечение (rate-limit). Повторите через "
                    "пару минут или используйте «Живую проверку» ссылки.")

    if report:
        report("готово", 100)
    result = {
        "added": added + tg_added + platform_added,
        "youtube_added": added, "telegram_added": tg_added,
        "platform_added": platform_added, "platform": platform,
        "deep_analyzed": deep_analyzed,
        "flagged": flagged, "queries": n, "samples": samples[:10],
        "by_category": by_category,
        "telegram_chats": len(tg_chats), "telegram_chat_links": tg_chats[:10],
        # эхо настроек автопоиска (для UI/диагностики)
        "country": (country or "all").lower().strip(),
        "categories": catalog._norm_categories(categories),
        "content_type": content_type, "sort": sort,
        "live_added": live_added,
        # эхо выбранной полосы УРОВНЯ ОПАСНОСТИ (для UI/диагностики)
        "min_risk": min_risk, "max_risk": max_risk,
    }
    # Нота для активного фильтра опасности, когда в полосе ничего не нашли: это НЕ
    # сбой — найденный контент просто вне выбранного уровня (он сохранён в фоне).
    total_added = added + tg_added + platform_added
    danger_active = (min_risk > 0) or (max_risk < 100)
    if not note and danger_active and total_added == 0:
        note = (f"По выбранному уровню опасности (риск {min_risk}–{max_risk}) новых находок нет. "
                "Контент вне этого диапазона не показан — снимите фильтр опасности, чтобы увидеть всё.")
    if note:
        result["note"] = note
    return result
