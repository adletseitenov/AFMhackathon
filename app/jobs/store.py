"""Потокобезопасный in-memory реестр фоновых задач.

Задача (job) — это dict со стабильной схемой:
    {
        id:         str  — uuid hex
        kind:       str  — тип задачи (например 'analyze')
        status:     str  — 'queued' | 'running' | 'done' | 'error'
        stage:      str  — человекочитаемая стадия (RU), напр. 'качаю видео'
        progress:   int  — 0..100
        result:     dict | None — результат при успехе
        error:      str | None  — текст ошибки при провале
        created_at: str  — ISO-8601 UTC
    }

Все операции защищены одним threading.Lock — реестр читают/пишут одновременно
worker-потоки (worker.py) и обработчики запросов (routes.py).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

# Единый замок на весь реестр (демо-масштаб: десятки задач, не миллионы).
_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}

_VALID_STATUSES = ("queued", "running", "done", "error")


def _now_iso() -> str:
    """Текущее время UTC в ISO-8601 (datetime разрешён — не запрещённые часы)."""
    return datetime.now(timezone.utc).isoformat()


def new_job(kind: str) -> str:
    """Создать новую задачу в статусе 'queued' и вернуть её id (uuid hex)."""
    job_id = uuid.uuid4().hex
    job: dict[str, Any] = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "stage": "в очереди",
        "progress": 0,
        "result": None,
        "error": None,
        "created_at": _now_iso(),
    }
    with _LOCK:
        _JOBS[job_id] = job
    return job_id


def get(job_id: str) -> dict[str, Any] | None:
    """Вернуть КОПИЮ задачи (чтобы внешний код не мутировал реестр) или None."""
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job is not None else None


def update(job_id: str, **fields: Any) -> dict[str, Any] | None:
    """Обновить произвольные поля задачи. Неизвестный id -> None (без падения)."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        job.update(fields)
        return dict(job)


def set_stage(job_id: str, stage: str, progress: int) -> dict[str, Any] | None:
    """Обновить стадию и прогресс (прогресс зажимается в диапазон 0..100)."""
    try:
        progress = int(progress)
    except (TypeError, ValueError):
        progress = 0
    progress = max(0, min(100, progress))
    return update(job_id, stage=str(stage), progress=progress)


def all_jobs() -> list[dict[str, Any]]:
    """Снимок всех задач (копии) — для отладки/служебных нужд."""
    with _LOCK:
        return [dict(j) for j in _JOBS.values()]


def clear() -> None:
    """Очистить реестр (используется в тестах для изоляции)."""
    with _LOCK:
        _JOBS.clear()
