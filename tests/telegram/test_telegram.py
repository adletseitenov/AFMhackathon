"""F8 — Telegram/Platform-coverage тесты КӨЗ.

Покрывают три инварианта:
  1. build_demo_records() выдаёт >=3 telegram-поста, у каждого предвычислены
     telegram-сущности; хотя бы один пост скорится в REVIEW-полосу (40-69) — так
     демо-лента показывает спред escalate + review + clean.
  2. telegram-посты участвуют в build_graph: два поста, делящие канал
     casino_win_kz, связаны через общий узел entity:telegram:casino_win_kz.
  3. fetch_telegram_channel — ОПЦИОНАЛЬНЫЙ best-effort live-путь: при любой
     сетевой ошибке деградирует в [] без исключения.
"""

from app import config, db
from app.config import action_for_risk
from app.graph.build import build_graph
from app.models import Entity, Extracted, Post, VisualConcept
from scripts.build_demo import build_demo_records


# --- хелперы ---------------------------------------------------------------

def _telegram_records() -> list[dict]:
    return [r for r in build_demo_records() if r["post"]["platform"] == "telegram"]


def _to_post(d: dict) -> Post:
    return Post(
        id=d["id"], platform=d["platform"], author_handle=d["author_handle"],
        url=d["url"], caption=d["caption"], posted_at=d["posted_at"],
        media_path=d["media_path"], thumb_url=d["thumb_url"], source=d["source"],
    )


def _to_extracted(d: dict) -> Extracted:
    return Extracted(
        post_id=d["post_id"], caption=d["caption"], transcript=d["transcript"],
        ocr_text=d["ocr_text"],
        visual_concepts=[VisualConcept(**v) for v in d["visual_concepts"]],
        combined_text=d["combined_text"],
        entities=[Entity(**e) for e in d["entities"]],
    )


# --- 1. build_demo_records: >=3 telegram + entities + review-band ----------

def test_build_demo_yields_at_least_three_telegram_posts():
    tg = _telegram_records()
    assert len(tg) >= 3, "ожидали минимум 3 telegram-поста в демо-датасете"
    for r in tg:
        assert r["post"]["platform"] == "telegram"
        assert r["post"]["source"] == "telegram"
        assert r["post"]["url"].startswith("https://t.me/")


def test_telegram_posts_have_precomputed_telegram_entities():
    tg = _telegram_records()
    for r in tg:
        ex = r["extracted"]
        assert ex["post_id"] == r["post"]["id"]
        assert ex["combined_text"], "combined_text должен быть непустым для скоринга"
        types = {e["type"] for e in ex["entities"]}
        assert "telegram" in types, f"нет telegram-сущности у {r['post']['id']}"


def test_telegram_extracted_keys_match_canonical_shape():
    """extracted сохраняет канонический набор ключей (не ломает seed-loader)."""
    expected = {"post_id", "caption", "transcript", "ocr_text",
                "visual_concepts", "combined_text", "entities"}
    for r in _telegram_records():
        assert set(r["extracted"].keys()) == expected


def test_at_least_one_telegram_post_scores_in_review_band(tmp_path, monkeypatch):
    """Сквозной скоринг: >=1 telegram-пост попадает в REVIEW-полосу (40-69).

    Скорим в изолированной БД нашей же обученной моделью (критерий №2) — это
    надёжнее, чем эвристический прокси по тексту. Демо-спред: escalate+review+clean.
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "tg.db")
    conn = db.connect()
    db.init_db(conn)
    try:
        from app.decision.scoring import score_post

        risks: list[int] = []
        for r in _telegram_records():
            post = _to_post(r["post"])
            ex = _to_extracted(r["extracted"])
            db.insert_post(conn, post)
            db.upsert_extracted(conn, ex)
            score = score_post(post, ex, conn=conn)
            assert 0 <= score.risk <= 100
            risks.append(score.risk)

        review = [rk for rk in risks if config.REVIEW_THRESHOLD <= rk < config.ESCALATE_THRESHOLD]
        assert review, (
            "ожидали хотя бы один telegram-пост в REVIEW-полосе 40-69, "
            f"фактические риски: {risks}"
        )
        # И спред: хотя бы один escalate (>=70) рядом с review.
        assert any(rk >= config.ESCALATE_THRESHOLD for rk in risks), (
            f"ожидали хотя бы один escalate-пост, риски: {risks}"
        )
    finally:
        conn.close()


def test_review_band_caption_is_ambiguous_proxy():
    """Прокси-проверка: review-пост написан мягко — без явного %/бренда казино.

    Дополняет сквозной скоринг: подтверждает, что мягкая формулировка (а не
    забытый явный сигнал) держит пост в средней полосе риска.
    """
    tg = _telegram_records()
    # review-пост помечаем по отсутствию явных escalate-маркеров в caption.
    soft = [
        r for r in tg
        if "%" not in r["post"]["caption"]
        and "1xbet" not in r["post"]["caption"].lower()
        and "mostbet" not in r["post"]["caption"].lower()
        and "промокод" not in r["post"]["caption"].lower()
    ]
    assert soft, "ожидали хотя бы один telegram-пост с мягкой формулировкой"


# --- 2. graph: telegram-посты связаны общей t.me-сущностью -----------------

def test_telegram_posts_share_entity_node_in_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "g.db")
    conn = db.connect()
    db.init_db(conn)
    try:
        ids: list[str] = []
        for r in _telegram_records():
            post = _to_post(r["post"])
            ex = _to_extracted(r["extracted"])
            db.insert_post(conn, post)
            db.upsert_extracted(conn, ex)
            ids.append(post.id)

        graph = build_graph(ids, conn=conn)
        node_ids = {n["id"] for n in graph["nodes"]}

        tg_node = "entity:telegram:casino_win_kz"
        assert tg_node in node_ids, (
            f"ожидали узел общей telegram-сущности, узлы: {sorted(node_ids)}"
        )
        touching = {
            (e["source"], e["target"]) for e in graph["edges"]
            if tg_node in (e["source"], e["target"])
        }
        post_nodes_linked = {
            n for pair in touching for n in pair if n.startswith("post:")
        }
        assert len(post_nodes_linked) >= 2, (
            "оба telegram-поста должны делить общий узел сущности casino_win_kz"
        )
    finally:
        conn.close()


# --- 3. fetch_telegram_channel: best-effort деградация ----------------------

def test_fetch_telegram_channel_returns_empty_on_network_error(monkeypatch):
    """Сеть рушится -> [] без исключения (никогда не требуется для демо)."""
    import app.ingestion.fetch as fetch_mod

    def boom(*a, **k):
        raise OSError("сеть недоступна")

    monkeypatch.setattr(fetch_mod, "_http_get", boom)
    posts = fetch_mod.fetch_telegram_channel("https://t.me/Casino_Win_KZ")
    assert posts == [], "при ошибке сети ожидали пустой список без исключения"


def test_fetch_telegram_channel_bogus_url_degrades_gracefully(monkeypatch):
    """Битый/непарсимый URL не валит вызов — возвращается []."""
    import app.ingestion.fetch as fetch_mod

    def boom(*a, **k):
        raise ValueError("bogus")

    monkeypatch.setattr(fetch_mod, "_http_get", boom)
    assert fetch_mod.fetch_telegram_channel("not-a-real-url-$$$") == []


def test_fetch_telegram_channel_parses_public_preview(monkeypatch):
    """При валидном web-preview HTML парсит публичные сообщения в Post."""
    import app.ingestion.fetch as fetch_mod

    sample = (
        '<div class="tgme_widget_message" data-post="Casino_Win_KZ/12">'
        '<div class="tgme_widget_message_text">'
        'Гарантированный доход 300% жми t.me/Casino_Win_KZ</div>'
        '<a class="tgme_widget_message_date" href="https://t.me/Casino_Win_KZ/12">'
        '<time datetime="2026-06-23T09:15:00+00:00"></time></a></div>'
    )
    monkeypatch.setattr(fetch_mod, "_http_get", lambda url: sample)
    posts = fetch_mod.fetch_telegram_channel("https://t.me/Casino_Win_KZ")
    assert len(posts) >= 1
    p = posts[0]
    assert p.platform == "telegram"
    assert p.source == "live"
    assert "300%" in p.caption
    assert p.url.startswith("https://t.me/Casino_Win_KZ")
    assert p.author_handle == "@Casino_Win_KZ"
