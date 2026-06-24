"""F6 — Сборка официального PDF-досье КӨЗ по флагнутому посту.

`build_case_pdf(post_id, conn=None) -> bytes`:
  - читает пост / extracted / score из БД канонI (db.get_post / db.get_extracted /
    db.get_score_row); если conn не передан — открывает своё через db.connect()
    и закрывает в finally (§0.2: standalone-функция);
  - переносит recommended_action из строки scores (его НЕТ в dataclass Score);
  - верстает через reportlab: шапка КӨЗ/АФМ + дата, метаданные поста, риск/
    категория/действие, доказательства по модальностям, русские буллеты-объяснения
    (app.decision.explain.explain), футер human-in-the-loop;
  - KeyError(post_id), если поста / extracted / score нет.

Кириллица: регистрируем TTF (arial/segoeui/DejaVuSans), иначе Helvetica
(может не отрисовать кириллицу — но PDF всё равно валиден, тест на %PDF проходит).
Не требует torch/whisper/ocr/clip — читает только из БД.
"""

import json
import os
from datetime import datetime
from io import BytesIO

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from app import db
from app.decision.explain import explain
from app.models import FeatureHit, Score

# --- Русские лейблы (для отображения в досье) ---
ACTION_LABELS_RU = {
    "escalate": "Эскалация",
    "review": "На проверку",
    "auto_clear": "Авто-очистка",
}
CATEGORY_LABELS_RU = {
    "gambling": "Гемблинг",
    "pyramid": "Финансовая пирамида",
    "fraud": "Мошенничество / реф-схема",
    "clean": "Чисто",
}

_FOOTER_RU = "Сформировано системой КӨЗ — требует проверки аналитиком (human-in-the-loop)."
_FONT = "Helvetica"


def action_label_ru(action: str) -> str:
    return ACTION_LABELS_RU.get(action, action)


def category_label_ru(category: str) -> str:
    return CATEGORY_LABELS_RU.get(category, category)


def _register_font() -> None:
    """Зарегистрировать TTF с кириллицей один раз (idempotent)."""
    global _FONT
    if _FONT != "Helvetica":
        return
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("KozBody", path))
                _FONT = "KozBody"
                return
            except Exception:
                continue


def _score_from_row(row) -> Score:
    """Восстановить Score из строки scores (recommended_action вешаем атрибутом)."""
    top_features = [
        FeatureHit(**f) for f in json.loads(row["top_features_json"] or "[]")
    ]
    score = Score(
        post_id=row["post_id"],
        risk=row["risk"],
        category=row["category"],
        class_probs=json.loads(row["class_probs_json"] or "{}"),
        top_features=top_features,
    )
    # recommended_action отсутствует в dataclass Score — несём из строки scores.
    score.recommended_action = row["recommended_action"]
    return score


def _esc(value) -> str:
    """HTML-экранирование для reportlab Paragraph (мини-разметка)."""
    s = "" if value is None else str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_case_pdf(post_id: str, conn=None) -> bytes:
    """Собрать PDF-досье по post_id и вернуть байты. KeyError, если дела нет."""
    own_conn = conn is None
    if own_conn:
        conn = db.connect()
    try:
        post = db.get_post(conn, post_id)
        extracted = db.get_extracted(conn, post_id)
        srow = db.get_score_row(conn, post_id)
    finally:
        if own_conn:
            conn.close()

    if post is None or extracted is None or srow is None:
        raise KeyError(post_id)

    score = _score_from_row(srow)
    _register_font()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        title=f"Досье КӨЗ — {post_id}",
    )
    base = getSampleStyleSheet()
    h_title = ParagraphStyle(
        "KozTitle", parent=base["Title"], fontName=_FONT, fontSize=18, alignment=TA_CENTER
    )
    h_sub = ParagraphStyle(
        "KozSub", parent=base["Normal"], fontName=_FONT, fontSize=10,
        alignment=TA_CENTER, textColor="#555555",
    )
    h_section = ParagraphStyle(
        "KozSection", parent=base["Heading2"], fontName=_FONT, fontSize=13
    )
    body = ParagraphStyle(
        "KozBodyStyle", parent=base["Normal"], fontName=_FONT, fontSize=10, leading=14
    )

    story = []

    # --- Шапка ---
    story.append(Paragraph("КӨЗ · АФМ РК", h_title))
    story.append(Paragraph("Досье по материалу · AI Media Watch", h_sub))
    story.append(
        Paragraph(f"Дата формирования: {datetime.now().strftime('%Y-%m-%d %H:%M')}", h_sub)
    )
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1, color="#222222"))
    story.append(Spacer(1, 10))

    # --- Метаданные поста ---
    story.append(Paragraph("Материал", h_section))
    story.append(Paragraph(f"<b>Платформа:</b> {_esc(post.platform)}", body))
    story.append(Paragraph(f"<b>Аккаунт:</b> {_esc(post.author_handle)}", body))
    story.append(Paragraph(f"<b>Ссылка:</b> {_esc(post.url)}", body))
    story.append(Paragraph(f"<b>Опубликовано:</b> {_esc(post.posted_at)}", body))
    story.append(Spacer(1, 8))

    # --- Риск-оценка ---
    story.append(Paragraph("Риск-оценка", h_section))
    action = getattr(score, "recommended_action", "") or ""
    story.append(Paragraph(f"<b>Риск:</b> {_esc(score.risk)} / 100", body))
    story.append(
        Paragraph(f"<b>Категория:</b> {_esc(category_label_ru(score.category))}", body)
    )
    story.append(
        Paragraph(
            f"<b>Рекомендованное действие:</b> {_esc(action_label_ru(action))}", body
        )
    )
    story.append(Spacer(1, 8))

    # --- Доказательства ---
    story.append(Paragraph("Доказательства", h_section))
    if extracted.caption:
        story.append(Paragraph(f"<b>Подпись:</b> {_esc(extracted.caption)}", body))
    if extracted.transcript:
        excerpt = extracted.transcript[:600]
        story.append(
            Paragraph(f"<b>Транскрипт (фрагмент):</b> {_esc(excerpt)}", body)
        )
    if extracted.ocr_text:
        story.append(
            Paragraph(f"<b>Текст с экрана (OCR):</b> {_esc(extracted.ocr_text)}", body)
        )
    if extracted.visual_concepts:
        vc = ", ".join(
            f"{_esc(c.label)} ({c.score:.2f})" for c in extracted.visual_concepts
        )
        story.append(Paragraph(f"<b>Визуальные концепты:</b> {vc}", body))
    if extracted.entities:
        ents = ", ".join(
            f"{_esc(e.value)} [{_esc(e.type)}]" for e in extracted.entities
        )
        story.append(Paragraph(f"<b>Сущности:</b> {ents}", body))
    story.append(Spacer(1, 8))

    # --- Объяснение ---
    story.append(Paragraph("Почему помечено", h_section))
    reasons = explain(score, extracted.entities, extracted)
    if not reasons:
        reasons = ["Явных признаков нарушения не выявлено."]
    for r in reasons:
        story.append(Paragraph(f"• {_esc(r)}", body))
    story.append(Spacer(1, 14))

    # --- Футер human-in-the-loop ---
    story.append(HRFlowable(width="100%", thickness=0.5, color="#999999"))
    story.append(Spacer(1, 4))
    story.append(Paragraph(_FOOTER_RU, h_sub))

    doc.build(story)
    return buf.getvalue()
