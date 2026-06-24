"""Тесты мультиплатформенного слоя данных watchlist (store + stats).

Проверяет:
  - store.list_entries() / add_entry() / remove_entry()
  - обратную совместимость: list_channels() / add_channel() / remove_channel()
  - легаси-формат на диске (плоский список строк)
  - нормализацию по платформам
  - дедупликацию по (platform, casefold(target))
  - stats.record_entry_scan() / get_entry_stats()
"""

import importlib
import json

import pytest

from app import config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """Подменяет DATA_DIR и перезагружает store."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store
    importlib.reload(store)
    return store


@pytest.fixture
def tmp_stats(tmp_path, monkeypatch):
    """Подменяет DATA_DIR и перезагружает stats."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import stats
    importlib.reload(stats)
    return stats


@pytest.fixture
def tmp_both(tmp_path, monkeypatch):
    """Подменяет DATA_DIR и перезагружает и store и stats."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store, stats
    importlib.reload(store)
    importlib.reload(stats)
    return store, stats


# ---------------------------------------------------------------------------
# list_entries() — изначально пусто
# ---------------------------------------------------------------------------

def test_list_entries_empty_initially(tmp_store):
    assert tmp_store.list_entries() == []


# ---------------------------------------------------------------------------
# add_entry() — базовая добавка
# ---------------------------------------------------------------------------

def test_add_entry_telegram(tmp_store):
    result = tmp_store.add_entry("@news", "telegram")
    assert result == [{"target": "news", "platform": "telegram"}]


def test_add_entry_youtube(tmp_store):
    result = tmp_store.add_entry("@MrBeast", "youtube")
    assert result == [{"target": "MrBeast", "platform": "youtube"}]


def test_add_entry_tiktok(tmp_store):
    result = tmp_store.add_entry("mostbet_official", "tiktok")
    assert result == [{"target": "mostbet_official", "platform": "tiktok"}]


def test_add_entry_operator(tmp_store):
    # оператор: сохраняет регистр target
    result = tmp_store.add_entry("1xBet", "operator")
    assert result == [{"target": "1xBet", "platform": "operator"}]


def test_add_entry_twitch(tmp_store):
    result = tmp_store.add_entry("@streamer", "twitch")
    assert result == [{"target": "streamer", "platform": "twitch"}]


def test_add_entry_kick(tmp_store):
    result = tmp_store.add_entry("@kickuser", "kick")
    assert result == [{"target": "kickuser", "platform": "kick"}]


def test_add_entry_instagram(tmp_store):
    result = tmp_store.add_entry("@instapage", "instagram")
    assert result == [{"target": "instapage", "platform": "instagram"}]


# ---------------------------------------------------------------------------
# Нормализация per-platform: telegram vs. прочие
# ---------------------------------------------------------------------------

def test_add_entry_telegram_normalizes_tme_link(tmp_store):
    # telegram: полный t.me парсинг через _normalize()
    tmp_store.add_entry("https://t.me/s/somechan", "telegram")
    assert tmp_store.list_entries() == [{"target": "somechan", "platform": "telegram"}]


def test_add_entry_non_telegram_no_tme_parsing(tmp_store):
    # youtube: не трогать t.me ссылку как telegram, просто strip @ у "@user"
    tmp_store.add_entry("@user123", "youtube")
    assert tmp_store.list_entries() == [{"target": "user123", "platform": "youtube"}]


def test_add_entry_operator_preserves_case(tmp_store):
    tmp_store.add_entry("Olimpbet", "operator")
    assert tmp_store.list_entries()[0]["target"] == "Olimpbet"


# ---------------------------------------------------------------------------
# Платформа: дефолт и неизвестное значение
# ---------------------------------------------------------------------------

def test_add_entry_default_platform_is_telegram(tmp_store):
    result = tmp_store.add_entry("channame")
    assert result == [{"target": "channame", "platform": "telegram"}]


def test_add_entry_unknown_platform_coerced_to_telegram(tmp_store):
    result = tmp_store.add_entry("channame", "unknown_platform")
    assert result == [{"target": "channame", "platform": "telegram"}]


def test_add_entry_none_platform_coerced_to_telegram(tmp_store):
    result = tmp_store.add_entry("channame", None)
    assert result == [{"target": "channame", "platform": "telegram"}]


def test_add_entry_empty_platform_coerced_to_telegram(tmp_store):
    result = tmp_store.add_entry("channame", "")
    assert result == [{"target": "channame", "platform": "telegram"}]


# ---------------------------------------------------------------------------
# Дедупликация по (platform, casefold(target))
# ---------------------------------------------------------------------------

def test_add_entry_dedup_same_platform_same_target_casefold(tmp_store):
    tmp_store.add_entry("MrBeast", "youtube")
    tmp_store.add_entry("mrbeast", "youtube")  # тот же, другой регистр
    assert len(tmp_store.list_entries()) == 1


def test_add_entry_same_target_different_platform_are_distinct(tmp_store):
    tmp_store.add_entry("x", "telegram")
    tmp_store.add_entry("x", "youtube")
    entries = tmp_store.list_entries()
    assert len(entries) == 2
    platforms = {e["platform"] for e in entries}
    assert platforms == {"telegram", "youtube"}


def test_add_entry_dedup_telegram_at_prefix(tmp_store):
    tmp_store.add_entry("@news", "telegram")
    tmp_store.add_entry("news", "telegram")
    assert len(tmp_store.list_entries()) == 1


# ---------------------------------------------------------------------------
# Пустой target -> noop
# ---------------------------------------------------------------------------

def test_add_entry_empty_target_noop(tmp_store):
    assert tmp_store.add_entry("   ", "telegram") == []
    assert tmp_store.add_entry("@", "telegram") == []


# ---------------------------------------------------------------------------
# remove_entry()
# ---------------------------------------------------------------------------

def test_remove_entry_telegram(tmp_store):
    tmp_store.add_entry("news", "telegram")
    tmp_store.add_entry("durov", "telegram")
    result = tmp_store.remove_entry("news", "telegram")
    assert result == [{"target": "durov", "platform": "telegram"}]


def test_remove_entry_specific_platform(tmp_store):
    tmp_store.add_entry("x", "telegram")
    tmp_store.add_entry("x", "youtube")
    tmp_store.remove_entry("x", "youtube")
    entries = tmp_store.list_entries()
    assert entries == [{"target": "x", "platform": "telegram"}]


def test_remove_entry_missing_is_noop(tmp_store):
    tmp_store.add_entry("a", "telegram")
    result = tmp_store.remove_entry("zzz", "telegram")
    assert result == [{"target": "a", "platform": "telegram"}]


def test_remove_entry_case_insensitive(tmp_store):
    tmp_store.add_entry("MrBeast", "youtube")
    result = tmp_store.remove_entry("mrbeast", "youtube")
    assert result == []


# ---------------------------------------------------------------------------
# Обратная совместимость: list_channels / add_channel / remove_channel
# ---------------------------------------------------------------------------

def test_add_channel_and_list_channels_unchanged(tmp_store):
    result = tmp_store.add_channel("@durov")
    assert result == ["durov"]
    assert tmp_store.list_channels() == ["durov"]


def test_list_channels_returns_only_telegram(tmp_store):
    tmp_store.add_entry("x", "telegram")
    tmp_store.add_entry("x", "youtube")
    tmp_store.add_entry("y", "tiktok")
    # list_channels() только telegram-цели
    assert tmp_store.list_channels() == ["x"]


def test_add_channel_creates_telegram_entry(tmp_store):
    tmp_store.add_channel("news")
    entries = tmp_store.list_entries()
    assert {"target": "news", "platform": "telegram"} in entries


def test_remove_channel_removes_telegram_entry(tmp_store):
    tmp_store.add_channel("a")
    tmp_store.add_channel("b")
    tmp_store.remove_channel("a")
    assert tmp_store.list_channels() == ["b"]
    # остальные платформы не тронуты
    assert tmp_store.list_entries() == [{"target": "b", "platform": "telegram"}]


def test_add_channel_then_add_entry_youtube_distinct(tmp_store):
    tmp_store.add_channel("@x")
    tmp_store.add_entry("x", "youtube")
    # два разных entry
    assert len(tmp_store.list_entries()) == 2
    # list_channels возвращает только telegram
    assert tmp_store.list_channels() == ["x"]


# ---------------------------------------------------------------------------
# Легаси-формат на диске (плоский список строк)
# ---------------------------------------------------------------------------

def test_legacy_flat_string_list_read(tmp_path, monkeypatch):
    """Старый watchlist.json как список строк читается корректно."""
    (tmp_path / "watchlist.json").write_text(
        json.dumps(["news", "durov"]), encoding="utf-8"
    )
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store
    importlib.reload(store)

    assert store.list_channels() == ["news", "durov"]
    assert store.list_entries() == [
        {"target": "news", "platform": "telegram"},
        {"target": "durov", "platform": "telegram"},
    ]


# ---------------------------------------------------------------------------
# Персистентность нового формата
# ---------------------------------------------------------------------------

def test_new_entries_persist_across_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import store
    importlib.reload(store)

    store.add_entry("mrb", "youtube")
    store.add_entry("chan", "telegram")

    importlib.reload(store)
    entries = store.list_entries()
    assert {"target": "mrb", "platform": "youtube"} in entries
    assert {"target": "chan", "platform": "telegram"} in entries


# ---------------------------------------------------------------------------
# stats: record_entry_scan / get_entry_stats
# ---------------------------------------------------------------------------

def test_get_entry_stats_empty_initially(tmp_stats):
    result = tmp_stats.get_entry_stats()
    assert result == {"entries": []}


def test_record_entry_scan_persists(tmp_stats):
    entry = tmp_stats.record_entry_scan(
        "news", "telegram", added=3, flagged=1,
        scanned_at="2026-06-25T00:00:00+00:00"
    )
    assert entry["target"] == "news"
    assert entry["platform"] == "telegram"
    assert entry["last_added"] == 3
    assert entry["last_flagged"] == 1
    assert entry["total_collected"] == 3
    assert entry["last_scan"] == "2026-06-25T00:00:00+00:00"
    assert entry["licensed"] is None


def test_record_entry_scan_accumulates_total(tmp_stats):
    tmp_stats.record_entry_scan("ch", "youtube", added=2, flagged=0,
                                 scanned_at="2026-06-25T00:00:00+00:00")
    tmp_stats.record_entry_scan("ch", "youtube", added=5, flagged=1,
                                 scanned_at="2026-06-25T01:00:00+00:00")
    entries = {(e["platform"], e["target"]): e for e in tmp_stats.get_entry_stats()["entries"]}
    e = entries[("youtube", "ch")]
    assert e["total_collected"] == 7
    assert e["last_added"] == 5
    assert e["last_flagged"] == 1
    assert e["last_scan"] == "2026-06-25T01:00:00+00:00"


def test_record_entry_scan_with_licensed(tmp_stats):
    entry = tmp_stats.record_entry_scan("1xbet", "operator", added=0, flagged=0,
                                         licensed=True)
    assert entry["licensed"] is True


def test_record_entry_scan_licensed_none_by_default(tmp_stats):
    entry = tmp_stats.record_entry_scan("somechan", "telegram", added=1, flagged=0)
    assert entry["licensed"] is None


def test_record_entry_scan_default_timestamp(tmp_stats, monkeypatch):
    monkeypatch.setattr(tmp_stats, "_now_iso", lambda: "2026-06-25T12:00:00+00:00")
    tmp_stats.record_entry_scan("ch", "tiktok", added=1, flagged=0)
    e = tmp_stats.get_entry_stats()["entries"][0]
    assert e["last_scan"] == "2026-06-25T12:00:00+00:00"


def test_get_entry_stats_shape(tmp_stats):
    tmp_stats.record_entry_scan("chan", "telegram", added=2, flagged=1,
                                 scanned_at="2026-06-25T00:00:00+00:00")
    result = tmp_stats.get_entry_stats()
    assert "entries" in result
    e = result["entries"][0]
    assert set(e.keys()) == {"target", "platform", "last_scan", "last_added",
                              "last_flagged", "total_collected", "licensed"}


def test_get_entry_stats_sorted_by_platform_then_target(tmp_stats):
    tmp_stats.record_entry_scan("zzz", "telegram", added=1, flagged=0)
    tmp_stats.record_entry_scan("aaa", "youtube", added=1, flagged=0)
    tmp_stats.record_entry_scan("bbb", "telegram", added=1, flagged=0)
    entries = tmp_stats.get_entry_stats()["entries"]
    keys = [(e["platform"], e["target"]) for e in entries]
    assert keys == sorted(keys)


def test_get_entry_stats_different_platforms_same_target_distinct(tmp_stats):
    tmp_stats.record_entry_scan("x", "telegram", added=1, flagged=0)
    tmp_stats.record_entry_scan("x", "youtube", added=2, flagged=0)
    entries = tmp_stats.get_entry_stats()["entries"]
    assert len(entries) == 2


def test_record_entry_scan_never_raises(tmp_stats):
    # патологические входы не должны бросать
    tmp_stats.record_entry_scan("", "telegram", added=1, flagged=0)
    tmp_stats.record_entry_scan("ch", "telegram", added="bad", flagged=None)
    tmp_stats.record_entry_scan(None, "telegram", added=1, flagged=0)


def test_get_entry_stats_tolerates_corrupt_file(tmp_path, monkeypatch):
    (tmp_path / "watchlist_entry_stats.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import stats
    importlib.reload(stats)
    assert stats.get_entry_stats() == {"entries": []}


def test_get_entry_stats_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.watchlist import stats
    importlib.reload(stats)
    assert stats.get_entry_stats() == {"entries": []}
