"""F4 — HTTP-роуты аналитической консоли КӨЗ (авто-роутер, A2/§0.2/§0.6).

Модуль экспортирует module-level `router`; app/main.py автоподключает его через
pkgutil-walk — main.py НЕ правится. Соединение берётся из request.app.state.db
(одно соединение из lifespan), стандалон-функции тут не используются.

Роуты:
  GET  /api/feed?min_risk=&category=&limit=  — приоритетная очередь revealed-постов
       (INNER JOIN scores), отсортированная по risk DESC. Форма строки:
       {post, score, recommended_action}.
  GET  /api/post/{post_id}                   — drill-down карточка:
       {post, extracted, score, explanation, recommended_action}. 404 если нет поста.
  POST /api/tick                             — раскрыть следующий батч seed-постов
       (db.reveal_next, TICK_REVEAL_N). -> {revealed, total_revealed}.
  POST /api/analyze                          — live-проверка по ссылке (JSON {url})
       ИЛИ загруженному файлу (multipart). fetch -> extract -> score_post,
       персист + revealed=1. -> {post, extracted, score, explanation,
       recommended_action}. Мягкая деградация: при отказе тяжёлого пути всё равно
       скорит доступный текст и возвращает note, а не 500.

ВАЖНО (§0.5): fetch_post / extract / score_post вызываются через атрибут своего
модуля (fetch_mod.fetch_post, pipeline_mod.extract, scoring_mod.score_post), чтобы
тесты могли monkeypatch'ить один seam.
"""

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

import app.decision.scoring as scoring_mod
import app.extractors.pipeline as pipeline_mod
import app.ingestion.fetch as fetch_mod
import app.jobs.worker as jobs
from app import config, db
from app.decision.explain import explain
from app.decision.licensed import (
    COMPLIANCE_HINT,
    REGISTRY_DISCLAIMER,
    licensed_operators,
)
from app.models import Extracted, FeatureHit, Post, Score

router = APIRouter()


# --------------------------------------------------------------------------- #
# Сериализация строк БД в форму контракта (§0.7).
# --------------------------------------------------------------------------- #

def _post_dict(r, licensed_text: "str | None" = None) -> dict:
    """Сериализует строку поста в форму контракта.

    Флаг «разрешён в РК»: licensed_operators — канонические имена ЛИЦЕНЗИРОВАННЫХ
    в РК букмекеров, упомянутых в тексте поста; licensed = bool(этого списка).
    По умолчанию матчим по caption (его достаточно для ленты); для drill-down
    передаём полный текст (combined_text) через licensed_text. Риск НЕ занижается —
    флаг лишь добавляет контекст для решения человека.
    """
    # Матчим и по подписи, И по author_handle: у tiktok/стрим-постов оператор —
    # это сам аккаунт (@olimpbet), а подпись может быть посторонней («#radmir»).
    if licensed_text is not None:
        text = licensed_text
    else:
        text = (r["caption"] or "") + " " + (r["author_handle"] or "")
    ops = licensed_operators(text)
    return {
        "id": r["id"], "platform": r["platform"], "author_handle": r["author_handle"],
        "url": r["url"], "caption": r["caption"], "posted_at": r["posted_at"],
        "media_path": r["media_path"], "thumb_url": r["thumb_url"], "source": r["source"],
        "view_count": r["view_count"],
        "licensed_operators": ops, "licensed": bool(ops),
    }


def _score_dict_from_row(r) -> dict:
    return {
        "post_id": r["post_id"], "risk": r["risk"], "category": r["category"],
        "class_probs": json.loads(r["class_probs_json"] or "{}"),
        "top_features": json.loads(r["top_features_json"] or "[]"),
    }


# --------------------------------------------------------------------------- #
# GET /api/feed — приоритетная очередь revealed-постов с выбираемой сортировкой.
# --------------------------------------------------------------------------- #

# БЕЛЫЙ СПИСОК сортировок -> ФИКСИРОВАННАЯ строка ORDER BY (значение НИКОГДА не
# интерполируется из ввода пользователя — мы лишь выбираем заранее заданную строку).
# Для novelty/popularity добавлен стабильный тай-брейкер по риску.
_SORT_ORDER_BY = {
    "relevance": "s.risk DESC",
    "novelty": "p.posted_at DESC, s.risk DESC",
    "popularity": "p.view_count DESC, s.risk DESC",
}
_DEFAULT_SORT = "relevance"


@router.get("/api/feed")
def api_feed(
    request: Request,
    min_risk: int = Query(0),
    category: "str | None" = Query(None),
    platform: "str | None" = Query(None),
    limit: int = Query(100),
    real_only: int = Query(0),
    sort: str = Query(_DEFAULT_SORT),
):
    conn = request.app.state.db
    # Неизвестное/пустое значение -> relevance (см. _SORT_ORDER_BY).
    order_by = _SORT_ORDER_BY.get((sort or "").lower().strip(),
                                  _SORT_ORDER_BY[_DEFAULT_SORT])
    sql = (
        "SELECT p.id, p.platform, p.author_handle, p.url, p.caption, p.posted_at, "
        "p.media_path, p.thumb_url, p.source, p.view_count, "
        "s.post_id, s.risk, s.category, s.class_probs_json, s.top_features_json, "
        "s.recommended_action "
        "FROM posts p JOIN scores s ON s.post_id = p.id "
        "WHERE p.revealed = 1 AND s.risk >= ?"
    )
    params: list = [min_risk]
    if real_only:
        # только НАСТОЯЩИЕ посты: live-скан Telegram (source=live) и автономно
        # найденные в интернете (source=discovered). seed-демо и синтетику скрываем.
        sql += " AND p.source IN ('live', 'discovered')"
    if category:
        sql += " AND s.category = ?"
        params.append(category)
    if platform:
        sql += " AND p.platform = ?"
        params.append(platform)
    sql += f" ORDER BY {order_by} LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "post": _post_dict(r),
            "score": _score_dict_from_row(r),
            "recommended_action": r["recommended_action"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# GET /api/post/{post_id} — полная карточка drill-down.
# --------------------------------------------------------------------------- #

@router.get("/api/post/{post_id}")
def api_post_detail(request: Request, post_id: str):
    conn = request.app.state.db
    p = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if p is None:
        raise HTTPException(status_code=404, detail="post not found")

    e = db.get_extracted(conn, post_id)            # Extracted | None (dataclass)
    s = db.get_score_row(conn, post_id)            # sqlite3.Row | None

    entities = e.entities if e else []
    score = None
    if s is not None:
        score = Score(
            post_id=post_id,
            risk=s["risk"],
            category=s["category"],
            class_probs=json.loads(s["class_probs_json"] or "{}"),
            top_features=[
                FeatureHit(**f) for f in json.loads(s["top_features_json"] or "[]")
            ],
        )

    # Флаг «разрешён в РК»: матчим по ПОЛНОМУ тексту (combined_text, иначе склейка
    # caption+transcript+ocr_text), чтобы поймать оператора в речи/на кадре, а не
    # только в подписи. Риск/score/recommended_action НЕ меняем.
    if e is not None:
        licensed_text = e.combined_text or " ".join(
            x for x in (e.caption or "", e.transcript or "", e.ocr_text or "") if x
        )
    else:
        licensed_text = p["caption"] or ""
    # + author_handle: оператор может быть самим аккаунтом (@olimpbet/@parimatch).
    licensed_text = (licensed_text or "") + " " + (p["author_handle"] or "")
    post_dict = _post_dict(p, licensed_text=licensed_text)
    is_licensed = post_dict["licensed"]

    return {
        "post": post_dict,
        "extracted": asdict(e) if e is not None else None,
        "score": asdict(score) if score is not None else None,
        "explanation": explain(score, entities, e) if score is not None else [],
        "recommended_action": s["recommended_action"] if s is not None else None,
        # Подсказка по рекламным нормам + дисклеймер реестра — только когда лицензирован.
        "licensed_note": COMPLIANCE_HINT if is_licensed else "",
        "licensed_disclaimer": REGISTRY_DISCLAIMER if is_licensed else "",
    }


# --------------------------------------------------------------------------- #
# POST /api/tick — раскрыть следующий батч seed-постов (drip-reveal).
# --------------------------------------------------------------------------- #

@router.post("/api/tick")
def api_tick(request: Request):
    conn = request.app.state.db
    n = db.reveal_next(conn, config.TICK_REVEAL_N)
    total = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE revealed = 1"
    ).fetchone()[0]
    return {"revealed": n, "total_revealed": total}


# --------------------------------------------------------------------------- #
# POST /api/analyze — live-проверка по ссылке/файлу через собственный пайплайн.
# --------------------------------------------------------------------------- #

@router.post("/api/analyze")
async def api_analyze(request: Request):
    """Live-проверка по ссылке/файлу. Тяжёлый разбор видео (yt-dlp + Whisper + OCR +
    CLIP) идёт в ФОНЕ через очередь задач: сразу возвращаем job_id, прогресс/результат —
    через GET /api/jobs/{id}. Тело: JSON {url} ИЛИ multipart file (§0.5/§0.7).

    Ошибки тяжёлого пути НЕ роняют запрос 500-кой: они либо мягко деградируют (скорим
    доступный текст + note), либо становятся статусом задачи 'error'.
    """
    url = None
    upload = None

    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype:
        form = await request.form()
        f = form.get("file")
        if f is not None and hasattr(f, "read"):
            data = await f.read()
            upload = (data, getattr(f, "filename", None) or "upload.bin")
        else:
            url = form.get("url") or None
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            url = payload.get("url") or None

    if not url and upload is None:
        raise HTTPException(status_code=400, detail="нужна ссылка (url) или файл (file)")

    def _run(report):
        note = None
        report("получение медиа", 8)
        # 1) fetch — Post (yt-dlp/файл). Деградация: минимальный Post по ссылке.
        try:
            post = fetch_mod.fetch_post(url=url, upload=upload)
        except Exception as exc:
            note = f"Не удалось получить медиа ({exc}); анализ по доступным данным."
            post = Post(
                id=uuid.uuid4().hex,
                platform="link" if url else "upload",
                author_handle="",
                url=url or "upload://unknown",
                caption="",
                posted_at=datetime.now(timezone.utc).isoformat(),
                media_path=None, thumb_url=None, source="live",
            )
        # 2) extract — реальные мультимодальные признаки с прогрессом (15..80%).
        try:
            extracted = pipeline_mod.extract(
                post, use_cache=False,
                progress=lambda s, p: report(s, 15 + int(max(0, min(100, p)) * 0.65)),
            )
        except Exception as exc:
            note = (note + " " if note else "") + f"Тяжёлые экстракторы недоступны ({exc}); скоринг по тексту."
            cap = post.caption or ""
            extracted = Extracted(
                post_id=post.id, caption=cap, transcript="", ocr_text="",
                visual_concepts=[], combined_text=cap, entities=[],
            )
        # 3) НЕ засоряем ленту, если контента нет (скачивание упало / нет текста).
        has_content = bool(
            (post.caption or extracted.transcript or extracted.ocr_text or "").strip()
        )
        if not has_content:
            report("готово", 100)
            note = (note + " " if note else "") + (
                "Не удалось получить контент из источника (скачивание/доступ) — пост НЕ "
                "добавлен в ленту. Надёжный путь — загрузка файла."
            )
            return {
                "post_id": post.id, "post": asdict(post), "extracted": asdict(extracted),
                "score": {"post_id": post.id, "risk": 0, "category": "clean",
                          "class_probs": {}, "top_features": []},
                "explanation": [], "recommended_action": "auto_clear", "note": note,
            }
        # 4) скоринг своей моделью + персист (своё соединение — мы в worker-потоке).
        report("скоринг своей моделью", 88)
        wconn = db.connect()
        try:
            score = scoring_mod.score_post(post, extracted, conn=wconn)
            action = scoring_mod.recommend_action(score.risk)
            db.insert_post(wconn, post)
            db.upsert_extracted(wconn, extracted)
            db.reveal_post(wconn, post.id)
        finally:
            wconn.close()
        result = {
            "post_id": post.id,
            "post": asdict(post),
            "extracted": asdict(extracted),
            "score": asdict(score),
            "explanation": explain(score, extracted.entities, extracted),
            "recommended_action": action,
        }
        if note:
            result["note"] = note
        return result

    job_id = jobs.run_job("analyze", _run)
    return {"job_id": job_id, "status": "queued"}
