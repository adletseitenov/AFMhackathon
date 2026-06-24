from dataclasses import fields

from app.models import (
    AuditEntry,
    Edge,
    Entity,
    Extracted,
    FeatureHit,
    Post,
    Score,
    VisualConcept,
)


def _names(cls):
    return [f.name for f in fields(cls)]


def test_entity_fields():
    assert _names(Entity) == ["type", "value", "normalized"]


def test_visual_concept_fields():
    assert _names(VisualConcept) == ["label", "score"]


def test_extracted_fields():
    assert _names(Extracted) == [
        "post_id", "caption", "transcript", "ocr_text",
        "visual_concepts", "combined_text", "entities",
    ]


def test_post_fields():
    # view_count + live — поля с дефолтом, идут последними (dataclass валиден).
    assert _names(Post) == [
        "id", "platform", "author_handle", "url", "caption",
        "posted_at", "media_path", "thumb_url", "source", "view_count", "live",
    ]


def test_post_view_count_defaults_to_zero():
    p = Post(
        id="p1", platform="tiktok", author_handle="@x", url="http://u",
        caption="c", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="seed",
    )
    assert p.view_count == 0
    p2 = Post(
        id="p2", platform="tiktok", author_handle="@x", url="http://u",
        caption="c", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="seed", view_count=4200,
    )
    assert p2.view_count == 4200


def test_feature_hit_fields():
    assert _names(FeatureHit) == ["feature", "weight", "evidence"]


def test_score_fields():
    assert _names(Score) == [
        "post_id", "risk", "category", "class_probs", "top_features",
    ]


def test_edge_fields():
    assert _names(Edge) == ["source", "target", "type", "weight"]


def test_audit_entry_defaults():
    assert _names(AuditEntry) == ["ts", "post_id", "action", "detail", "actor"]
    e = AuditEntry(ts="t", post_id="p", action="scored", detail="d")
    assert e.actor == "system"


def test_construct_full_objects():
    ent = Entity(type="casino_brand", value="1xBet", normalized="1xbet")
    vc = VisualConcept(label="roulette", score=0.9)
    ex = Extracted(
        post_id="p1", caption="c", transcript="t", ocr_text="o",
        visual_concepts=[vc], combined_text="c t o", entities=[ent],
    )
    assert ex.entities[0].normalized == "1xbet"
    sc = Score(
        post_id="p1", risk=80, category="gambling",
        class_probs={"gambling": 0.8},
        top_features=[FeatureHit(feature="payout", weight=0.5, evidence="30%")],
    )
    assert sc.risk == 80
