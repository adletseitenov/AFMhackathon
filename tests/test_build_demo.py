"""D1 — тесты scripts/build_demo: demo-записи с предвычисленными extracted."""

from scripts.build_demo import build_demo_records


def test_records_span_platforms_and_have_extracted():
    recs = build_demo_records()
    assert len(recs) >= 14
    platforms = {r["post"]["platform"] for r in recs}
    assert {"tiktok", "instagram", "youtube"}.issubset(platforms)
    for r in recs:
        assert set(r["post"].keys()) >= {
            "id", "platform", "author_handle", "url", "caption", "posted_at", "source"
        }
        ex = r["extracted"]
        assert set(ex.keys()) == {
            "post_id", "caption", "transcript", "ocr_text",
            "visual_concepts", "combined_text", "entities",
        }
        assert ex["post_id"] == r["post"]["id"]


def test_includes_high_risk_examples():
    recs = build_demo_records()
    cats = " ".join(r["extracted"]["combined_text"].lower() for r in recs)
    assert "1xbet" in cats or "казино" in cats
    assert "%" in cats  # обещания дохода


def test_includes_clean_examples():
    # Должны быть «чистые» примеры (для приоритетной очереди и метрик).
    recs = build_demo_records()
    captions = " ".join(r["post"]["caption"].lower() for r in recs)
    # хотя бы один пост без признаков риска: ни казино, ни процентов дохода
    clean_like = [
        r for r in recs
        if "%" not in r["extracted"]["combined_text"]
        and not any(
            b in r["extracted"]["combined_text"].lower()
            for b in ("1xbet", "mostbet", "казино", "ставк")
        )
    ]
    assert len(clean_like) >= 1
    assert captions  # непустые кэпшны


def test_unique_post_ids():
    recs = build_demo_records()
    ids = [r["post"]["id"] for r in recs]
    assert len(ids) == len(set(ids))


def test_entities_populated_for_high_risk():
    # Хотя бы у некоторых записей extract_entities нашёл сущности.
    recs = build_demo_records()
    total_entities = sum(len(r["extracted"]["entities"]) for r in recs)
    assert total_entities >= 3
    # формат сущности — dict с ключами type/value/normalized
    for r in recs:
        for e in r["extracted"]["entities"]:
            assert set(e.keys()) == {"type", "value", "normalized"}


def test_combined_text_matches_canonical_join():
    # §0.10: combined_text = join(filter(None,[caption,transcript,ocr,visual labels])).
    recs = build_demo_records()
    for r in recs:
        ex = r["extracted"]
        labels = " ".join(vc["label"] for vc in ex["visual_concepts"])
        parts = [ex["caption"], ex["transcript"], ex["ocr_text"], labels]
        expected = "\n".join(p for p in parts if p)
        assert ex["combined_text"] == expected
