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
from app.models import Extracted, FeatureHit, Post, Score

router = APIRouter()


# --------------------------------------------------------------------------- #
# Сериализация строк БД в форму контракта (§0.7).
# --------------------------------------------------------------------------- #

def _post_dict(r) -> dict:
    return {
        "id": r["id"], "platform": r["platform"], "author_handle": r["author_handle"],
        "url": r["url"], "caption": r["caption"], "posted_at": r["posted_at"],
        "media_path": r["media_path"], "thumb_url": r["thumb_url"], "source": r["source"],
    }


def _score_dict_from_row(r) -> dict:
    return {
        "post_id": r["post_id"], "risk": r["risk"], "category": r["category"],
        "class_probs": json.loads(r["class_probs_json"] or "{}"),
        "top_features": json.loads(r["top_features_json"] or "[]"),
    }


# --------------------------------------------------------------------------- #
# GET /api/feed — приоритетная очередь revealed-постов, сорт по риску desc.
# --------------------------------------------------------------------------- #

@router.get("/api/feed")
def api_feed(
    request: Request,
    min_risk: int = Query(0),
    category: "str | None" = Query(None),
    limit: int = Query(100),
):
    conn = request.app.state.db
    sql = (
        "SELECT p.id, p.platform, p.author_handle, p.url, p.caption, p.posted_at, "
        "p.media_path, p.thumb_url, p.source, "
        "s.post_id, s.risk, s.category, s.class_probs_json, s.top_features_json, "
        "s.recommended_action "
        "FROM posts p JOIN scores s ON s.post_id = p.id "
        "WHERE p.revealed = 1 AND s.risk >= ?"
    )
    params: list = [min_risk]
    if category:
        sql += " AND s.category = ?"
        params.append(category)
    sql += " ORDER BY s.risk DESC LIMIT ?"
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

    return {
        "post": _post_dict(p),
        "extracted": asdict(e) if e is not None else None,
        "score": asdict(score) if score is not None else None,
        "explanation": explain(score, entities) if score is not None else [],
        "recommended_action": s["recommended_action"] if s is not None else None,
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
            "explanation": explain(score, extracted.entities),
            "recommended_action": action,
        }
        if note:
            result["note"] = note
        return result

    job_id = jobs.run_job("analyze", _run)
    return {"job_id": job_id, "status": "queued"}
