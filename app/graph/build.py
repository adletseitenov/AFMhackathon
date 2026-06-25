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
from app.decision.licensed import licensed_operators


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

    # ПРОИЗВОДИТЕЛЬНОСТЬ: раньше на КАЖДЫЙ пост шло 3 отдельных запроса (extracted,
    # scores, posts) — 3×N round-trips. Тянем всё ТРЕМЯ bulk-запросами по списку
    # post_ids и кладём в dict-ы; дальше — только память. Результат идентичен.
    if not post_ids:
        return {"nodes": [], "edges": []}
    qmarks = ",".join("?" for _ in post_ids)
    ex_by_id = {
        r["post_id"]: r for r in conn.execute(
            f"SELECT post_id, entities_json, combined_text FROM extracted "
            f"WHERE post_id IN ({qmarks})", tuple(post_ids)
        ).fetchall()
    }
    risk_by_id = {
        r["post_id"]: int(r["risk"]) for r in conn.execute(
            f"SELECT post_id, risk FROM scores WHERE post_id IN ({qmarks})", tuple(post_ids)
        ).fetchall()
    }
    post_by_id = {
        r["id"]: r for r in conn.execute(
            f"SELECT id, caption, author_handle FROM posts WHERE id IN ({qmarks})", tuple(post_ids)
        ).fetchall()
    }

    for post_id in post_ids:
        row = ex_by_id.get(post_id)
        if row is None:
            # нет извлечённых данных для поста — пропускаем без падения
            continue

        risk = risk_by_id.get(post_id, 0)

        # Флаг «разрешён в РК»: лицензированный оператор по подписи + аккаунту + тексту
        # (оператор часто = сам аккаунт, напр. @olimpbet). Риск НЕ меняем — только метка.
        prow = post_by_id.get(post_id)
        lic_text = " ".join(
            x for x in (
                (prow["caption"] if prow else "") or "",
                (prow["author_handle"] if prow else "") or "",
                row["combined_text"] or "",
            ) if x
        )
        lic_ops = licensed_operators(lic_text)

        pid = _post_node_id(post_id)
        nodes[pid] = {"id": pid, "label": post_id, "type": "post", "risk": risk,
                      "licensed": bool(lic_ops), "licensed_operators": lic_ops}

        entities = json.loads(row["entities_json"]) if row["entities_json"] else []
        for ent in entities:
            if not isinstance(ent, dict) or not ent.get("type"):
                continue  # пропускаем малформ-записи без падения (как в trends.py)
            eid = _entity_node_id(ent)
            if eid not in nodes:
                ent_label = ent.get("value") or ent.get("normalized") or ""
                ent_lic = licensed_operators(ent_label)
                nodes[eid] = {
                    "id": eid,
                    "label": ent_label,
                    "type": ent.get("type"),
                    "risk": 0,
                    "licensed": bool(ent_lic),
                    "licensed_operators": ent_lic,
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
    # ПРОИЗВОДИТЕЛЬНОСТЬ: раньше это был классический N+1 — SELECT всех post_id, затем
    # отдельный запрос _entities_for() на КАЖДЫЙ пост (~1107 запросов + 1107 json.loads
    # на каждое открытие карточки). Заменяем на ОДИН bulk-запрос + инвертированный
    # индекс entity_id -> {post_id} в памяти (один проход). Результат идентичен:
    # co-пост делит >=1 сущность с эго-постом <=> встречается в индексе хотя бы одной
    # его сущности. Малформ-записи пропускаем РОВНО как _build_graph_with_conn (ниже).
    rows = conn.execute("SELECT post_id, entities_json FROM extracted").fetchall()

    entity_to_posts: dict[str, set[str]] = {}
    ego_entity_ids: set[str] = set()
    for r in rows:
        try:
            ents = json.loads(r["entities_json"]) if r["entities_json"] else []
        except (TypeError, ValueError):
            ents = []
        ids = set()
        for ent in ents:
            if not isinstance(ent, dict) or not ent.get("type"):
                continue  # пропускаем малформ (как lines 86-87 ниже) — ключ узла не плывёт
            ids.add(_entity_node_id(ent))
        if r["post_id"] == post_id:
            ego_entity_ids = ids
        for eid in ids:
            entity_to_posts.setdefault(eid, set()).add(r["post_id"])

    co_post_ids = {post_id}
    for eid in ego_entity_ids:
        co_post_ids |= entity_to_posts.get(eid, set())

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
