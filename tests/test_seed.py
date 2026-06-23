"""D2 — тесты app/ingestion/seed: загрузка demo в БД + кэш + первичный скоринг."""

from app import config, db
from app.ingestion import seed


def test_load_seed_inserts_posts_extracted_and_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    db.init_db(conn)
    n = seed.load_seed(conn)
    assert n >= 14
    rows = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert rows == n  # каждый seed-пост скорен (§0.8)
    pid = conn.execute("SELECT id FROM posts LIMIT 1").fetchone()[0]
    ex = db.get_extracted(conn, pid)
    assert ex is not None  # extracted закэширован в БД
    conn.close()


def test_load_seed_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    db.init_db(conn)
    first = seed.load_seed(conn)
    assert first >= 14
    # второй вызов на непустой БД -> 0, без дублей
    second = seed.load_seed(conn)
    assert second == 0
    posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    assert posts == first
    conn.close()


def test_seed_posts_start_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    db.init_db(conn)
    seed.load_seed(conn)
    revealed = conn.execute("SELECT COUNT(*) FROM posts WHERE revealed=1").fetchone()[0]
    assert revealed == 0  # стартуют скрытыми (drip-reveal через тикер)
    conn.close()


def test_seed_falls_back_to_build_demo_when_no_file(tmp_path, monkeypatch):
    # DEMO_POSTS_PATH не существует -> seed строит записи через build_demo_records.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DEMO_POSTS_PATH", tmp_path / "missing.jsonl")
    conn = db.connect()
    db.init_db(conn)
    n = seed.load_seed(conn)
    assert n >= 14
    conn.close()


def test_seed_scores_have_recommended_action(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    db.init_db(conn)
    seed.load_seed(conn)
    rows = conn.execute(
        "SELECT recommended_action FROM scores"
    ).fetchall()
    actions = {r["recommended_action"] for r in rows}
    assert actions  # непусто
    assert actions.issubset({"auto_clear", "review", "escalate"})
    conn.close()


def test_seed_high_risk_post_escalates(tmp_path, monkeypatch):
    # Реальный обученный классификатор должен эскалировать явный казино/пирамида-пост.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    db.init_db(conn)
    seed.load_seed(conn)
    rows = conn.execute("SELECT risk, recommended_action FROM scores").fetchall()
    risks = [r["risk"] for r in rows]
    assert max(risks) >= 70  # есть хотя бы один высокорисковый пост
    assert "escalate" in {r["recommended_action"] for r in rows}
