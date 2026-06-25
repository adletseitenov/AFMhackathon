"""Тесты построения графа связей (F5) — build_graph / build_ego_graph.

БД изолируется через monkeypatch config.DB_PATH на временный файл (§0.2);
посты/extracted/scores пишутся прямыми INSERT'ами (фикстуры F5 не зависят
от F2/F3 — пишут в extracted/scores напрямую).
"""

import json

import pytest

from app import config, db
from app.graph.build import build_graph, build_ego_graph


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Чистая временная БД; возвращает открытое соединение."""
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", dbfile)
    conn = db.connect()
    db.init_db(conn)
    yield conn
    conn.close()


def _insert_post(conn, post_id, revealed=1):
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, "
        "media_path, thumb_url, source, revealed) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (post_id, "telegram", "@" + post_id, "https://t.me/x", "cap",
         "2026-06-24T10:00:00", None, None, "telegram", revealed),
    )
    conn.commit()


def _insert_extracted(conn, post_id, entities):
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) VALUES(?,?,?,?,?,?,?)",
        (post_id, "cap", "", "", "[]", "cap", json.dumps(entities)),
    )
    conn.commit()


def _insert_score(conn, post_id, risk, category="gambling"):
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) VALUES(?,?,?,?,?,?,?)",
        (post_id, risk, category, "{}", "[]", "review", "2026-06-24T10:00:00"),
    )
    conn.commit()


# --- Задача 1: узлы и рёбра одного поста (контракт node id / Edge) ---

def test_single_post_two_entities_node_and_edge_shape(fresh_db):
    conn = fresh_db
    _insert_post(conn, "p1")
    _insert_score(conn, "p1", 80)
    _insert_extracted(conn, "p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
        {"type": "casino_brand", "value": "1xBet", "normalized": "1xbet"},
    ])

    graph = build_graph(["p1"], conn=conn)

    node_ids = {n["id"] for n in graph["nodes"]}
    assert "post:p1" in node_ids
    assert "entity:telegram:casinox" in node_ids
    assert "entity:casino_brand:1xbet" in node_ids

    post_node = next(n for n in graph["nodes"] if n["id"] == "post:p1")
    assert post_node["type"] == "post"
    assert post_node["risk"] == 80
    assert post_node["label"] == "p1"

    assert len(graph["edges"]) == 2
    e = graph["edges"][0]
    assert set(e.keys()) == {"source", "target", "type", "weight"}
    assert e["source"] == "post:p1"
    assert e["target"].startswith("entity:")
    assert e["type"] == "contains"
    assert e["weight"] == 1.0


# --- Задача 2: общая сущность связывает два поста в одну компоненту ---

def test_shared_telegram_entity_links_two_posts(fresh_db):
    conn = fresh_db
    _insert_post(conn, "p1")
    _insert_score(conn, "p1", 80)
    _insert_extracted(conn, "p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
    ])
    _insert_post(conn, "p2")
    _insert_score(conn, "p2", 65)
    _insert_extracted(conn, "p2", [
        {"type": "telegram", "value": "@casinox", "normalized": "casinox"},
    ])

    graph = build_graph(["p1", "p2"], conn=conn)

    # Один общий узел-сущность для двух постов
    entity_nodes = [n for n in graph["nodes"] if n["type"] == "telegram"]
    assert len(entity_nodes) == 1
    shared_id = entity_nodes[0]["id"]
    assert shared_id == "entity:telegram:casinox"

    # Оба поста имеют ребро к общему узлу => связная компонента
    sources_to_shared = {e["source"] for e in graph["edges"] if e["target"] == shared_id}
    assert sources_to_shared == {"post:p1", "post:p2"}

    # Два поста + один общий узел-сущность = 3 узла, 2 ребра
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 2


# --- Задача 3: эго-сеть для поста (его сущности + co-posts) ---

def test_ego_graph_returns_entities_and_co_posts(fresh_db):
    conn = fresh_db
    # p1 и p2 делят telegram:casinox; p3 не связан
    _insert_post(conn, "p1"); _insert_score(conn, "p1", 80)
    _insert_extracted(conn, "p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
        {"type": "promo_code", "value": "WIN100", "normalized": "win100"},
    ])
    _insert_post(conn, "p2"); _insert_score(conn, "p2", 70)
    _insert_extracted(conn, "p2", [
        {"type": "telegram", "value": "@casinox", "normalized": "casinox"},
    ])
    _insert_post(conn, "p3"); _insert_score(conn, "p3", 10)
    _insert_extracted(conn, "p3", [
        {"type": "casino_brand", "value": "Other", "normalized": "other"},
    ])

    graph = build_ego_graph("p1", conn=conn)
    node_ids = {n["id"] for n in graph["nodes"]}

    # сам пост + его сущности
    assert "post:p1" in node_ids
    assert "entity:telegram:casinox" in node_ids
    assert "entity:promo_code:win100" in node_ids
    # co-post через общую сущность
    assert "post:p2" in node_ids
    # несвязанный пост и его сущность исключены
    assert "post:p3" not in node_ids
    assert "entity:casino_brand:other" not in node_ids


# --- Задача 4: флаг кластеров высокого риска ---

def test_entity_node_risk_is_max_incident_post_and_flags_high_risk(fresh_db):
    conn = fresh_db
    _insert_post(conn, "p1"); _insert_score(conn, "p1", 80)
    _insert_extracted(conn, "p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
    ])
    _insert_post(conn, "p2"); _insert_score(conn, "p2", 65)
    _insert_extracted(conn, "p2", [
        {"type": "telegram", "value": "@casinox", "normalized": "casinox"},
    ])

    graph = build_graph(["p1", "p2"], conn=conn)
    ent = next(n for n in graph["nodes"] if n["id"] == "entity:telegram:casinox")
    assert ent["risk"] == 80          # макс. риска инцидентных постов
    assert ent["high_risk"] is True   # >= ESCALATE_THRESHOLD (70)

    p2 = next(n for n in graph["nodes"] if n["id"] == "post:p2")
    assert p2["high_risk"] is False   # 65 < 70


# --- Перф-оптимизация: эго-граф через инвертированный индекс == брутфорс-эталон ---

def _bruteforce_ego_copost_ids(conn, post_id):
    """Эталон: попарное пересечение множеств сущностей (старый алгоритм N+1)."""
    import json as _json

    def ent_ids(pid):
        row = conn.execute(
            "SELECT entities_json FROM extracted WHERE post_id=?", (pid,)
        ).fetchone()
        if row is None or not row["entities_json"]:
            return set()
        out = set()
        for e in _json.loads(row["entities_json"]):
            if isinstance(e, dict) and e.get("type"):
                out.add(f"entity:{e.get('type')}:{e.get('normalized') or e.get('value','')}")
        return out

    ego = ent_ids(post_id)
    all_ids = [r["post_id"] for r in conn.execute("SELECT post_id FROM extracted").fetchall()]
    co = {post_id}
    for other in all_ids:
        if other != post_id and (ego & ent_ids(other)):
            co.add(other)
    return co


def test_ego_inverted_index_matches_bruteforce_parity(fresh_db):
    """Оптимизированный build_ego_graph (bulk-запрос + инвертированный индекс) даёт
    тот же набор узлов/рёбер, что и попарное пересечение, и на МАЛФОРМ-сущностях."""
    conn = fresh_db
    # сеть из общих сущностей + малформ-записи (None/без type) — должны игнорироваться.
    _insert_post(conn, "a"); _insert_score(conn, "a", 80)
    _insert_extracted(conn, "a", [
        {"type": "telegram", "value": "@Net", "normalized": "net"},
        {"type": "promo_code", "value": "X1", "normalized": "x1"},
        {"value": "no-type-skip"},          # малформ: без type -> игнор
        "broken-not-a-dict",                  # малформ: не dict -> игнор
    ])
    _insert_post(conn, "b"); _insert_score(conn, "b", 70)
    _insert_extracted(conn, "b", [{"type": "telegram", "value": "@net", "normalized": "net"}])
    _insert_post(conn, "c"); _insert_score(conn, "c", 50)
    _insert_extracted(conn, "c", [{"type": "promo_code", "value": "X1", "normalized": "x1"}])
    _insert_post(conn, "d"); _insert_score(conn, "d", 10)
    _insert_extracted(conn, "d", [{"type": "casino_brand", "value": "Other", "normalized": "other"}])

    graph = build_ego_graph("a", conn=conn)
    post_nodes = {n["id"] for n in graph["nodes"] if n["type"] == "post"}
    expected = {f"post:{p}" for p in _bruteforce_ego_copost_ids(conn, "a")}
    assert post_nodes == expected            # b и c связаны, d исключён
    assert "post:d" not in post_nodes
    # малформ-сущности не создают узлов
    assert all("no-type-skip" not in n["id"] and "broken" not in n["id"] for n in graph["nodes"])


# --- conn=None: функция открывает своё соединение (standalone) ---

def test_build_graph_opens_own_connection_when_conn_none(fresh_db):
    # fresh_db уже примонкипатчил config.DB_PATH -> временный файл
    conn = fresh_db
    _insert_post(conn, "p1"); _insert_score(conn, "p1", 90)
    _insert_extracted(conn, "p1", [
        {"type": "telegram", "value": "@X", "normalized": "x"},
    ])

    # conn не передаём — build_graph должен открыть своё соединение к config.DB_PATH
    graph = build_graph(["p1"])
    node_ids = {n["id"] for n in graph["nodes"]}
    assert "post:p1" in node_ids
    assert "entity:telegram:x" in node_ids


def test_missing_post_is_skipped(fresh_db):
    conn = fresh_db
    # пост без extracted-строки -> пропускается, без падения
    graph = build_graph(["ghost"], conn=conn)
    assert graph["nodes"] == []
    assert graph["edges"] == []


def test_post_without_score_defaults_risk_zero(fresh_db):
    conn = fresh_db
    _insert_post(conn, "p1")
    _insert_extracted(conn, "p1", [
        {"type": "telegram", "value": "@X", "normalized": "x"},
    ])
    # без scores-строки
    graph = build_graph(["p1"], conn=conn)
    post_node = next(n for n in graph["nodes"] if n["id"] == "post:p1")
    assert post_node["risk"] == 0
    assert post_node["high_risk"] is False


# --- Флаг «разрешён в РК» (лицензированный оператор) на узлах графа ---

def test_licensed_operator_marked_on_post_and_entity_nodes(fresh_db):
    conn = fresh_db
    # пост от ЛИЦЕНЗИРОВАННОГО аккаунта (@olimpbet) + entity-бренд Olimpbet
    _insert_post(conn, "olimpbet")  # author_handle="@olimpbet"
    _insert_score(conn, "olimpbet", 80)
    _insert_extracted(conn, "olimpbet",
                      [{"type": "betting_brand", "value": "Olimpbet", "normalized": "olimpbet"}])
    # НЕлицензированный пост для контраста
    _insert_post(conn, "shill1")
    _insert_score(conn, "shill1", 90)
    _insert_extracted(conn, "shill1",
                      [{"type": "betting_brand", "value": "mostbet", "normalized": "mostbet"}])

    g = build_graph(["olimpbet", "shill1"], conn=conn)
    by_label = {n["label"]: n for n in g["nodes"]}

    # post-узел лицензированного аккаунта помечен
    assert by_label["olimpbet"]["licensed"] is True
    assert "Olimpbet" in by_label["olimpbet"]["licensed_operators"]
    # entity-бренд Olimpbet помечен, mostbet — нет
    assert by_label["Olimpbet"]["licensed"] is True
    assert by_label["mostbet"]["licensed"] is False
    # риск НЕ занижается флагом
    assert by_label["olimpbet"]["risk"] == 80
