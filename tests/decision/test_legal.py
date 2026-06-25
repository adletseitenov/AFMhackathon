"""Тесты правового основания (app/decision/legal.py): статьи РК, наказание, путь.

Проверяем: для каждой угрозы есть релевантная статья + санкция + законный порядок
мер; лицензированный гемблинг идёт «рекламной» веткой без уголовных статей; чистая
/неизвестная категория не даёт обвинительных статей; функция crash-safe."""

from app.decision.legal import LEGAL_DISCLAIMER, legal_basis


def _has_article(lb, code_sub, art_sub):
    return any(code_sub in a["code"] and art_sub in a["article"] for a in lb["articles"])


def test_gambling_cites_igorny_and_reklama_articles():
    lb = legal_basis("gambling", licensed=False)
    assert lb["category"] == "gambling"
    assert _has_article(lb, "КоАП", "425")           # игорный бизнес
    assert _has_article(lb, "УК", "307")             # незаконное предпринимательство
    # каждая статья несёт наказание + суть
    for a in lb["articles"]:
        assert a["punishment"].strip() and a["summary"].strip()
    # законный путь: фиксация -> АФМ -> блокировка через уполномоченный орган
    joined = " ".join(lb["enforcement"]).lower()
    assert "афм" in joined and ("блокировк" in joined or "доступ" in joined)
    assert lb["disclaimer"] == LEGAL_DISCLAIMER


def test_pyramid_cites_uk_217():
    lb = legal_basis("pyramid")
    assert _has_article(lb, "УК", "217")             # финпирамида
    assert any("6 лет" in a["punishment"] or "лишени" in a["punishment"].lower()
               for a in lb["articles"])
    joined = " ".join(lb["enforcement"]).lower()
    assert "предупрежд" in joined or "нацбанк" in joined or "финанс" in joined


def test_fraud_cites_uk_190():
    lb = legal_basis("fraud")
    assert _has_article(lb, "УК", "190")             # мошенничество
    assert lb["category_ru"]


def test_licensed_gambling_has_no_criminal_articles():
    lb = legal_basis("gambling", licensed=True)
    # лицензированный оператор: уголовных статей (УК) быть НЕ должно — только реклама
    assert not any(a["code"].startswith("УК") for a in lb["articles"])
    joined = " ".join(lb["enforcement"]).lower()
    assert "не блокировать" in joined


def test_clean_or_unknown_has_no_accusatory_articles():
    for cat in ("clean", "", None, "whatever"):
        lb = legal_basis(cat)
        assert lb["articles"] == []
        assert isinstance(lb["enforcement"], list) and lb["enforcement"]


def test_never_raises_on_bad_input():
    # не падает на любом мусоре
    for bad in (123, [], {}, object()):
        lb = legal_basis(bad)  # type: ignore[arg-type]
        assert "articles" in lb and "enforcement" in lb
