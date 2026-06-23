"""F6 — Роут отдачи PDF-досье (авто-роутер, A2/R2).

Модуль-уровневый `router` авто-подключается в app/main.py
(pkgutil.walk_packages по app/**/routes.py) — main.py НЕ правим.
Соединение берём из request.app.state.db (одно из lifespan).
"""

from fastapi import APIRouter, HTTPException, Request, Response

from app.report.pdf import build_case_pdf

router = APIRouter()


@router.get("/api/report/{post_id}.pdf")
def get_report_pdf(post_id: str, request: Request) -> Response:
    """Отдать PDF-досье по post_id; 404 — если материал не найден."""
    conn = request.app.state.db
    try:
        pdf_bytes = build_case_pdf(post_id, conn=conn)
    except KeyError:
        raise HTTPException(status_code=404, detail="Материал не найден")
    headers = {
        "Content-Disposition": f'inline; filename="koz_dossier_{post_id}.pdf"',
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
