"""HTTP-роут графа связей (F5) — подключается авто-роутером main.py (A2).

GET /api/graph?post_id=&min_risk=
  - post_id задан      -> эго-сеть этого поста (его сущности + co-posts);
  - post_id отсутствует -> весь граф revealed-постов с риском >= min_risk.

Соединение БД берётся из request.app.state.db (одно соединение из lifespan,
§0.2); функции построения вызываются с conn=этим соединением (не закрываем).
"""

from fastapi import APIRouter, Request

from app.graph.build import build_ego_graph, build_graph

router = APIRouter()


@router.get("/api/graph")
def api_graph(request: Request, post_id: "str | None" = None, min_risk: int = 0):
    conn = request.app.state.db

    if post_id:
        return build_ego_graph(post_id, conn=conn)

    rows = conn.execute(
        "SELECT s.post_id FROM scores s "
        "JOIN posts p ON p.id = s.post_id "
        "WHERE p.revealed = 1 AND s.risk >= ? "
        "ORDER BY s.risk DESC",
        (min_risk,),
    ).fetchall()
    return build_graph([r["post_id"] for r in rows], conn=conn)
