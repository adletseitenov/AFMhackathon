"""Движок РЕКОМЕНДАЦИЙ КӨЗ — ЧИСТО rule-based превентивные действия для АФМ.

`build_recommendations(conn=None) -> list[dict]` читает агрегаты трендов
(переиспользует app.analytics.trends.aggregate) + несколько собственных
read-only SQL по posts/scores/extracted, применяет НАБОР ПРАВИЛ и возвращает
приоритезированный список превентивных рекомендаций на русском.

Критерий №2: НИКАКИХ внешних API/LLM — только детерминированные пороговые
правила поверх локальных агрегатов.

Каждая рекомендация: {title, rationale, action, priority, evidence}.
  - priority: 'high' | 'medium' | 'low'
  - rationale: «почему» со ссылкой на конкретные цифры
  - action: «что сделать» — из меню действий АФМ (блокировка платёжных каналов,
    takedown, запрос площадке, публичное предупреждение, единое дело, и т.п.)
  - evidence: краткие факты/числа (строка)

Устойчивость (R4): пустые агрегаты -> разумный дефолт; битый JSON в extracted
пропускается; функция НИКОГДА не падает. Standalone-safe (§0.2): conn=None ->
открыть своё соединение через db.connect() и закрыть в finally.
"""

import json
import sqlite3
from collections import Counter

from app import config, db
from app.analytics.trends import BRAND_ENTITY_TYPES, aggregate

# --- Пороги правил (детерминированы, без магии по месту) ----------------------
# Доминирующий бренд: >= N постов с одним брендом -> high.
DOMINANT_BRAND_MIN = 4
# Координированная сеть: один и тот же реквизит/промокод/кошелёк в >= N постах.
NETWORK_REUSE_MIN = 3
# Типы «реквизитов», повтор которых указывает на координированную сеть.
NETWORK_ENTITY_TYPES = {"promo_code", "crypto_wallet", "telegram", "whatsapp", "phone"}
# Доля категории/уровня риска, начиная с которой правило срабатывает.
CATEGORY_SHARE_HIGH = 0.30  # доля одной категории среди флагнутых
HIGH_RISK_SHARE = 0.50      # доля high-risk (70-100) среди скоренных
PLATFORM_ESCALATE_SHARE = 0.50  # доля escalate в постах площадки
# Платформы стриминга (промо казино в эфире).
STREAMING_PLATFORMS = {"twitch", "kick", "live"}
# RU-названия категорий для текстов.
_CAT_RU = {
    "gambling": "нелегальный гемблинг",
    "pyramid": "финансовые пирамиды",
    "fraud": "мошенничество",
    "clean": "чистый контент",
}
# Порядок приоритетов для сортировки.
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def build_recommendations(conn: "sqlite3.Connection | None" = None) -> list[dict]:
    """Построить приоритезированный список превентивных рекомендаций для АФМ.

    conn=None -> открыть своё соединение (config.DB_PATH) и закрыть в finally.
    Никогда не бросает: на пустых/битых данных возвращает разумный дефолт.
    """
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    try:
        return _build(conn)
    except Exception:
        # R4: движок не должен ронять роут — отдаём безопасный дефолт.
        return [_default_monitoring_rec(total_posts=0, flagged=0)]
    finally:
        if own_conn:
            conn.close()


# --- Внутреннее ----------------------------------------------------------------
def _build(conn: sqlite3.Connection) -> list[dict]:
    agg = aggregate(conn)
    total = agg["total_posts"]
    by_category = agg["by_category"]
    by_platform = agg["by_platform"]
    by_action = agg["by_recommended_action"]
    top_brands = agg["top_brands"]
    risk_hist = agg["risk_histogram"]

    # Флагнутыми считаем всё, что НЕ clean (gambling/pyramid/fraud).
    flagged = sum(v for k, v in by_category.items() if k != "clean")
    high_risk = risk_hist.get("70-100", 0)
    scored_total = sum(risk_hist.values())

    recs: list[dict] = []

    # --- Правило 1: доминирующий бренд (high) ---------------------------------
    if top_brands:
        top = top_brands[0]
        if top["count"] >= DOMINANT_BRAND_MIN:
            recs.append({
                "title": f"Доминирующий бренд: {top['brand']}",
                "rationale": (
                    f"Бренд «{top['brand']}» фигурирует в {top['count']} постах — "
                    "это устойчивый рекламный канал нелегальной площадки, а не "
                    "разовое упоминание."
                ),
                "action": (
                    f"Запросить блокировку платёжных каналов и takedown аккаунтов "
                    f"бренда «{top['brand']}»; направить материалы провайдерам/банкам."
                ),
                "priority": "high",
                "evidence": f"{top['brand']}: {top['count']} постов",
            })

    # --- Правило 2: площадка-лидер по эскалациям ------------------------------
    plat_esc = _platform_escalation_shares(conn)
    if plat_esc:
        plat, share, esc, n = plat_esc[0]
        if esc >= 2 and share >= PLATFORM_ESCALATE_SHARE:
            recs.append({
                "title": f"Площадка-лидер по эскалациям: {plat}",
                "rationale": (
                    f"На площадке {plat} {esc} из {n} постов уходят в эскалацию "
                    f"({round(share * 100)}% постов площадки) — концентрация угрозы."
                ),
                "action": (
                    f"Усилить мониторинг площадки {plat}; направить площадке "
                    "официальный запрос на удаление противоправного контента."
                ),
                "priority": "high" if share >= 0.7 else "medium",
                "evidence": f"{plat}: {esc}/{n} escalate ({round(share * 100)}%)",
            })

    # --- Правило 3: всплеск пирамид -------------------------------------------
    if flagged > 0:
        pyramid = by_category.get("pyramid", 0)
        pyr_share = pyramid / flagged
        if pyramid >= 3 and pyr_share >= CATEGORY_SHARE_HIGH:
            recs.append({
                "title": "Всплеск финансовых пирамид",
                "rationale": (
                    f"Пирамиды дают {pyramid} из {flagged} флагнутых постов "
                    f"({round(pyr_share * 100)}%) — признак активной вербовочной "
                    "кампании."
                ),
                "action": (
                    "Выпустить публичное предупреждение о финпирамидах; "
                    "уведомить Национальный Банк РК и финрегулятор."
                ),
                "priority": "high" if pyr_share >= 0.5 else "medium",
                "evidence": f"pyramid: {pyramid}/{flagged} ({round(pyr_share * 100)}%)",
            })

    # --- Правило 4: высокая доля high-risk в очереди ---------------------------
    if scored_total > 0:
        hr_share = high_risk / scored_total
        if high_risk >= 5 and hr_share >= HIGH_RISK_SHARE:
            recs.append({
                "title": "Высокая доля контента высокого риска в очереди",
                "rationale": (
                    f"{high_risk} из {scored_total} постов имеют риск 70+ "
                    f"({round(hr_share * 100)}%) — очередь ручной проверки "
                    "перегружена."
                ),
                "action": (
                    "Приоритизировать ручную проверку high-risk; при нехватке "
                    "аналитиков — временно поднять порог автоэскалации."
                ),
                "priority": "medium",
                "evidence": f"high-risk(70+): {high_risk}/{scored_total} "
                            f"({round(hr_share * 100)}%)",
            })

    # --- Правило 5: стрим-гемблинг (twitch/kick) -------------------------------
    stream_n = sum(by_platform.get(p, 0) for p in STREAMING_PLATFORMS)
    if stream_n >= 2:
        plats = ", ".join(
            p for p in STREAMING_PLATFORMS if by_platform.get(p, 0) > 0
        )
        recs.append({
            "title": "Промо казино в стриминге",
            "rationale": (
                f"Обнаружено {stream_n} постов на стриминговых площадках "
                f"({plats}) — продвижение казино в прямом эфире, часто без "
                "возрастных ограничений."
            ),
            "action": (
                "Координация с площадками стриминга; запрос на возрастные "
                "ограничения и удаление промо казино из эфиров."
            ),
            "priority": "medium",
            "evidence": f"стриминг: {stream_n} постов ({plats})",
        })

    # --- Правило 6: повторяющиеся реквизиты -> координированная сеть -----------
    reuse = _repeated_requisites(conn)
    if reuse:
        etype, value, count = reuse[0]
        recs.append({
            "title": "Признак координированной сети",
            "rationale": (
                f"Один и тот же реквизит ({_entity_type_ru(etype)} «{value}») "
                f"встречается в {count} постах — посты публикует одна сеть, а не "
                "независимые авторы."
            ),
            "action": (
                "Оформить единое дело по сети; запросить данные по реквизиту у "
                "платёжного провайдера/мессенджера."
            ),
            "priority": "high" if count >= 5 else "medium",
            "evidence": f"{_entity_type_ru(etype)} «{value}»: {count} постов",
        })

    # --- Правило 7: доминирующая категория угроз (low/medium обзорное) ---------
    if flagged > 0:
        top_cat = max(
            ("gambling", "pyramid", "fraud"),
            key=lambda c: by_category.get(c, 0),
        )
        top_cat_n = by_category.get(top_cat, 0)
        if top_cat_n > 0:
            share = top_cat_n / flagged
            recs.append({
                "title": f"Преобладающая угроза: {_CAT_RU[top_cat]}",
                "rationale": (
                    f"Категория «{_CAT_RU[top_cat]}» — {top_cat_n} из {flagged} "
                    f"флагнутых постов ({round(share * 100)}%); фокусировать "
                    "профильные ресурсы здесь."
                ),
                "action": (
                    f"Сформировать профильную группу по направлению "
                    f"«{_CAT_RU[top_cat]}»; обновить ключевые слова мониторинга."
                ),
                "priority": "low",
                "evidence": f"{top_cat}: {top_cat_n}/{flagged} ({round(share * 100)}%)",
            })

    # --- Дефолт: данных мало / ничего не сработало -----------------------------
    if not recs:
        recs.append(_default_monitoring_rec(total, flagged))
        if total == 0:
            recs.append({
                "title": "Запустить автопоиск по площадкам",
                "rationale": (
                    "В базе нет ни одного поста — нет основания для превентивных "
                    "действий, пока не собраны данные."
                ),
                "action": (
                    "Запустить автопоиск (YouTube/Telegram/TikTok/Instagram) и "
                    "повторить анализ трендов."
                ),
                "priority": "low",
                "evidence": "total_posts=0",
            })

    # Сортировка по приоритету (high -> medium -> low), стабильно сохраняя
    # исходный порядок внутри одного приоритета.
    recs.sort(key=lambda r: _PRIORITY_RANK.get(r["priority"], 3))
    return recs


def _default_monitoring_rec(total_posts: int, flagged: int) -> dict:
    """Базовая рекомендация на случай малого объёма данных/тишины правил."""
    return {
        "title": "Расширить мониторинг и сбор данных",
        "rationale": (
            f"Накоплено {total_posts} постов (флагнуто {flagged}); этого мало "
            "для статистически значимых выводов — нужно больше наблюдений."
        ),
        "action": (
            "Расширить список отслеживаемых аккаунтов и ключевых слов; "
            "увеличить частоту автопоиска по площадкам."
        ),
        "priority": "low",
        "evidence": f"total_posts={total_posts}, flagged={flagged}",
    }


def _entity_type_ru(etype: str) -> str:
    return {
        "promo_code": "промокод",
        "crypto_wallet": "крипто-кошелёк",
        "telegram": "Telegram-канал",
        "whatsapp": "WhatsApp",
        "phone": "телефон",
    }.get(etype, etype)


def _platform_escalation_shares(conn: sqlite3.Connection):
    """[(platform, escalate_share, escalate_count, total)] упорядочено по доле убыв.

    Только площадки, у которых есть хотя бы один скоренный пост. Read-only.
    """
    rows = conn.execute(
        """
        SELECT p.platform AS platform,
               COUNT(*) AS total,
               SUM(CASE WHEN s.recommended_action = 'escalate' THEN 1 ELSE 0 END)
                   AS escalated
        FROM posts p
        JOIN scores s ON s.post_id = p.id
        GROUP BY p.platform
        """
    ).fetchall()
    out = []
    for r in rows:
        total = r["total"] or 0
        if total <= 0:
            continue
        esc = r["escalated"] or 0
        out.append((r["platform"], esc / total, esc, total))
    out.sort(key=lambda t: (-t[1], -t[2], str(t[0])))
    return out


def _repeated_requisites(conn: sqlite3.Connection):
    """[(entity_type, value, post_count)] для реквизитов, повторяющихся в >= N постах.

    Считает уникальные посты на (type, normalized-or-value), чтобы повтор одного
    реквизита внутри одного поста не накручивал счётчик. Битый JSON пропускается.
    Упорядочено по числу постов убыв. Read-only.
    """
    counter: Counter = Counter()
    type_of: dict = {}
    for r in conn.execute("SELECT entities_json FROM extracted"):
        try:
            entities = json.loads(r["entities_json"]) or []
        except (TypeError, ValueError):
            entities = []
        if not isinstance(entities, list):
            continue
        seen_in_post = set()
        for e in entities:
            if not isinstance(e, dict):
                continue
            etype = e.get("type")
            if etype not in NETWORK_ENTITY_TYPES:
                continue
            # бренды считаются отдельным правилом (доминирующий бренд) — не дублируем.
            if etype in BRAND_ENTITY_TYPES:
                continue
            key = e.get("normalized") or e.get("value")
            if not key:
                continue
            ident = (etype, key)
            if ident in seen_in_post:
                continue  # один реквизит в одном посте = 1
            seen_in_post.add(ident)
            counter[ident] += 1
            type_of[ident] = etype
    out = [
        (etype, value, count)
        for (etype, value), count in counter.items()
        if count >= NETWORK_REUSE_MIN
    ]
    out.sort(key=lambda t: (-t[2], str(t[1])))
    return out
