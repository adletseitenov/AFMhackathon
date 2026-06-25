"""Юнит-тесты реестра лицензированных в РК букмекеров (app/decision/licensed.py).

Покрываем края (по рекомендации ревью): алиасы (лат/кир), обфускация (разрядка),
и — критично для compliance — что НЕЛИЦЕНЗИРОВАННЫЕ бренды НЕ матчатся (нет ложного
«разрешён в РК» для mostbet/1xbet/1win/казино)."""

from app.decision.licensed import (
    COMPLIANCE_HINT,
    LICENSED_KZ_BOOKMAKERS,
    is_licensed,
    licensed_operators,
)


def test_licensed_matches_latin_and_cyrillic_aliases():
    assert licensed_operators("заходи на olimpbet, фрибет") == ["Olimpbet"]
    assert licensed_operators("ОЛИМПБЕТ ставка дня") == ["Olimpbet"]
    assert licensed_operators("PARI экспресс прогноз") == ["PARI"]
    assert licensed_operators("пари матч сегодня") == ["PARI"]


def test_licensed_matches_obfuscated_spacing():
    # анти-обфускация: разрядка букв не должна прятать оператора
    assert "Olimpbet" in licensed_operators("о л и м п б е т фрибет")


def test_unlicensed_brands_do_not_match():
    # НЕ лицензированы в РК -> флага быть НЕ должно (иначе ложное «разрешено»).
    # 1xBet перенесён в лицензированные (БК 1xBet.kz имеет лицензию РК) — здесь
    # только реально нелицензированные бренды/онлайн-казино.
    for txt in ("mostbet casino занос", "1win бонус",
                "vavada slots", "melbet занос", "онлайн казино рулетка"):
        assert licensed_operators(txt) == [], txt
        assert is_licensed(txt) is False, txt


def test_1xbet_kz_is_licensed():
    # Владелец АФМ-контекста: БК 1xBet.kz лицензирована в РК (сверять с реестром АФМ).
    assert "1xBet" in licensed_operators("Заходи на 1xbet, промокод")
    assert "1xBet" in licensed_operators("1хбет бонус сегодня")
    assert is_licensed("1 x bet занос") is True


def test_empty_and_none_safe():
    assert licensed_operators("") == []
    assert licensed_operators(None) == []
    assert is_licensed("") is False


def test_registry_shape_and_compliance_hint():
    assert "Olimpbet" in LICENSED_KZ_BOOKMAKERS and "PARI" in LICENSED_KZ_BOOKMAKERS
    for name, spec in LICENSED_KZ_BOOKMAKERS.items():
        assert "pattern" in spec and "note" in spec
    # подсказка ориентирует на проверку рекламы, а не на блокировку
    assert "блокировка не требуется" in COMPLIANCE_HINT.lower() or "не требуется" in COMPLIANCE_HINT


def test_editable_registry_add_remove_and_persist(tmp_path, monkeypatch):
    """Аналитик-редактируемый реестр: добавить оператора, отключить дефолтного,
    снова включить. Оверрайды персистятся в data/licensed_registry.json."""
    from app.decision import licensed as L
    monkeypatch.setattr(L, "_REGISTRY_PATH", tmp_path / "reg.json")

    # добавить кастомного оператора (по ключевым словам)
    state = L.set_operator("MyBook", True, keywords=["mybook", "майбук"])
    assert any(o["name"] == "MyBook" and o["source"] == "added" for o in state["operators"])
    assert "MyBook" in L.licensed_operators("реклама mybook сегодня")
    assert (tmp_path / "reg.json").exists()  # персист

    # отключить дефолтного (1xBet) -> больше не лицензирован
    L.set_operator("1xBet", False)
    assert L.licensed_operators("1xbet промокод") == []

    # снова включить 1xBet
    L.set_operator("1xBet", True)
    assert "1xBet" in L.licensed_operators("1xbet промокод")
