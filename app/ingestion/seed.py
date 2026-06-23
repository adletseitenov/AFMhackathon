"""D2 — seed-загрузчик КӨЗ: demo-посты в БД + кэш extracted + первичный скоринг.

`load_seed(conn)` идемпотентно загружает демо-датасет (config.DEMO_POSTS_PATH или
fallback на scripts.build_demo.build_demo_records()), для каждой записи:
  1) вставляет Post (`db.insert_post`) — стартует скрытым (revealed=0, drip-reveal);
  2) кэширует предвычисленный Extracted (`db.upsert_extracted`);
  3) скорит собственной моделью через `score_post` (он сам upsert'ит scores + audit),
     иначе INNER JOIN в /api/feed отбросит пост (§0.8).

Вызывается из lifespan F0 при пустой БД. Возвращает число загруженных постов;
если posts уже непустой — возвращает 0 без дублирования (идемпотентность).
"""

import json

from app import config, db
from app.decision import scoring
from app.models import Entity, Extracted, Post, VisualConcept


def _load_records() -> list[dict]:
    """Читает config.DEMO_POSTS_PATH (JSONL); при отсутствии — строит на лету."""
    path = config.DEMO_POSTS_PATH
    if path.exists():
        records: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        if records:
            return records
    # Файла нет (или пуст) — генерируем записи через D1.
    from scripts.build_demo import build_demo_records

    return build_demo_records()


def _post_from_dict(d: dict) -> Post:
    return Post(
        id=d["id"],
        platform=d["platform"],
        author_handle=d["author_handle"],
        url=d["url"],
        caption=d["caption"],
        posted_at=d["posted_at"],
        media_path=d.get("media_path"),
        thumb_url=d.get("thumb_url"),
        source=d.get("source", "seed"),
    )


def _extracted_from_dict(d: dict) -> Extracted:
    return Extracted(
        post_id=d["post_id"],
        caption=d["caption"],
        transcript=d["transcript"],
        ocr_text=d["ocr_text"],
        visual_concepts=[VisualConcept(**vc) for vc in d.get("visual_concepts", [])],
        combined_text=d["combined_text"],
        entities=[Entity(**e) for e in d.get("entities", [])],
    )


def load_seed(conn) -> int:
    """Идемпотентно загрузить + закэшировать + скорить demo-посты. Вернуть число."""
    existing = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    if existing:
        return 0

    records = _load_records()
    count = 0
    for rec in records:
        post = _post_from_dict(rec["post"])
        ex = _extracted_from_dict(rec["extracted"])
        db.insert_post(conn, post)          # revealed=0 по умолчанию (drip-reveal)
        db.upsert_extracted(conn, ex)
        scoring.score_post(post, ex, conn=conn)  # upsert scores + audit (§0.8)
        count += 1
    return count
