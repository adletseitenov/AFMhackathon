"""F-обратная связь КӨЗ — захват вердикта аналитика + активное обучение (авто-роутер).

Модуль экспортирует module-level `router`; app/main.py автоподключает его через
pkgutil-walk — main.py НЕ правится. Соединение к БД берётся из request.app.state.db
(одно соединение из lifespan, §0.2).

Идея активного обучения: аналитик выносит вердикт по карточке (подтвердить /
отклонить / переклассифицировать), вердикт превращается в одну строку обучающей
выборки `data/analyst_labels.jsonl` — это КОНТРАКТ, который потребляет агент
модели/обучения (через scripts.gen_dataset / app.model.train он подмешивает эти
строки в датасет). Формат строки (append-only, UTF-8, по одному JSON-объекту):

    {"text": <combined_text поста>, "label": <одна из gambling|pyramid|fraud|clean>}

Разрешение метки по вердикту:
  - "confirm"     => метка = предсказанная моделью категория (категория из scores);
                     если модель сказала "clean", метка остаётся "clean".
  - "reject"      => метка = "clean" (аналитик говорит: на самом деле НЕ угроза).
  - "reclassify"  => метка = переданная `category` (обязана быть валидной категорией).

Эндпоинты:
  POST /api/feedback/verdict   — записать вердикт -> {ok, total_labels}
  GET  /api/feedback/stats     — {total_labels, labels_until_retrain, by_label}
  GET  /api/feedback/uncertain?limit= — посты у границы решения (форма ленты)
  POST /api/feedback/retrain   — фоновое переобучение -> {job_id}

ВАЖНО (тестируемость): путь к файлу меток вынесен в module-атрибут `LABELS_PATH`,
а фактическое переобучение — в функцию `_retrain_job`; тесты их monkeypatch'ят,
чтобы не засорять реальные данные и не запускать настоящий subprocess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

import app.jobs.worker as jobs
from app import config, db

router = APIRouter()

# Путь к обучающей выборке аналитика (КОНТРАКТ с агентом модели). Module-атрибут,
# чтобы тесты могли monkeypatch'ить его на временный файл.
LABELS_PATH = config.DATA_DIR / "analyst_labels.jsonl"

# Сколько новых меток должно накопиться до рекомендации переобучить модель.
RETRAIN_BATCH = 10

# Допустимые вердикты и категории.
_VERDICTS = ("confirm", "reject", "reclassify")


# --------------------------------------------------------------------------- #
# Вспомогательное: чтение/запись файла меток.
# --------------------------------------------------------------------------- #

def _labels_path():
    """Текущий путь к файлу меток (читаем через атрибут модуля, чтобы monkeypatch работал)."""
    import app.feedback.routes as _self
    return _self.LABELS_PATH


def _read_labels() -> list[dict]:
    """Прочитать все строки меток. Битые строки пропускаются (append-only устойчивость)."""
    path = _labels_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_label(text: str, label: str) -> int:
    """Дописать одну метку {"text", "label"} в JSONL (UTF-8). Вернуть новое число меток."""
    path = _labels_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"text": text, "label": label}, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return len(_read_labels())


def _combined_text(conn, post_id: str) -> str:
    """Достать combined_text поста (caption+transcript+ocr); фолбэк на caption поста."""
    ex = db.get_extracted(conn, post_id)
    if ex is not None and (ex.combined_text or "").strip():
        return ex.combined_text
    if ex is not None:
        joined = " ".join(
            t for t in (ex.caption, ex.transcript, ex.ocr_text) if t
        ).strip()
        if joined:
            return joined
    post = db.get_post(conn, post_id)
    return (post.caption if post is not None else "") or ""


# --------------------------------------------------------------------------- #
# Сериализация строки БД в форму ленты (совпадает с GET /api/feed).
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
# POST /api/feedback/verdict — захват вердикта аналитика.
# --------------------------------------------------------------------------- #

@router.post("/api/feedback/verdict")
async def api_verdict(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="ожидается JSON-тело")

    post_id = payload.get("post_id")
    verdict = payload.get("verdict")
    category = payload.get("category")
    reason = payload.get("reason") or ""

    if not post_id:
        raise HTTPException(status_code=400, detail="не указан post_id")
    if verdict not in _VERDICTS:
        raise HTTPException(
            status_code=400,
            detail=f"verdict должен быть одним из {_VERDICTS}",
        )

    conn = request.app.state.db
    post = db.get_post(conn, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="пост не найден")

    score_row = db.get_score_row(conn, post_id)
    model_category = score_row["category"] if score_row is not None else "clean"

    # Разрешение итоговой метки.
    if verdict == "confirm":
        label = model_category
    elif verdict == "reject":
        label = "clean"
    else:  # reclassify
        if category not in config.CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail=f"для reclassify нужна category из {config.CATEGORIES}",
            )
        label = category

    text = _combined_text(conn, post_id)
    total = _append_label(text, label)

    # Аудит-строка о вердикте аналитика.
    ts = datetime.now(timezone.utc).isoformat()
    detail = json.dumps(
        {"verdict": verdict, "label": label, "model_category": model_category,
         "reason": reason},
        ensure_ascii=False,
    )
    db.add_audit(conn, ts=ts, post_id=post_id, action="analyst_verdict",
                 actor="analyst", detail=detail)

    return {"ok": True, "total_labels": total}


# --------------------------------------------------------------------------- #
# GET /api/feedback/stats — статистика накопленных меток.
# --------------------------------------------------------------------------- #

@router.get("/api/feedback/stats")
def api_stats():
    rows = _read_labels()
    total = len(rows)
    counts = Counter(r.get("label") for r in rows if r.get("label"))
    # labels_until_retrain: сколько меток до следующего полного батча RETRAIN_BATCH.
    # Пустой набор -> весь батч; ровно на границе батча -> снова весь батч.
    remainder = total % RETRAIN_BATCH
    labels_until_retrain = RETRAIN_BATCH - remainder if remainder else RETRAIN_BATCH
    return {
        "total_labels": total,
        "labels_until_retrain": labels_until_retrain,
        "by_label": dict(counts),
    }


# --------------------------------------------------------------------------- #
# GET /api/feedback/uncertain — посты у границы решения (для приоритета разметки).
# --------------------------------------------------------------------------- #

@router.get("/api/feedback/uncertain")
def api_uncertain(request: Request, limit: int = Query(20), band: int = Query(10)):
    """Посты, чей последний risk близок к границе REVIEW(40) или ESCALATE(70).

    «Близко» = в пределах +/- `band` (по умолчанию 10) от любого порога. Форма
    строки совпадает с /api/feed ({post, score, recommended_action}), чтобы фронт
    переиспользовал тот же рендер карточки. Только revealed-посты, новые сверху.
    """
    conn = request.app.state.db
    lo_r, hi_r = config.REVIEW_THRESHOLD - band, config.REVIEW_THRESHOLD + band
    lo_e, hi_e = config.ESCALATE_THRESHOLD - band, config.ESCALATE_THRESHOLD + band
    rows = conn.execute(
        "SELECT p.id, p.platform, p.author_handle, p.url, p.caption, p.posted_at, "
        "p.media_path, p.thumb_url, p.source, "
        "s.post_id, s.risk, s.category, s.class_probs_json, s.top_features_json, "
        "s.recommended_action "
        "FROM posts p JOIN scores s ON s.post_id = p.id "
        "WHERE p.revealed = 1 AND ("
        "  (s.risk >= ? AND s.risk <= ?) OR (s.risk >= ? AND s.risk <= ?)) "
        "ORDER BY p.posted_at DESC, s.risk DESC LIMIT ?",
        (lo_r, hi_r, lo_e, hi_e, limit),
    ).fetchall()
    return [
        {
            "post": _post_dict(r),
            "score": _score_dict_from_row(r),
            "recommended_action": r["recommended_action"],
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# POST /api/feedback/retrain — фоновое переобучение собственной модели.
# --------------------------------------------------------------------------- #

def _read_macro_f1() -> float | None:
    """Прочитать macro_f1 из metrics.json; None при любой ошибке (не валим запрос)."""
    try:
        data = json.loads(config.METRICS_PATH.read_text(encoding="utf-8"))
        return data.get("macro_f1")
    except Exception:
        return None


def _retrain_job(report) -> dict:
    """Тело фоновой задачи: до/после macro_f1 вокруг `python -m app.model.train`.

    Каждый шаг устойчив к сбою — провал шага НЕ роняет задачу 500-кой (R3).
    Возвращает {before, after, delta, total_labels}.
    """
    report("чтение метрик до", 5)
    before = _read_macro_f1()

    total_labels = len(_read_labels())

    report("переобучение собственной модели", 20)
    try:
        subprocess.run(
            [sys.executable, "-m", "app.model.train"],
            cwd=str(config.BASE_DIR),
            check=False,
            capture_output=True,
        )
    except Exception:
        # Сбой запуска тренировки не должен валить задачу — статус останется 'done'
        # с before/after как есть (after может совпасть с before).
        pass

    report("чтение метрик после", 90)
    after = _read_macro_f1()

    delta = None
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        delta = round(after - before, 4)

    report("готово", 100)
    return {
        "before": before,
        "after": after,
        "delta": delta,
        "total_labels": total_labels,
    }


@router.post("/api/feedback/retrain")
def api_retrain():
    """Поставить переобучение в фоновую очередь задач, вернуть {job_id}."""
    import app.feedback.routes as _self
    job_id = jobs.run_job("retrain", _self._retrain_job)
    return {"job_id": job_id, "status": "queued"}
