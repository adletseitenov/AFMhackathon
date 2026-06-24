"""Тесты фоновой очереди задач КӨЗ.

Тяжёлые либы НЕ нужны: задачи здесь — обычные python-функции. Воркеры —
настоящие потоки, поэтому ждём завершения опросом store.get с таймаутом.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.jobs import store, worker
from app.main import app


@pytest.fixture(autouse=True)
def _clean_registry():
    """Изоляция: чистый реестр до и после каждого теста."""
    store.clear()
    yield
    store.clear()


def _wait_until_settled(job_id: str, timeout: float = 5.0) -> dict:
    """Опрос store.get с ограниченным циклом, пока статус не done/error."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        assert job is not None
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.02)
    pytest.fail(f"задача {job_id} не завершилась за {timeout}s: {store.get(job_id)}")


# --------------------------------------------------------------------------- #
# store.py
# --------------------------------------------------------------------------- #


def test_new_job_initial_shape():
    job_id = store.new_job("analyze")
    job = store.get(job_id)
    assert job is not None
    assert job["id"] == job_id
    assert len(job_id) == 32  # uuid hex
    assert job["kind"] == "analyze"
    assert job["status"] == "queued"
    assert job["progress"] == 0
    assert job["result"] is None
    assert job["error"] is None
    assert isinstance(job["stage"], str)
    assert isinstance(job["created_at"], str) and "T" in job["created_at"]


def test_get_unknown_returns_none():
    assert store.get("does-not-exist") is None


def test_get_returns_copy_not_internal_reference():
    job_id = store.new_job("analyze")
    job = store.get(job_id)
    job["status"] = "tampered"
    # Мутация копии не должна затронуть реестр.
    assert store.get(job_id)["status"] == "queued"


def test_update_fields_and_unknown_id():
    job_id = store.new_job("analyze")
    store.update(job_id, status="running")
    assert store.get(job_id)["status"] == "running"
    assert store.update("nope", status="running") is None


def test_set_stage_clamps_progress():
    job_id = store.new_job("analyze")
    store.set_stage(job_id, "качаю", 50)
    j = store.get(job_id)
    assert j["stage"] == "качаю"
    assert j["progress"] == 50
    store.set_stage(job_id, "перебор", 250)
    assert store.get(job_id)["progress"] == 100
    store.set_stage(job_id, "недобор", -10)
    assert store.get(job_id)["progress"] == 0


# --------------------------------------------------------------------------- #
# worker.py — успех
# --------------------------------------------------------------------------- #


def test_run_job_success_reports_stage_and_result():
    observed = []

    def fn(report):
        report("качаю", 50)
        observed.append("качаю")
        return {"ok": True}

    job_id = worker.run_job("analyze", fn)
    job = _wait_until_settled(job_id)

    assert job["status"] == "done"
    assert job["progress"] == 100
    assert job["result"] == {"ok": True}
    assert job["error"] is None
    # Стадия 'качаю' действительно наблюдалась внутри задачи.
    assert "качаю" in observed


def test_run_job_marks_running_then_done():
    # Внутри задачи статус уже должен быть 'running'.
    seen_status = {}

    def fn(report):
        seen_status["during"] = store.get(report_job_id)["status"]
        return {"ok": True}

    # run_job создаёт id внутри — используем submit, чтобы знать id заранее.
    report_job_id = store.new_job("analyze")
    worker.submit(report_job_id, fn)
    job = _wait_until_settled(report_job_id)

    assert seen_status["during"] == "running"
    assert job["status"] == "done"


# --------------------------------------------------------------------------- #
# worker.py — ошибка (исключение не должно вылетать из потока)
# --------------------------------------------------------------------------- #


def test_run_job_failure_sets_error_and_does_not_raise():
    def fn(report):
        report("ломаюсь", 10)
        raise ValueError("бум")

    job_id = worker.run_job("analyze", fn)
    job = _wait_until_settled(job_id)

    assert job["status"] == "error"
    assert job["error"] == "бум"
    assert job["result"] is None


# --------------------------------------------------------------------------- #
# routes.py — GET /api/jobs/{id}
# --------------------------------------------------------------------------- #


def test_route_returns_job():
    job_id = store.new_job("analyze")
    store.set_stage(job_id, "качаю", 50)
    with TestClient(app) as client:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == job_id
        assert body["stage"] == "качаю"
        assert body["progress"] == 50


def test_route_404_for_unknown():
    with TestClient(app) as client:
        resp = client.get("/api/jobs/unknown-id")
        assert resp.status_code == 404
