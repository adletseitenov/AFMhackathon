from app import db
from app.models import Entity, Extracted, FeatureHit, Post, Score, VisualConcept


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _cols(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _post(pid="p1", source="seed"):
    return Post(
        id=pid, platform="tiktok", author_handle="@x", url="http://u",
        caption="играй и выигрывай", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source=source,
    )


# --- schema ---

def test_init_db_creates_all_tables(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    assert {"posts", "extracted", "scores", "audit"} <= _tables(conn)


def test_posts_columns(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    assert _cols(conn, "posts") == [
        "id", "platform", "author_handle", "url", "caption",
        "posted_at", "media_path", "thumb_url", "source", "revealed",
    ]


def test_extracted_and_scores_columns(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    assert _cols(conn, "extracted") == [
        "post_id", "caption", "transcript", "ocr_text",
        "visual_concepts_json", "combined_text", "entities_json",
    ]
    assert _cols(conn, "scores") == [
        "post_id", "risk", "category", "class_probs_json",
        "top_features_json", "recommended_action", "scored_at",
    ]


def test_audit_columns(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    assert _cols(conn, "audit") == ["id", "ts", "post_id", "action", "actor", "detail"]


def test_init_db_is_idempotent(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.init_db(conn)
    assert {"posts", "extracted", "scores", "audit"} <= _tables(conn)


def test_row_factory_is_row(tmp_path):
    import sqlite3
    conn = db.connect(str(tmp_path / "t.db"))
    assert conn.row_factory is sqlite3.Row


# --- helpers ---

def test_insert_post_and_get(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.insert_post(conn, _post())
    got = db.get_post(conn, "p1")
    assert got.id == "p1"
    assert got.platform == "tiktok"
    assert got.source == "seed"
    assert db.get_post(conn, "missing") is None


def test_upsert_extracted_roundtrip(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.insert_post(conn, _post())
    ex = Extracted(
        post_id="p1", caption="c", transcript="t", ocr_text="o",
        visual_concepts=[VisualConcept(label="casino", score=0.7)],
        combined_text="c t o",
        entities=[Entity(type="casino_brand", value="1xBet", normalized="1xbet")],
    )
    db.upsert_extracted(conn, ex)
    got = db.get_extracted(conn, "p1")
    assert got.visual_concepts[0].label == "casino"
    assert got.visual_concepts[0].score == 0.7
    assert got.entities[0].normalized == "1xbet"
    assert got.combined_text == "c t o"


def test_upsert_score_sets_action(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.insert_post(conn, _post())
    sc = Score(
        post_id="p1", risk=85, category="gambling",
        class_probs={"gambling": 0.85, "clean": 0.05},
        top_features=[FeatureHit(feature="casino_brand", weight=0.6, evidence="1xBet")],
    )
    db.upsert_score(conn, sc, recommended_action="escalate", scored_at="2026-06-24T10:01:00")
    row = db.get_score_row(conn, "p1")
    assert row["risk"] == 85
    assert row["recommended_action"] == "escalate"
    assert row["category"] == "gambling"


def test_add_audit_autoincrement(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.add_audit(conn, ts="t1", post_id="p1", action="scored", actor="system", detail="d1")
    db.add_audit(conn, ts="t2", post_id="p1", action="revealed", actor="system", detail="d2")
    rows = conn.execute("SELECT id, action FROM audit ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[1]["action"] == "revealed"


def test_get_revealed_posts(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.insert_post(conn, _post("p1"))
    db.insert_post(conn, _post("p2"))
    conn.execute("UPDATE posts SET revealed=1 WHERE id='p1'")
    conn.commit()
    revealed = db.get_revealed_posts(conn)
    assert [p.id for p in revealed] == ["p1"]


def test_reveal_next(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.insert_post(conn, _post("p1"))
    db.insert_post(conn, _post("p2"))
    db.insert_post(conn, _post("p3"))
    n = db.reveal_next(conn, 2)
    assert n == 2
    assert {p.id for p in db.get_revealed_posts(conn)} == {"p1", "p2"}
    # third reveal: only one left
    assert db.reveal_next(conn, 5) == 1


def test_reveal_post(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.insert_post(conn, _post("p1"))
    db.insert_post(conn, _post("p2"))
    db.reveal_post(conn, "p2")
    assert [p.id for p in db.get_revealed_posts(conn)] == ["p2"]
