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
from app.decision.licensed import (
    COMPLIANCE_HINT,
    is_licensed,
    licensed_operators,
)

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
# Базовый вес приоритета для числового score важности (high=3/medium=2/low=1).
_PRIORITY_WEIGHT = {"high": 3.0, "medium": 2.0, "low": 1.0}
# Корзины риска поста (§0.11: пороги 40/70).
_RISK_REVIEW = 40
_RISK_ESCALATE = 70


def _importance_score(priority: str, evidence_scale: float = 0.0) -> float:
    """Числовой score важности рекомендации.

    score = вес_приоритета (high=3/medium=2/low=1) + НОРМИРОВАННАЯ добавка масштаба
    улики (доля 0..1 ИЛИ счёт постов, ужатый в 0..0.99 через count/(count+ k)).
    Добавка < 1, поэтому приоритет ВСЕГДА доминирует над масштабом: любой high
    идёт раньше любого medium/low (требование «high раньше low»), а внутри одного
    приоритета крупнее улика -> выше score.
    """
    base = _PRIORITY_WEIGHT.get(priority, 1.0)
    try:
        scale = float(evidence_scale)
    except (TypeError, ValueError):
        scale = 0.0
    if scale < 0:
        scale = 0.0
    if scale > 1:  # это счёт постов -> ужать в (0, 1) монотонно.
        scale = scale / (scale + 4.0)
    # держим добавку строго < 1, чтобы не перепрыгнуть соседний приоритет.
    return round(base + min(scale, 0.99), 4)


def _rank_and_dedup(recs: list[dict]) -> list[dict]:
    """Отсортировать по score важности убыв. и убрать близкие дубли.

    Дедуп: по нормализованному title И по нормализованному action (первая —
    самая важная — рекомендация побеждает, остальные близкие отбрасываются).
    Тай-брейкер при равном score — ранг приоритета, затем title (детерминизм).
    """
    for r in recs:
        if "score" not in r:
            r["score"] = _importance_score(r.get("priority", "low"))
    recs.sort(key=lambda r: (
        -float(r.get("score", 0.0)),
        _PRIORITY_RANK.get(r.get("priority"), 3),
        str(r.get("title", "")),
    ))
    out: list[dict] = []
    seen_titles: set = set()
    seen_actions: set = set()
    for r in recs:
        t = str(r.get("title", "")).strip().lower()
        a = str(r.get("action", "")).strip().lower()
        if t in seen_titles or a in seen_actions:
            continue
        seen_titles.add(t)
        seen_actions.add(a)
        out.append(r)
    return out


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


# --- ТОЧЕЧНЫЕ рекомендации под конкретный кейс --------------------------------
def recommendations_for_post(
    conn: "sqlite3.Connection | None",
    post: "str | dict | object",
    score: "int | dict | object | None" = None,
) -> list[dict]:
    """Точечные рекомендации ИМЕННО для этого кейса (2-5 шт., отсортированы).

    `post` — post_id (str), dict (как из _post_dict) или объект с атрибутами.
    `score` — числовой риск (0..100), Score-подобный объект/словарь, либо None
    (тогда берём риск из БД по post_id, если получится).

    Правила привязаны к категории/риску/бренду/площадке/лицензии/реквизитам
    конкретного поста (без агрегатов): нелицензированный гемблинг -> takedown +
    блок платёжных каналов; лицензированный оператор -> проверка рекламы (не блок);
    пирамида -> публичное предупреждение; высокий риск -> эскалация аналитику;
    найденные промокод/кошелёк/контакты -> приобщить к делу + запрос провайдеру.

    ЧИСТО rule-based (критерий №2). Никогда не падает: при любой ошибке/нехватке
    данных возвращает разумный мониторинговый дефолт (>=1 рекомендация).
    """
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    try:
        ctx = _post_context(conn, post, score)
        recs = _case_rules(ctx)
        if not recs:
            recs = [_case_default(ctx)]
        # держим 2-5 рекомендаций: добиваем «наполнителями» до 2 (уникальными по
        # title), обрезаем хвост маловажных при переборе.
        existing_titles = {r["title"].strip().lower() for r in recs}
        for extra in (_case_default(ctx), *_case_fillers(ctx)):
            if len(recs) >= 2:
                break
            t = extra["title"].strip().lower()
            if t not in existing_titles:
                recs.append(extra)
                existing_titles.add(t)
        ranked = _rank_and_dedup(recs)
        return ranked[:5] if ranked else [_case_default(ctx)]
    except Exception:
        # R4: точечный движок не должен ронять /api/post — безопасный дефолт.
        return [{
            "title": "Ручная проверка кейса",
            "rationale": "Не удалось собрать признаки кейса — нужна проверка аналитиком.",
            "action": "Передать пост аналитику АФМ для ручной оценки.",
            "priority": "low",
            "evidence": "fallback",
            "score": _importance_score("low"),
        }]
    finally:
        if own_conn:
            conn.close()


def _post_context(conn, post, score) -> dict:
    """Свести post/score (+ БД при наличии post_id) в нормализованный контекст.

    Возвращает dict: post_id, platform, category, risk, licensed(bool),
    licensed_ops(list[str]), brand(str|None), requisites(list[(type,value)]),
    text(str). Никогда не бросает: чего нет — заполняется дефолтом.
    """
    ctx = {
        "post_id": None, "platform": "", "category": "", "risk": None,
        "licensed": False, "licensed_ops": [], "brand": None,
        "requisites": [], "text": "",
    }

    # --- 1) разобрать `post` -> базовые поля + post_id для добора из БД ---------
    row = None
    if isinstance(post, str):
        ctx["post_id"] = post
    elif isinstance(post, dict):
        ctx["post_id"] = post.get("id")
        ctx["platform"] = post.get("platform") or ""
        ctx["category"] = (post.get("category") or "")
        if "risk" in post and post.get("risk") is not None:
            ctx["risk"] = _coerce_risk(post.get("risk"))
        ctx["text"] = " ".join(
            str(post.get(k) or "")
            for k in ("caption", "combined_text", "author_handle")
        )
        if "licensed_operators" in post:
            ctx["licensed_ops"] = list(post.get("licensed_operators") or [])
    else:  # объект с атрибутами (Post-подобный)
        ctx["post_id"] = getattr(post, "id", None)
        ctx["platform"] = getattr(post, "platform", "") or ""
        ctx["text"] = " ".join(
            str(getattr(post, k, "") or "")
            for k in ("caption", "combined_text", "author_handle")
        )

    # --- 2) score -> risk/category ---------------------------------------------
    if score is not None:
        if isinstance(score, dict):
            if score.get("risk") is not None:
                ctx["risk"] = _coerce_risk(score.get("risk"))
            if score.get("category"):
                ctx["category"] = score.get("category")
        elif hasattr(score, "risk"):
            ctx["risk"] = _coerce_risk(getattr(score, "risk"))
            if getattr(score, "category", None):
                ctx["category"] = getattr(score, "category")
        else:
            ctx["risk"] = _coerce_risk(score)

    # --- 3) добор из БД по post_id (категория/риск/платформа/текст/реквизиты) ---
    pid = ctx["post_id"]
    if pid:
        try:
            p = conn.execute(
                "SELECT platform, caption, author_handle FROM posts WHERE id=?",
                (pid,),
            ).fetchone()
            if p is not None:
                if not ctx["platform"]:
                    ctx["platform"] = p["platform"] or ""
                ctx["text"] = (ctx["text"] + " " + (p["caption"] or "")
                               + " " + (p["author_handle"] or "")).strip()
        except Exception:
            pass
        try:
            s = conn.execute(
                "SELECT risk, category FROM scores WHERE post_id=?", (pid,)
            ).fetchone()
            if s is not None:
                if ctx["risk"] is None:
                    ctx["risk"] = _coerce_risk(s["risk"])
                if not ctx["category"]:
                    ctx["category"] = s["category"] or ""
        except Exception:
            pass
        # реквизиты/бренд из extracted.entities_json (битый JSON пропускаем).
        try:
            e = conn.execute(
                "SELECT combined_text, entities_json FROM extracted WHERE post_id=?",
                (pid,),
            ).fetchone()
            if e is not None:
                ctx["text"] = (ctx["text"] + " " + (e["combined_text"] or "")).strip()
                ctx["brand"], ctx["requisites"] = _entities_of(e["entities_json"])
        except Exception:
            pass

    # --- 4) флаг лицензии: по уже найденным операторам ИЛИ по тексту кейса ------
    if not ctx["licensed_ops"]:
        try:
            ctx["licensed_ops"] = licensed_operators(ctx["text"])
        except Exception:
            ctx["licensed_ops"] = []
    ctx["licensed"] = bool(ctx["licensed_ops"])
    if ctx["risk"] is None:
        ctx["risk"] = 0
    return ctx


def _coerce_risk(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def _entities_of(entities_json):
    """(brand|None, [(type, value), ...]) из entities_json. Битый JSON -> (None, [])."""
    try:
        entities = json.loads(entities_json) or []
    except (TypeError, ValueError):
        return None, []
    if not isinstance(entities, list):
        return None, []
    brand = None
    reqs: list = []
    seen: set = set()
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        etype = ent.get("type")
        value = ent.get("normalized") or ent.get("value")
        if not value:
            continue
        if etype in BRAND_ENTITY_TYPES and brand is None:
            brand = ent.get("value") or value
        elif etype in NETWORK_ENTITY_TYPES:
            key = (etype, value)
            if key not in seen:
                seen.add(key)
                reqs.append((etype, ent.get("value") or value))
    return brand, reqs


def _case_rules(ctx: dict) -> list[dict]:
    """Набор точечных правил для одного кейса -> list[dict] (до ранжирования)."""
    recs: list[dict] = []
    risk = ctx["risk"] or 0
    category = (ctx["category"] or "").lower()
    platform = ctx["platform"] or "площадке"
    licensed = ctx["licensed"]
    ops_txt = ", ".join(ctx["licensed_ops"]) if ctx["licensed_ops"] else ""
    brand = ctx["brand"]
    reqs = ctx["requisites"]

    # --- Гемблинг -------------------------------------------------------------
    if category == "gambling":
        if licensed:
            recs.append({
                "title": f"Лицензированный оператор: {ops_txt}" if ops_txt
                         else "Лицензированный оператор",
                "rationale": (
                    f"В кейсе упомянут лицензированный в РК букмекер "
                    f"({ops_txt or 'оператор'}) — деятельность легальна, блокировка "
                    "не требуется; контроль за рекламными нормами."
                ),
                "action": (
                    f"Не блокировать оператора. {COMPLIANCE_HINT} При нарушениях "
                    "рекламы — предписание оператору/площадке."
                ),
                "priority": "medium",
                "evidence": (
                    f"лицензирован в РК ({ops_txt}); сверять с реестром АФМ/ЕУЦ"
                    if ops_txt else "лицензированный оператор (сверять с реестром АФМ)"
                ),
                "score": _importance_score("medium", 0.5),
            })
        else:
            bn = f" бренда «{brand}»" if brand else ""
            recs.append({
                "title": f"Takedown и блок платёжных каналов{bn}".strip(),
                "rationale": (
                    f"Нелицензированный гемблинг{bn} — устойчивая реклама нелегальной "
                    "площадки; основная мера — снять контент и перекрыть приём платежей."
                ),
                "action": (
                    f"Запросить takedown аккаунта/поста{bn} у площадки {platform}; "
                    "инициировать блокировку платёжных каналов через банки/провайдеров."
                ),
                "priority": "high",
                "evidence": (f"gambling, риск {risk}/100"
                             + (f", бренд {brand}" if brand else "")),
                "score": _importance_score("high", risk / 100.0),
            })

    # --- Пирамида -------------------------------------------------------------
    elif category == "pyramid":
        recs.append({
            "title": "Публичное предупреждение о финпирамиде",
            "rationale": (
                f"Кейс классифицирован как финансовая пирамида (риск {risk}/100) — "
                "угроза массового ущерба вкладчикам, важна скорость оповещения."
            ),
            "action": (
                "Выпустить публичное предупреждение о признаках финпирамиды; "
                "уведомить финрегулятор/Нацбанк РК; запросить takedown у площадки."
            ),
            "priority": "high" if risk >= _RISK_ESCALATE else "medium",
            "evidence": f"pyramid, риск {risk}/100",
            "score": _importance_score(
                "high" if risk >= _RISK_ESCALATE else "medium", risk / 100.0),
        })

    # --- Мошенничество --------------------------------------------------------
    elif category == "fraud":
        recs.append({
            "title": "Пресечь мошенническую схему",
            "rationale": (
                f"Кейс классифицирован как мошенничество (риск {risk}/100) — "
                "признаки обмана пользователей, требуется быстрое реагирование."
            ),
            "action": (
                f"Запросить удаление контента у площадки {platform}; зафиксировать "
                "доказательства и при подтверждении передать материалы в производство."
            ),
            "priority": "high" if risk >= _RISK_ESCALATE else "medium",
            "evidence": f"fraud, риск {risk}/100",
            "score": _importance_score(
                "high" if risk >= _RISK_ESCALATE else "medium", risk / 100.0),
        })

    # --- Реквизиты (промокод/кошелёк/контакты) -> приобщить к делу --------------
    if reqs:
        listed = "; ".join(
            f"{_entity_type_ru(t)} «{v}»" for t, v in reqs[:4]
        )
        recs.append({
            "title": "Приобщить реквизиты к делу",
            "rationale": (
                f"В кейсе найдены реквизиты ({listed}) — это привязки к организаторам "
                "и платёжной инфраструктуре, ключевые для расследования."
            ),
            "action": (
                "Приобщить найденные реквизиты к делу; запросить данные у платёжного "
                "провайдера/мессенджера; проверить повторы реквизита по другим постам."
            ),
            "priority": "high" if any(
                t in ("crypto_wallet", "promo_code") for t, _ in reqs
            ) else "medium",
            "evidence": listed,
            "score": _importance_score(
                "high" if any(t in ("crypto_wallet", "promo_code")
                              for t, _ in reqs) else "medium",
                min(len(reqs), 5) / 5.0),
        })

    # --- Высокий риск -> эскалация аналитику (для любой угрозы) -----------------
    if risk >= _RISK_ESCALATE and category in ("gambling", "pyramid", "fraud"):
        recs.append({
            "title": "Эскалация аналитику (высокий риск)",
            "rationale": (
                f"Риск-скор {risk}/100 (порог эскалации {_RISK_ESCALATE}) — кейс "
                "в красной зоне, нужен приоритетный разбор человеком."
            ),
            "action": (
                "Эскалировать кейс ведущему аналитику АФМ для приоритетной проверки "
                "и решения о мерах реагирования."
            ),
            "priority": "high",
            "evidence": f"риск {risk}/100 >= {_RISK_ESCALATE}",
            "score": _importance_score("high", risk / 100.0),
        })
    elif _RISK_REVIEW <= risk < _RISK_ESCALATE and category in (
        "gambling", "pyramid", "fraud"
    ):
        recs.append({
            "title": "Ручная проверка (средний риск)",
            "rationale": (
                f"Риск-скор {risk}/100 — пограничная зона ({_RISK_REVIEW}-"
                f"{_RISK_ESCALATE}); решение требует подтверждения аналитиком."
            ),
            "action": "Поставить кейс в очередь ручной проверки аналитика АФМ.",
            "priority": "medium",
            "evidence": f"риск {risk}/100 в зоне review",
            "score": _importance_score("medium", risk / 100.0),
        })

    return recs


def _case_default(ctx: dict) -> dict:
    """Дефолт для кейса: чистый/низкорисковый пост или нехватка признаков."""
    risk = ctx.get("risk") or 0
    return {
        "title": "Наблюдение без жёстких мер",
        "rationale": (
            f"Низкий риск ({risk}/100) и/или нет признаков нарушения — оснований "
            "для блокировки/takedown нет, достаточно наблюдения."
        ),
        "action": (
            "Оставить под наблюдением; пересмотреть при появлении новых сигналов "
            "(бренд, реквизиты, рост охвата)."
        ),
        "priority": "low",
        "evidence": f"риск {risk}/100",
        "score": _importance_score("low", risk / 100.0),
    }


def _case_fillers(ctx: dict) -> list[dict]:
    """Доп. low-рекомендации, чтобы кейс всегда давал >=2 (напр. чистый пост)."""
    risk = ctx.get("risk") or 0
    platform = ctx.get("platform") or "площадке"
    return [{
        "title": "Периодическая переоценка кейса",
        "rationale": (
            "Контент и охват могут измениться — стоит перепроверить кейс при "
            "обновлении модели или росте просмотров."
        ),
        "action": (
            f"Запланировать повторную проверку поста на площадке {platform} при "
            "новых сигналах или обновлении скоринга."
        ),
        "priority": "low",
        "evidence": f"риск {risk}/100, плановый пересмотр",
        "score": _importance_score("low", 0.1),
    }]


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

    # --- Правило 1: доминирующий бренд ----------------------------------------
    # ЛИЦЕНЗИРОВАННЫЙ оператор (Olimpbet/PARI/Tennisi/...) работает в РК ЛЕГАЛЬНО:
    # блокировка не требуется — проверяем РЕКЛАМНЫЕ нормы (medium). Нелицензированный
    # бренд (mostbet/1win/1xbet/...) — прежняя жёсткая рекомендация (high): блокировка
    # платёжных каналов / takedown. Риск-скор контента при этом не занижается.
    if top_brands:
        top = top_brands[0]
        if top["count"] >= DOMINANT_BRAND_MIN:
            if is_licensed(top["brand"]):
                recs.append({
                    "title": f"Лицензированный оператор: {top['brand']}",
                    "rationale": (
                        f"Бренд «{top['brand']}» фигурирует в {top['count']} постах "
                        "и относится к ЛИЦЕНЗИРОВАННЫМ в РК букмекерам — деятельность "
                        "оператора легальна, блокировка не требуется. Контроль — за "
                        "соблюдением рекламных норм (контент остаётся гемблингом)."
                    ),
                    "action": (
                        f"Не блокировать оператора «{top['brand']}». {COMPLIANCE_HINT} "
                        "При нарушениях рекламы — предписание оператору/площадке."
                    ),
                    "priority": "medium",
                    "evidence": (
                        f"{top['brand']}: {top['count']} постов; лицензирован в РК "
                        "(сверять с реестром АФМ/ЕУЦ)"
                    ),
                    "score": _importance_score("medium", top["count"]),
                })
            else:
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
                    "score": _importance_score("high", top["count"]),
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
                "score": _importance_score(
                    "high" if share >= 0.7 else "medium", share),
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
                "score": _importance_score(
                    "high" if pyr_share >= 0.5 else "medium", pyr_share),
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
                "score": _importance_score("medium", hr_share),
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
            "score": _importance_score("medium", stream_n),
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
            "score": _importance_score("high" if count >= 5 else "medium", count),
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
                "score": _importance_score("low", share),
            })

    # --- Правило 8: разделение легальных и нелегальных операторов (обзор) ------
    # Если среди топ-брендов есть И лицензированные, И нелицензированные —
    # подсветить аналитику: блокировки фокусировать на нелегальных, по легальным —
    # контроль рекламы. Срабатывает только когда есть что разделять (есть оба класса).
    if top_brands:
        # licensed_operators(brand) непусто <=> бренд лицензирован (спец. задачи).
        licensed_brands = [
            b["brand"] for b in top_brands if licensed_operators(b["brand"])
        ]
        unlicensed_brands = [
            b["brand"] for b in top_brands if not licensed_operators(b["brand"])
        ]
        if licensed_brands and unlicensed_brands:
            n_lic = len(licensed_brands)
            n_unlic = len(unlicensed_brands)
            n_total = n_lic + n_unlic
            unlic_share = n_unlic / n_total
            recs.append({
                "title": "Разделение легальных и нелегальных операторов",
                "rationale": (
                    f"Среди топ-брендов {n_lic} из {n_total} лицензированы в РК "
                    f"({', '.join(licensed_brands)}), {n_unlic} — нелицензированы "
                    f"({', '.join(unlicensed_brands)}). Это разные режимы реагирования: "
                    "по нелегальным — блокировка, по легальным — контроль рекламы."
                ),
                "action": (
                    "Сфокусировать блокировки платёжных каналов и takedown на "
                    "нелицензированных операторах; по лицензированным — контроль "
                    "соблюдения рекламных норм (возраст 21+, пометка «реклама», "
                    "запрет «гарантированного дохода» и агрессивных бонусов)."
                ),
                "priority": "high" if unlic_share >= 0.5 else "medium",
                "evidence": (
                    f"лицензированы: {n_lic} ({', '.join(licensed_brands)}); "
                    f"нелицензированы: {n_unlic} ({', '.join(unlicensed_brands)})"
                ),
                "score": _importance_score(
                    "high" if unlic_share >= 0.5 else "medium", unlic_share),
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
                "score": _importance_score("low"),
            })

    # РАНЖИРОВАНИЕ по числовому score важности (high раньше low) + дедуп близких.
    return _rank_and_dedup(recs)


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
        "score": _importance_score("low"),
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
