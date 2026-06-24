"""FastAPI-приложение КӨЗ — единственная точка инстанцирования `app` (F0).

- lifespan (A3): одно sqlite-соединение в app.state.db; init_db; опционально
  seed-загрузка (если есть артефакт модели И demo-файл); фоновый тикер ингестии.
- auto-routers (A2): автообнаружение `router` в любом app/**/routes.py —
  фича-команды НИКОГДА не правят main.py.
- статика монтируется ПОСЛЕДНЕЙ (§0.6).
"""

import asyncio
import importlib
import json
import os
import pkgutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import app as app_pkg
from app import config, db


def ingestion_tick() -> None:
    """Заглушка фонового тикера ингестии (синхронный шаг).

    В F0 — no-op (возвращает None). Реальный drip-reveal seed-постов
    (db.reveal_next) подключит фича ингестии, переопределив этот шаг.
    """
    return None


async def _ticker_loop(application: FastAPI) -> None:
    """Периодически вызывает шаг ингестии (drip-reveal). Безопасен к отмене."""
    while True:
        try:
            await asyncio.sleep(config.TICK_INTERVAL_SEC)
            tick = getattr(application.state, "ingestion_tick", ingestion_tick)
            tick()
        except asyncio.CancelledError:
            break
        except Exception as e:  # тикер не должен ронять приложение
            print(f"[ticker] step error: {e}")


def _maybe_seed(conn) -> None:
    """Загрузить demo-посты, если есть артефакт модели И demo-файл.

    seed.py появляется на более поздней фазе — пропускаем тихо, если его нет.
    """
    try:
        if config.CLF_PATH.exists() and config.DEMO_POSTS_PATH.exists():
            from app.ingestion import seed  # импорт здесь: модуль может ещё не существовать
            seed.load_seed(conn)
    except Exception as e:
        print(f"[seed] skipped: {e}")


# Периодический реальный сбор по watchlist (непрерывный мониторинг).
WATCHLIST_SCAN_INTERVAL_SEC = 600


def _scan_watchlist_once() -> None:
    """Один проход скана watchlist в отдельном потоке (СВОЁ соединение к БД)."""
    try:
        from app.watchlist.service import scan_watchlist

        wconn = db.connect()
        try:
            res = scan_watchlist(wconn)
            if res.get("added"):
                print(f"[watchlist] +{res['added']} постов, флагнуто {res.get('flagged', 0)}")
        finally:
            wconn.close()
    except Exception as e:
        print(f"[watchlist] step error: {e}")


async def _watchlist_loop() -> None:
    """Периодически сканирует watchlist-каналы (реальный сбор). Сетевой/CPU-разбор
    выносим в executor, чтобы не блокировать event-loop. Безопасен к отмене."""
    try:
        await asyncio.sleep(12)  # первый проход вскоре после старта — мониторинг сразу «живой»
        await asyncio.get_event_loop().run_in_executor(None, _scan_watchlist_once)
    except asyncio.CancelledError:
        return
    while True:
        try:
            await asyncio.sleep(WATCHLIST_SCAN_INTERVAL_SEC)
            await asyncio.get_event_loop().run_in_executor(None, _scan_watchlist_once)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[watchlist] loop error: {e}")


# Периодический АВТОНОМНЫЙ поиск опасных постов в интернете (YouTube ytsearch).
DISCOVERY_INTERVAL_SEC = 1800


def _discover_once() -> None:
    """Один проход автономного поиска в отдельном потоке (своё соединение к БД).

    env KOZ_DEEP_DISCOVER=1 включает глубокий разбор топ-1 свежего поста (реальные
    Whisper+OCR+CLIP) — чтобы к показу в ленте были посты с заполненным блоком
    доказательств без ручной 90-сек проверки. По умолчанию ВЫКЛ (без фоновых
    загрузок видео); сбои разбора проглатываются внутри discover() (no-500)."""
    try:
        from app.discovery.discover import discover

        deep = os.environ.get("KOZ_DEEP_DISCOVER", "") == "1"
        wconn = db.connect()
        try:
            # YouTube; Telegram — через watchlist
            res = discover(wconn, with_telegram=False, deep=deep, deep_top=1)
            if res.get("added"):
                tail = f", глубоко разобрано {res['deep_analyzed']}" if res.get("deep_analyzed") else ""
                print(f"[discovery] +{res['added']} реальных постов, флаг {res.get('flagged', 0)}{tail}")
        finally:
            wconn.close()
    except Exception as e:
        print(f"[discovery] step error: {e}")


async def _discovery_loop() -> None:
    """Фоновый автономный поиск. Включается env KOZ_AUTO_DISCOVER=1 (по умолчанию ВЫКЛ —
    тесты/офлайн не ходят в сеть; на боевом сервере run.bat включает)."""
    if os.environ.get("KOZ_AUTO_DISCOVER", "") != "1":
        return
    try:
        await asyncio.sleep(8)  # первый проход вскоре после старта — лента сразу с реальными
        await asyncio.get_event_loop().run_in_executor(None, _discover_once)
    except asyncio.CancelledError:
        return
    while True:
        try:
            await asyncio.sleep(DISCOVERY_INTERVAL_SEC)
            await asyncio.get_event_loop().run_in_executor(None, _discover_once)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[discovery] loop error: {e}")


@asynccontextmanager
async def lifespan(application: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    application.state.db = conn
    # Реальный drip-reveal: фоновый тикер постепенно раскрывает seed-посты,
    # имитируя непрерывный поток мониторинга (ticker крутится в этом же event-loop
    # потоке, что и conn -> без проблем потокобезопасности).
    application.state.ingestion_tick = lambda: db.reveal_next(conn, config.TICK_REVEAL_N)
    _maybe_seed(conn)
    ticker = asyncio.create_task(_ticker_loop(application))
    application.state.ticker = ticker
    watchlist_task = asyncio.create_task(_watchlist_loop())
    application.state.watchlist_task = watchlist_task
    discovery_task = asyncio.create_task(_discovery_loop())
    application.state.discovery_task = discovery_task
    try:
        yield
    finally:
        tasks = (ticker, watchlist_task, discovery_task)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        conn.close()


app = FastAPI(title="КӨЗ — AI Media Watch", lifespan=lifespan)


def _include_feature_routers(application):
    """Автообнаружение и подключение `router` из любого app/**/routes.py (A2)."""
    for modinfo in pkgutil.walk_packages(app_pkg.__path__, prefix="app."):
        if modinfo.name.endswith(".routes"):
            try:
                m = importlib.import_module(modinfo.name)
                r = getattr(m, "router", None)
                if r is not None:
                    application.include_router(r)
            except Exception as e:
                print(f"[routers] skipped {modinfo.name}: {e}")


_include_feature_routers(app)


@app.get("/")
def index():
    return FileResponse(str(config.WEB_DIR / "index.html"))


@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})


@app.get("/api/metrics")
def api_metrics():
    """Отчёт метрик собственной модели (критерий №2). 404, если ещё не обучена."""
    if not config.METRICS_PATH.exists():
        raise HTTPException(status_code=404, detail="metrics not found — train the model first")
    return json.loads(config.METRICS_PATH.read_text(encoding="utf-8"))


# Скачанные клипы (deep-разбор/live-проверка) отдаём статикой, чтобы аналитик мог
# проиграть проанализированное видео в drill-down (фронт строит /data/media/<file>).
# Монтируем ДО корневого "/" (Starlette матчит mounts по порядку) — иначе catch-all
# перехватит путь и вернёт 404.
config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/data/media", StaticFiles(directory=str(config.MEDIA_DIR)), name="media")

# Статика монтируется ПОСЛЕДНЕЙ (после всех @app.get и авто-роутеров) — §0.6.
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
