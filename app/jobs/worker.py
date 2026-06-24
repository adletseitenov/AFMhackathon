"""Пул потоков для фоновых задач КӨЗ.

Тяжёлая CPU-работа (скачивание/whisper/ocr/clip ~30с) запускается здесь,
чтобы не блокировать event-loop FastAPI. Модульный ThreadPoolExecutor с
двумя воркерами разделяется всем процессом.

Контракт fn:
    fn вызывается как fn(report), где report(stage:str, progress:int) пишет
    прогресс в store. Возвращаемое значение fn (dict) кладётся в job['result'].
    ЛЮБОЕ исключение из fn ловится здесь и фиксируется как status='error' —
    оно НИКОГДА не вылетает из потока (R3).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.jobs import store

# Два воркера: один длинный анализ не должен полностью застопорить очередь.
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="koz-job")

# Тип callback-репортёра, который передаётся в задачу.
Reporter = Callable[[str, int], None]
# Тип самой задачи: получает report, возвращает dict-результат.
JobFn = Callable[[Reporter], dict]


def _run(job_id: str, fn: JobFn) -> None:
    """Тело, исполняемое в потоке: отметить running, выполнить fn, зафиксировать итог."""
    store.update(job_id, status="running")

    def report(stage: str, progress: int) -> None:
        # Прогресс-callback для задачи; зажим 0..100 делает set_stage.
        store.set_stage(job_id, stage, progress)

    try:
        result = fn(report)
        if not isinstance(result, dict):
            # Нормализуем: контракт обещает result:dict|None.
            result = {"value": result} if result is not None else {}
        store.update(job_id, status="done", progress=100, result=result, error=None)
    except Exception as exc:  # noqa: BLE001 — ошибка задачи НЕ должна всплывать из потока
        store.update(job_id, status="error", error=str(exc) or exc.__class__.__name__)


def submit(job_id: str, fn: JobFn) -> None:
    """Поставить уже созданную задачу job_id на исполнение в пуле потоков."""
    _EXECUTOR.submit(_run, job_id, fn)


def run_job(kind: str, fn: JobFn) -> str:
    """Создать задачу типа kind и сразу отправить её в пул. Вернуть job_id."""
    job_id = store.new_job(kind)
    submit(job_id, fn)
    return job_id


def shutdown(wait: bool = False) -> None:
    """Остановить пул (на всякий случай; обычно живёт всё время процесса)."""
    _EXECUTOR.shutdown(wait=wait)
