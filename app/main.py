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


@asynccontextmanager
async def lifespan(application: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    application.state.db = conn
    application.state.ingestion_tick = ingestion_tick
    _maybe_seed(conn)
    ticker = asyncio.create_task(_ticker_loop(application))
    application.state.ticker = ticker
    try:
        yield
    finally:
        ticker.cancel()
        try:
            await ticker
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


# Статика монтируется ПОСЛЕДНЕЙ (после всех @app.get и авто-роутеров) — §0.6.
app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
