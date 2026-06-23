"""Построение графа связей пост<->сущность (F5).

Из уже извлечённых сущностей (F2, таблица `extracted`) и риск-скоров (F3,
таблица `scores`) строится граф: узлы-посты + узлы-сущности, рёбра
`post -> entity` (type="contains"). Сущность, общая для нескольких постов,
становится единым узлом, связывая их в одну компоненту — так выявляются
координированные сети аккаунтов и реф-ринги.

Контракт (§0.7 — согласованное расширение):
  build_graph(post_ids, conn=None) -> {
    "nodes": [{id, label, type, risk, high_risk}],
    "edges": [{source, target, type, weight}],
  }
  build_ego_graph(post_id, conn=None) -> то же для эго-сети поста.

Схема id:
  пост      -> "post:<post_id>"
  сущность  -> "entity:<type>:<normalized>"

Соединение БД: в standalone-вызове (conn=None) открываем своё через
db.connect() и закрываем в finally (§0.2); если conn передан (роут/тест) —
используем его и НЕ закрываем.
"""

import json

from app import config, db


def _post_node_id(post_id: str) -> str:
    return f"post:{post_id}"


def _entity_node_id(entity: dict) -> str:
    return f"entity:{entity.get('type', '?')}:{entity.get('normalized') or entity.get('value', '')}"


def _entities_for(conn, post_id: str) -> list[dict]:
    """Список сущностей поста из extracted.entities_json (пустой при отсутствии)."""
    row = conn.execute(
        "SELECT entities_json FROM extracted WHERE post_id = ?", (post_id,)
    ).fetchone()
    if row is None or not row["entities_json"]:
        return []
    return json.loads(row["entities_json"])


def _build_graph_with_conn(conn, post_ids: list[str]) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for post_id in post_ids:
        row = conn.execute(
            "SELECT entities_json FROM extracted WHERE post_id = ?", (post_id,)
        ).fetchone()
        if row is None:
            # нет извлечённых данных для поста — пропускаем без падения
            continue

        score_row = conn.execute(
            "SELECT risk FROM scores WHERE post_id = ?", (post_id,)
        ).fetchone()
        risk = int(score_row["risk"]) if score_row is not None else 0

        pid = _post_node_id(post_id)
        nodes[pid] = {"id": pid, "label": post_id, "type": "post", "risk": risk}

        entities = json.loads(row["entities_json"]) if row["entities_json"] else []
        for ent in entities:
            if not isinstance(ent, dict) or not ent.get("type"):
                continue  # пропускаем малформ-записи без падения (как в trends.py)
            eid = _entity_node_id(ent)
            if eid not in nodes:
                nodes[eid] = {
                    "id": eid,
                    "label": ent.get("value") or ent.get("normalized") or "",
                    "type": ent.get("type"),
                    "risk": 0,
                }
            # узел-сущность наследует макс. риск инцидентных постов (кластер)
            nodes[eid]["risk"] = max(nodes[eid]["risk"], risk)
            edges.append(
                {"source": pid, "target": eid, "type": "contains", "weight": 1.0}
            )

    for node in nodes.values():
        node["high_risk"] = node["risk"] >= config.ESCALATE_THRESHOLD

    return {"nodes": list(nodes.values()), "edges": edges}


def build_graph(post_ids: list[str], conn=None) -> dict:
    """Граф пост<->сущность для заданных постов.

    Узлы-посты несут risk (из scores, 0 если скора нет), узлы-сущности —
    макс. риск инцидентных постов и флаг high_risk (>= ESCALATE_THRESHOLD).
    Рёбра post->entity type="contains", weight=1.0. Узлы-сущности
    дедуплицируются по id, поэтому общая сущность связывает посты.
    """
    if conn is not None:
        return _build_graph_with_conn(conn, post_ids)
    own = db.connect()
    try:
        return _build_graph_with_conn(own, post_ids)
    finally:
        own.close()


def _build_ego_graph_with_conn(conn, post_id: str) -> dict:
    ego_entity_ids = {_entity_node_id(e) for e in _entities_for(conn, post_id)}

    all_post_ids = [
        r["post_id"] for r in conn.execute("SELECT post_id FROM extracted").fetchall()
    ]

    co_post_ids = {post_id}
    for other in all_post_ids:
        if other == post_id:
            continue
        other_ids = {_entity_node_id(e) for e in _entities_for(conn, other)}
        if ego_entity_ids & other_ids:
            co_post_ids.add(other)

    return _build_graph_with_conn(conn, sorted(co_post_ids))


def build_ego_graph(post_id: str, conn=None) -> dict:
    """Эго-сеть поста: сам пост, его сущности и посты, делящие хотя бы одну
    сущность с ним (co-posts). Посторонние посты/сущности исключены.
    """
    if conn is not None:
        return _build_ego_graph_with_conn(conn, post_id)
    own = db.connect()
    try:
        return _build_ego_graph_with_conn(own, post_id)
    finally:
        own.close()
