"""F2 pipeline tests: combined_text order (§0.10), entity extraction, and the
cache-hit path returning Extracted WITHOUT loading any heavy model (A6)."""

import app.extractors.audio as audio
import app.extractors.ocr as ocr
import app.extractors.visual as visual
from app import config, db
from app.extractors import pipeline
from app.extractors.pipeline import _build_extracted_from_parts, build_combined_text
from app.models import Extracted, Post, VisualConcept


def _post(pid="cached1", caption="доход 50% в месяц"):
    return Post(
        id=pid,
        platform="tiktok",
        author_handle="@x",
        url="http://x",
        caption=caption,
        posted_at="2026-06-24T10:00:00",
        media_path=None,
        thumb_url=None,
        source="seed",
    )


# --- combined_text (§0.10 canonical order) ---
def test_combined_text_concatenation_order():
    combined = build_combined_text(
        caption="кэпшн",
        transcript="транскрипт",
        ocr_text="окр",
        visual_labels=["casino", "roulette"],
    )
    assert combined == "кэпшн транскрипт окр casino roulette"


def test_combined_text_skips_empty_parts():
    combined = build_combined_text(
        caption="кэпшн", transcript="", ocr_text="окр", visual_labels=[]
    )
    assert combined == "кэпшн окр"


def test_build_extracted_runs_entity_extraction():
    ex = _build_extracted_from_parts(
        post_id="p1",
        caption="Пиши https://t.me/win_bot, доход 30% в месяц",
        transcript="",
        ocr_text="",
        visual_concepts=[],
    )
    types = {e.type for e in ex.entities}
    assert "telegram" in types
    assert "payout_promise" in types
    assert ex.combined_text == "Пиши https://t.me/win_bot, доход 30% в месяц"
    assert ex.post_id == "p1"


def test_build_extracted_includes_visual_labels_in_combined():
    ex = _build_extracted_from_parts(
        post_id="p2",
        caption="кэпшн",
        transcript="речь",
        ocr_text="текст",
        visual_concepts=[VisualConcept(label="casino", score=0.9)],
    )
    assert ex.combined_text == "кэпшн речь текст casino"


# --- cache hit MUST NOT touch heavy models ---
def test_cached_extract_does_not_invoke_heavy_models(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("heavy model loaded on cache hit")

    monkeypatch.setattr(audio, "_load_model", boom)
    monkeypatch.setattr(ocr, "_load_reader", boom)
    monkeypatch.setattr(visual, "_load_model", boom)

    cached = Extracted(
        post_id="cached1",
        caption="доход 50% в месяц",
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text="доход 50% в месяц",
        entities=[],
    )
    monkeypatch.setattr(pipeline, "_load_cached", lambda pid: cached)

    result = pipeline.extract(_post("cached1"), use_cache=True)
    assert result is cached
    assert result.combined_text == "доход 50% в месяц"


def test_cache_hit_from_ondisk_json(tmp_path, monkeypatch):
    # Реальный on-disk кэш: extract читает JSON и не дергает БД/тяжёлые модели.
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)

    def boom(*a, **k):
        raise AssertionError("heavy model loaded on disk-cache hit")

    monkeypatch.setattr(audio, "_load_model", boom)
    monkeypatch.setattr(ocr, "_load_reader", boom)
    monkeypatch.setattr(visual, "_load_model", boom)

    (tmp_path / "diskpost.json").write_text(
        '{"post_id":"diskpost","caption":"казино 1xbet","transcript":"",'
        '"ocr_text":"","visual_concepts":[{"label":"casino","score":0.8}],'
        '"combined_text":"казино 1xbet casino",'
        '"entities":[{"type":"betting_brand","value":"1xbet","normalized":"1xbet"}]}',
        encoding="utf-8",
    )
    result = pipeline.extract(_post("diskpost"), use_cache=True)
    assert result.combined_text == "казино 1xbet casino"
    assert result.visual_concepts[0].label == "casino"
    assert result.entities[0].type == "betting_brand"


def test_cache_hit_from_db_extracted_row(tmp_path, monkeypatch):
    # Нет on-disk файла -> _load_cached падает в БД и находит строку extracted.
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "empty_cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    db.init_db(conn)
    ex = Extracted(
        post_id="dbpost",
        caption="доход 30% в месяц",
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text="доход 30% в месяц",
        entities=[],
    )
    db.upsert_extracted(conn, ex)
    conn.close()

    def boom(*a, **k):
        raise AssertionError("heavy model loaded on db-cache hit")

    monkeypatch.setattr(audio, "_load_model", boom)
    monkeypatch.setattr(ocr, "_load_reader", boom)
    monkeypatch.setattr(visual, "_load_model", boom)

    result = pipeline.extract(_post("dbpost"), use_cache=True)
    assert result.post_id == "dbpost"
    assert result.combined_text == "доход 30% в месяц"


def test_use_cache_false_skips_cache_lookup(monkeypatch):
    called = {"cache": False}

    def fake_cache(pid):
        called["cache"] = True
        return None

    monkeypatch.setattr(pipeline, "_load_cached", fake_cache)
    monkeypatch.setattr(
        pipeline,
        "_run_live",
        lambda post: Extracted(
            post_id=post.id,
            caption=post.caption,
            transcript="",
            ocr_text="",
            visual_concepts=[],
            combined_text=post.caption,
            entities=[],
        ),
    )
    monkeypatch.setattr(pipeline, "_persist", lambda ex: None)
    pipeline.extract(_post("live1"), use_cache=False)
    assert called["cache"] is False


def test_live_path_persists_and_then_cache_hits(tmp_path, monkeypatch):
    # use_cache=False -> live (media_path None => no heavy models) -> persist;
    # затем use_cache=True читает закэшированный on-disk JSON.
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect()
    db.init_db(conn)
    conn.close()

    post = _post("livepersist", caption="ставки на mostbet, доход 40% в месяц")
    ex = pipeline.extract(post, use_cache=False)
    assert ex.post_id == "livepersist"
    assert "betting_brand" in {e.type for e in ex.entities}
    assert (tmp_path / "livepersist.json").exists()

    again = pipeline.extract(post, use_cache=True)
    assert again.combined_text == ex.combined_text
