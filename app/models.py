"""Канонические dataclass-контракты КӨЗ (§0.2).

Точные имена полей и порядок — единственный источник истины для всех фич.
В AuditEntry поле с дефолтом (actor) стоит ПОСЛЕ полей без дефолта, поэтому
порядок: ts, post_id, action, detail, actor.
"""

from dataclasses import dataclass


@dataclass
class Entity:
    type: str
    value: str
    normalized: str


@dataclass
class VisualConcept:
    label: str
    score: float


@dataclass
class Extracted:
    post_id: str
    caption: str
    transcript: str
    ocr_text: str
    visual_concepts: list[VisualConcept]
    combined_text: str
    entities: list[Entity]


@dataclass
class Post:
    id: str
    platform: str
    author_handle: str
    url: str
    caption: str
    posted_at: str
    media_path: str | None
    thumb_url: str | None
    source: str
    # популярность поста (просмотры). Единственное поле с дефолтом — стоит ПОСЛЕДНИМ,
    # чтобы dataclass оставался валидным (поля без дефолта идут раньше).
    view_count: int = 0


@dataclass
class FeatureHit:
    feature: str
    weight: float
    evidence: str


@dataclass
class Score:
    post_id: str
    risk: int
    category: str
    class_probs: dict[str, float]
    top_features: list[FeatureHit]


@dataclass
class Edge:
    source: str
    target: str
    type: str
    weight: float


@dataclass
class AuditEntry:
    ts: str
    post_id: str
    action: str
    detail: str
    actor: str = "system"
