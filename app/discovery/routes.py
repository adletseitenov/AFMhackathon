"""Роут НАСТРАИВАЕМОГО автономного поиска опасных постов (авто-подключается main.py).

POST /api/discover  body (опц.): {"per_query": 4, "queries": [...], "with_telegram": true,
  "platform": "all", "deep": false, "country": "all", "categories": [...],
  "content_type": "all", "sort": "relevance"}
-> ставит фоновую задачу (поиск + скоринг идут десятки секунд), возвращает {job_id}.
platform: "all"|"youtube"|"telegram"|"tiktok"|"instagram"|"twitch"|"kick".
country: "all"|"kz"|"ru". categories: список ["casino","pyramid","fraud","crypto"]
(или ["all"]). content_type: "all"|"video"|"live". sort: "relevance"|"recent"|"popular".
deep=true включает глубокий мультимодальный разбор (Whisper+OCR+CLIP) top-постов.
Прогресс/результат — через GET /api/jobs/{id}.

GET /api/discover/catalog -> {countries, categories, content_types, sorts} — реестр
из app/discovery/catalog.py, чтобы фронт сам наполнил селекторы.
"""

from fastapi import APIRouter, Body, Request

import app.discovery.catalog as catalog
import app.discovery.discover as discovery_mod
import app.jobs.worker as jobs
from app import db

router = APIRouter()


@router.get("/api/discover/catalog")
async def api_discover_catalog():
    """Реестр настраиваемого автопоиска (списки {id,label}) для селекторов фронта."""
    return catalog.catalog_payload()


@router.post("/api/discover")
async def api_discover(request: Request, payload: dict | None = Body(default=None)):
    payload = payload or {}
    per_query = max(1, min(8, int(payload.get("per_query") or 4)))
    queries = payload.get("queries") if isinstance(payload.get("queries"), list) else None
    with_tg = payload.get("with_telegram", True)
    raw_platform = payload.get("platform")
    platform = str(raw_platform).lower().strip() if raw_platform else "all"
    deep = bool(payload.get("deep", False))
    raw_country = payload.get("country")
    country = str(raw_country).lower().strip() if raw_country else "all"
    categories = payload.get("categories") if isinstance(payload.get("categories"), list) else None
    raw_ctype = payload.get("content_type")
    content_type = str(raw_ctype).lower().strip() if raw_ctype else "all"
    raw_sort = payload.get("sort")
    sort = str(raw_sort).lower().strip() if raw_sort else "relevance"

    def _run(report):
        report("автономный поиск в интернете", 3)
        wconn = db.connect()
        try:
            return discovery_mod.discover(
                wconn, queries=queries, per_query=per_query,
                with_telegram=bool(with_tg), platform=platform, deep=deep,
                country=country, categories=categories,
                content_type=content_type, sort=sort,
                report=report,
            )
        finally:
            wconn.close()

    return {"job_id": jobs.run_job("discover", _run), "status": "queued"}
