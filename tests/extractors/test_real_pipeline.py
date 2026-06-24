"""F2 «реальный» live-путь pipeline: progress-колбэк, кэш-хит без тяжёлых
моделей, и мягкая деградация экстракторов БЕЗ установленных тяжёлых библиотек.

Эти тесты НЕ требуют torch/faster_whisper/easyocr/open_clip/cv2 — они либо
проверяют graceful-фолбэк (возврат ""/[] когда библиотека отсутствует), либо
monkeypatch'ат seam'ы, чтобы тяжёлый код вообще не вызывался.
"""

import app.extractors.audio as audio
import app.extractors.ocr as ocr
import app.extractors.visual as visual
from app import config
from app.extractors import pipeline
from app.models import Post


def _post(pid="real1", caption="доход 50% в месяц", media_path=None):
    return Post(
        id=pid,
        platform="tiktok",
        author_handle="@x",
        url="http://x",
        caption=caption,
        posted_at="2026-06-24T10:00:00",
        media_path=media_path,
        thumb_url=None,
        source="seed",
    )


# --- (a) progress-колбэк: упорядоченные этапы для текстового поста (без media) ---
def test_progress_callback_reports_ordered_stages_for_text_only_post(
    tmp_path, monkeypatch
):
    # Изолируем кэш/БД во временную папку, чтобы реально пройти live-путь.
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from app import db

    conn = db.connect()
    db.init_db(conn)
    conn.close()

    stages = []

    def progress(stage, percent):
        stages.append((stage, percent))

    post = _post("textonly", caption="ставки на mostbet, доход 40% в месяц")
    ex = pipeline.extract(post, use_cache=True, progress=progress)

    reported = [s for s, _p in stages]
    # Первый этап — кэш, последний — готово, порядок live-этапов соблюдён.
    assert reported[0] == pipeline.STAGE_CACHE
    assert reported[-1] == pipeline.STAGE_DONE
    assert pipeline.STAGE_DONE in reported
    # Все объявленные live-этапы присутствуют и идут в каноническом порядке.
    expected_order = [
        pipeline.STAGE_CACHE,
        pipeline.STAGE_DOWNLOAD,
        pipeline.STAGE_AUDIO,
        pipeline.STAGE_OCR,
        pipeline.STAGE_VISUAL,
        pipeline.STAGE_DONE,
    ]
    assert reported == expected_order
    # Проценты монотонно не убывают.
    percents = [p for _s, p in stages]
    assert percents == sorted(percents)
    # Extracted построен из caption (media не было -> transcript/ocr/visual пусты).
    assert ex.post_id == "textonly"
    assert ex.caption == "ставки на mostbet, доход 40% в месяц"
    assert ex.transcript == ""
    assert ex.ocr_text == ""
    assert ex.visual_concepts == []
    assert "mostbet" in ex.combined_text
    assert "betting_brand" in {e.type for e in ex.entities}


def test_progress_is_optional_none_safe(tmp_path, monkeypatch):
    # progress=None (по умолчанию) не должен ломать разбор.
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from app import db

    conn = db.connect()
    db.init_db(conn)
    conn.close()

    ex = pipeline.extract(_post("nopg", caption="привет мир"))
    assert ex.post_id == "nopg"
    assert ex.combined_text == "привет мир"


def test_progress_callback_errors_do_not_break_pipeline(tmp_path, monkeypatch):
    # Кривой колбэк потребителя НЕ должен валить пайплайн (§R3).
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    from app import db

    conn = db.connect()
    db.init_db(conn)
    conn.close()

    def bad_progress(stage, percent):
        raise RuntimeError("boom from consumer callback")

    ex = pipeline.extract(
        _post("badpg", caption="доход 30% в месяц"),
        use_cache=True,
        progress=bad_progress,
    )
    assert ex.post_id == "badpg"
    assert "payout_promise" in {e.type for e in ex.entities}


# --- (b) кэш-хит возвращает мгновенно, БЕЗ вызова тяжёлых экстракторов ---
def test_cache_hit_does_not_call_heavy_extractors(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)

    def boom(*a, **k):
        raise AssertionError("heavy extractor called on cache hit")

    # Глушим сами seam-функции в pipeline (как их использует _run_live).
    monkeypatch.setattr(pipeline, "transcribe", boom)
    monkeypatch.setattr(pipeline, "ocr_frames", boom)
    monkeypatch.setattr(pipeline, "visual_concepts", boom)
    monkeypatch.setattr(pipeline, "sample_frames", boom)
    # И на уровень глубже — на случай регрессии.
    monkeypatch.setattr(audio, "_load_model", boom)
    monkeypatch.setattr(ocr, "_load_reader", boom)
    monkeypatch.setattr(visual, "_load_model", boom)

    # Сеем on-disk кэш для поста, у которого ЕСТЬ media_path (чтобы доказать, что
    # даже live-выглядящий пост на кэш-хите не трогает экстракторы).
    (tmp_path / "seeded.json").write_text(
        '{"post_id":"seeded","caption":"казино 1xbet","transcript":"речь",'
        '"ocr_text":"бонус","visual_concepts":[{"label":"casino","score":0.8}],'
        '"combined_text":"казино 1xbet речь бонус casino",'
        '"entities":[{"type":"betting_brand","value":"1xbet","normalized":"1xbet"}]}',
        encoding="utf-8",
    )

    stages = []
    result = pipeline.extract(
        _post("seeded", media_path="/nonexistent/video.mp4"),
        use_cache=True,
        progress=lambda s, p: stages.append(s),
    )
    # Вернулся закэшированный Extracted без обращения к тяжёлым моделям.
    assert result.post_id == "seeded"
    assert result.combined_text == "казино 1xbet речь бонус casino"
    assert result.visual_concepts[0].label == "casino"
    # На кэш-хите прогресс — только кэш -> готово (без live-этапов).
    assert stages == [pipeline.STAGE_CACHE, pipeline.STAGE_DONE]


# --- (c) при отсутствии тяжёлых библиотек экстракторы деградируют мягко ---
# Форсируем "библиотека недоступна", глуша загрузчики моделей -> None. Так тест
# проверяет именно graceful-фолбэк и НЕ зависит от того, установлены ли тяжёлые
# библиотеки (и не грузит/не качает веса моделей — быстро и детерминированно).
def test_audio_transcribe_graceful_without_lib(monkeypatch):
    monkeypatch.setattr(audio, "_load_model", lambda: None)
    assert audio.transcribe("x") == ""
    assert audio.transcribe("/nonexistent/path/audio.mp4") == ""
    assert audio.transcribe(None) == ""
    assert audio.transcribe("") == ""


def test_ocr_frames_graceful_without_lib(monkeypatch):
    monkeypatch.setattr(ocr, "_load_reader", lambda: None)
    assert ocr.ocr_frames([]) == ""
    assert ocr.ocr_frames(None) == ""
    assert ocr.ocr_frames(["/nonexistent/frame0.png"]) == ""


def test_visual_concepts_graceful_without_lib(monkeypatch):
    monkeypatch.setattr(visual, "_load_model", lambda: (None, None, None))
    assert visual.visual_concepts([]) == []
    assert visual.visual_concepts(None) == []
    assert visual.visual_concepts(["/nonexistent/frame0.png"]) == []
