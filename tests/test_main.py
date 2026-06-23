import json

from fastapi.testclient import TestClient

from app import config
from app.main import app, ingestion_tick


def test_root_serves_index_html():
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "КӨЗ" in resp.text


def test_health_route():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_ingestion_tick_stub_is_callable():
    assert ingestion_tick() is None


def test_lifespan_opens_db_connection():
    with TestClient(app) as client:
        # lifespan must have stored a live connection on app.state
        assert app.state.db is not None
        assert app.state.db.execute("SELECT 1").fetchone()[0] == 1
        # ticker task started
        assert app.state.ticker is not None
        client.get("/health")


def test_metrics_route_returns_report(tmp_path, monkeypatch):
    p = tmp_path / "metrics.json"
    p.write_text(
        json.dumps({"macro_f1": 0.82, "n_train": 480, "n_test": 120,
                    "per_class": {}, "confusion_matrix": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "METRICS_PATH", p)
    with TestClient(app) as c:
        r = c.get("/api/metrics")
        assert r.status_code == 200
        assert r.json()["macro_f1"] == 0.82


def test_metrics_route_404_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_PATH", tmp_path / "missing.json")
    with TestClient(app) as c:
        assert c.get("/api/metrics").status_code == 404


def test_static_mount_serves_index_html_at_root():
    # html=True StaticFiles mount serves index.html for "/" and is last-mounted.
    with TestClient(app) as c:
        assert c.get("/index.html").status_code == 200
