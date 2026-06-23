"""Предзащитный smoke-тест «КӨЗ»: проверяет, что ключевые эндпоинты отвечают 200.

Это финальный интеграционный чек перед защитой. Поднимает РЕАЛЬНОЕ FastAPI-приложение
`app.main:app` через `fastapi.testclient.TestClient` (в процессе, без сети и без отдельного
uvicorn), раскрывает несколько seed-постов (через `POST /api/tick` или `db.reveal_next`),
затем дёргает каждый обязательный эндпоинт и печатает PASS/FAIL построчно.

Запуск:  python scripts/dry_run.py
Вывод:   таблица `[OK ] <METHOD> <path> -> 200` + финальная строка `DRY-RUN OK`/`DRY-RUN FAILED`.
Код возврата: 0 если все эндпоинты вернули 200, иначе 1.

check_endpoints(client) принимает любой объект с методами .get()/.post()
(httpx.Client или fastapi.testclient.TestClient) и возвращает [(label, status_code)].

ВАЖНО (gotcha): при поднятии реального app мы заранее переключаем config.DB_PATH на свежий
временный файл ДО импорта app.main — иначе стартующий lifespan возьмёт прод-БД data/koz.db,
которую может держать залоченным забытый dev-uvicorn, и сидинг упадёт.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Запуск как `python scripts/dry_run.py` кладёт на sys.path папку scripts/, а не корень репо,
# поэтому `import app...` падает. Добавляем корень репозитория (родитель папки scripts/) сами.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Консоль Windows по умолчанию cp1252 — print() кириллицы («КӨЗ») роняет UnicodeEncodeError.
# Переключаем stdout/stderr на UTF-8 (Python 3.7+ reconfigure). Безопасно и для не-Windows.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

# (method, path) — путь с {id} подставляется первым post_id из /api/feed.
# Список покрывает контракт F9: health + метрики + лента + drill-down + tick + граф + тренды + PDF.
REQUIRED_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/health"),
    ("GET", "/api/metrics"),
    ("GET", "/api/feed"),
    ("GET", "/api/post/{id}"),
    ("POST", "/api/tick"),
    ("GET", "/api/graph"),
    ("GET", "/api/trends"),
    ("GET", "/api/report/{id}.pdf"),
]


def _first_post_id(client) -> str | None:
    """Берёт id первого поста из приоритетной ленты (для подстановки в {id})."""
    for url in ("/api/feed?limit=1", "/api/feed"):
        try:
            resp = client.get(url)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except Exception:
            continue
        if data:
            item = data[0]
            # форма F4: [{"post": {...}, "score": {...}, ...}]; на всякий случай fallback на плоский id
            if isinstance(item, dict):
                post = item.get("post")
                if isinstance(post, dict) and post.get("id"):
                    return str(post["id"])
                if item.get("id"):
                    return str(item["id"])
    return None


def check_endpoints(client) -> list[tuple[str, int]]:
    """Дёргает каждый обязательный эндпоинт, возвращает [(label, status_code)].

    Для путей с {id} подставляет первый post_id из /api/feed; если данных нет — статус 0.
    """
    post_id = _first_post_id(client)
    results: list[tuple[str, int]] = []
    for method, path in REQUIRED_ENDPOINTS:
        real_path = path
        if "{id}" in path:
            if post_id is None:
                results.append((f"{method} {path}", 0))  # 0 = нет данных для подстановки
                continue
            real_path = path.replace("{id}", post_id)
        try:
            if method == "POST":
                resp = client.post(real_path)
            else:
                resp = client.get(real_path)
            results.append((f"{method} {path}", resp.status_code))
        except Exception:
            results.append((f"{method} {path}", 0))
    return results


def _build_client():
    """Поднимает реальное FastAPI-приложение «КӨЗ» через TestClient на свежей временной БД.

    Переключаем config.DB_PATH на временный файл ДО импорта app.main, чтобы lifespan засеял
    чистую БД и не наткнулся на лок прод-файла data/koz.db.
    """
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app import config

    tmp_db = Path(tempfile.gettempdir()) / "koz_dry_run.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(tmp_db) + suffix)
        try:
            if f.exists():
                f.unlink()
        except OSError:
            pass
    config.DB_PATH = tmp_db

    from app.main import app

    return TestClient(app)


def _reveal_some(client, n: int = 3) -> None:
    """Раскрывает несколько seed-постов, чтобы /api/feed и /api/post/{id} были непустыми.

    Сначала пробуем штатный путь POST /api/tick (если F4 его собрал); параллельно дёргаем
    db.reveal_next прямо по соединению из app.state.db как надёжный fallback.
    """
    # Путь 1: HTTP-тик (если роут существует).
    for _ in range(2):
        try:
            client.post("/api/tick")
        except Exception:
            break

    # Путь 2: прямой reveal через соединение приложения (надёжно, не зависит от F4-роута).
    try:
        from app import db

        conn = getattr(getattr(client, "app", None), "state", None)
        conn = getattr(conn, "db", None)
        if conn is not None:
            db.reveal_next(conn, n)
    except Exception:
        pass


def _run(client) -> int:
    """Раскрывает ленту, проверяет эндпоинты, печатает отчёт, возвращает код возврата."""
    _reveal_some(client, 3)

    results = check_endpoints(client)

    print("=== КӨЗ dry-run smoke-test ===")
    ok = True
    for label, status in results:
        mark = "OK " if status == 200 else "FAIL"
        if status != 200:
            ok = False
        print(f"[{mark}] {label} -> {status}")

    if ok:
        print("DRY-RUN OK")
        return 0
    print("DRY-RUN FAILED")
    return 1


def main(client=None) -> int:
    """Бутит app, раскрывает ленту, проверяет эндпоинты, печатает отчёт, возвращает код возврата.

    Когда клиент не передан, поднимаем реальное app и входим в TestClient как контекст-менеджер —
    это ЗАПУСКАЕТ lifespan (открывает app.state.db, сидит demo-посты, стартует тикер). Без входа
    в контекст lifespan не выполняется и все /api/* падают (status 0). Инъецированный клиент
    (стаб-тесты) используем как есть, не управляя его жизненным циклом.
    """
    if client is not None:
        return _run(client)

    built = _build_client()
    try:
        with built as ctx_client:  # вход в контекст = запуск lifespan приложения
            return _run(ctx_client)
    except Exception as exc:  # boot/lifespan упал — это тоже провал smoke-теста
        print("=== КӨЗ dry-run smoke-test ===")
        print(f"[FAIL] boot app via TestClient -> {type(exc).__name__}: {exc}")
        print("DRY-RUN FAILED")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
