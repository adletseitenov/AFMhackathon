"""Оркестратор экстракторов КӨЗ. Кэш по post_id; ленивые тяжёлые модели.

extract(post, use_cache=True, progress=None) -> Extracted:
  - на КЭШ-ХИТ (on-disk JSON ИЛИ строка в таблице extracted) возвращает мгновенно,
    НЕ трогая ни одну тяжёлую модель (whisper/ocr/clip) — критично для seed/demo;
  - иначе запускает полный мультимодальный разбор (live-путь) и персистит;
  - опциональный progress(stage:str, percent:int) вызывается вокруг каждого этапа
    ('кэш','загрузка','аудио→текст','текст с экрана (OCR)','визуальные маркеры',
    'готово'). progress None-safe: None отключает уведомления.

combined_text строится в каноническом порядке §0.10:
  caption, transcript, ocr_text, затем labels визуал-концептов.

DB-хелперы F0 принимают conn ПЕРВЫМ аргументом (§0.2). Это standalone-слой, поэтому
_load_cached/_persist открывают своё conn = db.connect() и закрывают в finally.
"""

import inspect
import json
import os

from app import config, db
from app.extractors.audio import transcribe
from app.extractors.ocr import ocr_frames
from app.extractors.text import extract_entities, normalize
from app.extractors.visual import visual_concepts
from app.ingestion.fetch import sample_frames
from app.models import Entity, Extracted, Post, VisualConcept

# Канонические человекочитаемые названия этапов прогресса (RU, §R3).
STAGE_CACHE = "кэш"
STAGE_DOWNLOAD = "загрузка"
STAGE_AUDIO = "аудио→текст"
STAGE_OCR = "текст с экрана (OCR)"
STAGE_VISUAL = "визуальные маркеры"
STAGE_DONE = "готово"


def _emit(progress, stage: str, percent: int) -> None:
    """None-safe вызов progress-колбэка. Ошибки колбэка НЕ ломают разбор (§R3)."""
    if progress is None:
        return
    try:
        progress(stage, percent)
    except Exception:
        # колбэк потребителя не должен валить пайплайн
        pass


def build_combined_text(
    caption: str, transcript: str, ocr_text: str, visual_labels: list
) -> str:
    """Склейка модальностей в фикс. порядке (§0.10): caption, transcript, ocr, labels."""
    parts: list = []
    for p in (caption, transcript, ocr_text):
        p = normalize(p)
        if p:
            parts.append(p)
    parts.extend(label for label in visual_labels if label)
    return " ".join(parts)


def _build_extracted_from_parts(
    post_id: str,
    caption: str,
    transcript: str,
    ocr_text: str,
    visual_concepts: list,
) -> Extracted:
    """Собирает Extracted: combined_text + entity extraction по combined_text."""
    caption = normalize(caption)
    transcript = normalize(transcript)
    ocr_text = normalize(ocr_text)
    combined = build_combined_text(
        caption, transcript, ocr_text, [vc.label for vc in visual_concepts]
    )
    return Extracted(
        post_id=post_id,
        caption=caption,
        transcript=transcript,
        ocr_text=ocr_text,
        visual_concepts=visual_concepts,
        combined_text=combined,
        entities=extract_entities(combined),
    )


def _load_cached(post_id: str) -> "Extracted | None":
    """Сначала on-disk JSON-кэш (без БД, мгновенно), затем таблица extracted."""
    cache_file = os.path.join(str(config.CACHE_DIR), f"{post_id}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                d = json.load(f)
            return Extracted(
                post_id=d["post_id"],
                caption=d["caption"],
                transcript=d["transcript"],
                ocr_text=d["ocr_text"],
                visual_concepts=[VisualConcept(**vc) for vc in d["visual_concepts"]],
                combined_text=d["combined_text"],
                entities=[Entity(**e) for e in d["entities"]],
            )
        except Exception:
            pass
    conn = db.connect()
    try:
        return db.get_extracted(conn, post_id)  # None если в БД нет
    except Exception:
        return None
    finally:
        conn.close()


def _run_live(post: Post, progress=None) -> Extracted:
    """Полный мультимодальный разбор для live-постов (тяжёлый путь, ленивые модели).

    Если у поста есть media_path — РЕАЛЬНО прогоняем кадры/аудио через
    transcribe + ocr_frames + visual_concepts (каждый сам ленив и деградирует в
    ""/[] без тяжёлых библиотек). Без media — fallback на caption-only (text).
    progress опционален и None-safe; этапы сообщаются вокруг каждой модальности.
    """
    if post.media_path:
        _emit(progress, STAGE_DOWNLOAD, 10)
        # Кадры пересэмплируются по media_path (Post их не хранит).
        frames = sample_frames(post.media_path)
        _emit(progress, STAGE_AUDIO, 35)
        transcript = transcribe(post.media_path)
        _emit(progress, STAGE_OCR, 60)
        ocr_text = ocr_frames(frames)
        _emit(progress, STAGE_VISUAL, 85)
        vconcepts = visual_concepts(frames)
    else:
        # Текстовый пост (seed/telegram-preview): только caption, тяжёлые модели
        # НЕ загружаются. Сообщаем те же этапы для единообразного UX.
        _emit(progress, STAGE_DOWNLOAD, 10)
        _emit(progress, STAGE_AUDIO, 35)
        _emit(progress, STAGE_OCR, 60)
        _emit(progress, STAGE_VISUAL, 85)
        transcript = ""
        ocr_text = ""
        vconcepts = []
    return _build_extracted_from_parts(
        post_id=post.id,
        caption=post.caption,
        transcript=transcript,
        ocr_text=ocr_text,
        visual_concepts=vconcepts,
    )


def _call_run_live(post: Post, progress) -> Extracted:
    """Вызывает _run_live, передавая progress ТОЛЬКО если функция его принимает.

    _run_live в тестах может быть подменён на 1-арг lambda(post) — тогда progress
    не передаём (без эвристик по TypeError, чтобы не глотать настоящие ошибки)."""
    fn = _run_live
    try:
        accepts_progress = "progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        accepts_progress = False
    if accepts_progress:
        return fn(post, progress=progress)
    return fn(post)


def _persist(ex: Extracted) -> None:
    """Пишет Extracted в таблицу extracted и в on-disk JSON-кэш."""
    conn = db.connect()
    try:
        db.upsert_extracted(conn, ex)
    finally:
        conn.close()
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(str(config.CACHE_DIR), f"{ex.post_id}.json")
    payload = {
        "post_id": ex.post_id,
        "caption": ex.caption,
        "transcript": ex.transcript,
        "ocr_text": ex.ocr_text,
        "visual_concepts": [
            {"label": vc.label, "score": vc.score} for vc in ex.visual_concepts
        ],
        "combined_text": ex.combined_text,
        "entities": [
            {"type": e.type, "value": e.value, "normalized": e.normalized}
            for e in ex.entities
        ],
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def extract(post: Post, use_cache: bool = True, progress=None) -> Extracted:
    """Главная точка входа. Кэш -> мгновенно; иначе полный разбор + персист.

    progress(stage:str, percent:int) — опциональный колбэк для UI/прогресс-бара.
    Этапы: 'кэш' -> ('загрузка','аудио→текст','текст с экрана (OCR)',
    'визуальные маркеры') -> 'готово'. None-safe.
    """
    _emit(progress, STAGE_CACHE, 0)
    if use_cache:
        cached = _load_cached(post.id)
        if cached is not None:
            # Кэш-хит: сразу 'готово', тяжёлые модели не трогаем.
            _emit(progress, STAGE_DONE, 100)
            return cached
    ex = _call_run_live(post, progress)
    _persist(ex)
    _emit(progress, STAGE_DONE, 100)
    return ex
