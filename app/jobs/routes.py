"""HTTP-роут опроса статуса фоновых задач (авто-подключается main.py).

GET /api/jobs/{job_id} -> dict задачи (см. app/jobs/store.py).
Неизвестный id -> 404 (HTTPException), а не 500.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.jobs import store

router = APIRouter()


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    """Вернуть текущее состояние задачи по её id или 404, если такой нет."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="задача не найдена")
    return job
