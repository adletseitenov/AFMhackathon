"""Роут автономного поиска опасных постов (авто-подключается main.py).

POST /api/discover  body (опц.): {"per_query": 4, "queries": [...], "with_telegram": true}
-> ставит фоновую задачу (поиск + скоринг идут десятки секунд), возвращает {job_id}.
Прогресс/результат — через GET /api/jobs/{id}.
"""

from fastapi import APIRouter, Body, Request

import app.discovery.discover as discovery_mod
import app.jobs.worker as jobs
from app import db

router = APIRouter()


@router.post("/api/discover")
async def api_discover(request: Request, payload: dict | None = Body(default=None)):
    payload = payload or {}
    per_query = max(1, min(8, int(payload.get("per_query") or 4)))
    queries = payload.get("queries") if isinstance(payload.get("queries"), list) else None
    with_tg = payload.get("with_telegram", True)

    def _run(report):
        report("автономный поиск в интернете", 3)
        wconn = db.connect()
        try:
            return discovery_mod.discover(
                wconn, queries=queries, per_query=per_query,
                with_telegram=bool(with_tg), report=report,
            )
        finally:
            wconn.close()

    return {"job_id": jobs.run_job("discover", _run), "status": "queued"}
