# КӨЗ (AI Media Watch) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **§0 (ниже) — КАНОНИЧЕСКАЯ и переопределяет любые расхождения в посекционных планах фич (F0–F9).** Секции фич писались параллельно и местами разошлись в именах функций/путей; §0 сводит их к единому контракту. При конфликте — следовать §0.

**Goal:** Построить за ~24 ч локально запускаемый (Windows) MVP «КӨЗ» — аналитическую консоль АФМ, которая собирает посты соцсетей, оценивает риск **собственной обученной мультимодальной моделью** (критерий №2), объясняет признаки на русском и приоритизирует материалы для проверки.

**Architecture:** FastAPI + SQLite. Пайплайн: ingestion (seed-симуляция потока + live yt-dlp/upload) → мультимодальные экстракторы (Whisper/EasyOCR/open-CLIP + текст/сущности) с кэшем → собственный классификатор (заморож. мультиязычные эмбеддинги + наши признаки + sklearn-голова) → decision (скоринг/объяснение/рекомендация/аудит) → веб-консоль (лента/очередь, drill-down, граф связей, тренды, PDF-досье, live-проверка).

**Tech Stack:** Python 3.11, FastAPI, uvicorn, sqlite3 (stdlib), sentence-transformers (`paraphrase-multilingual-MiniLM-L12-v2`), scikit-learn (LogisticRegression), joblib, faster-whisper, easyocr, open-clip-torch + torch, yt-dlp, reportlab; фронт — Tailwind/Alpine/vis-network/Chart.js по CDN; pytest.

---

## §0. Канонические интерфейсы и правила согласования (ОБЯЗАТЕЛЬНО)

### §0.1 Владение файлами (single-owner)
- **`app/config.py`, `app/models.py`, `app/db.py`, `app/main.py` создаёт ТОЛЬКО F0.** Все остальные фичи их **Modify** (дописывают), не пересоздают.
- **`app/main.py`: приложение FastAPI инстанцируется один раз в F0.** Запрещено повторно создавать `app = FastAPI(...)` (исключить из F3 и F4). Все роуты добавляются в существующий `app` через `@app.get/@app.post` правкой `app/main.py`.
- **Тикер ингестии живёт в `lifespan(app)` (F0), а НЕ в `@app.on_event('startup')`** (исключить on_event из F4). lifespan стартует `asyncio.create_task(_ticker_loop())` на входе и отменяет на выходе.
- **`web/index.html` создаёт ТОЛЬКО F0** (полноценную оболочку консоли). F4/F5/F7 дописывают `web/app.js` и вью; index.html не пересоздавать.

### §0.2 Канонический API БД (`app/db.py` — подпись с `conn` ПЕРВЫМ аргументом)
Использовать ВЕЗДЕ именно эти имена/подписи (заменить в F3/F4/F7/F8 все `db.get_conn()`, бесконтекстные вызовы и несуществующие имена):
```
connect(path: str | Path | None = None) -> sqlite3.Connection   # path=None -> config.DB_PATH; row_factory=sqlite3.Row
init_db(conn) -> None
insert_post(conn, post: Post) -> None
get_post(conn, post_id: str) -> Post | None
get_revealed_posts(conn) -> list[Post]
upsert_extracted(conn, ex: Extracted) -> None
get_extracted(conn, post_id: str) -> Extracted | None
upsert_score(conn, score: Score, recommended_action: str, scored_at: str) -> None
get_score_row(conn, post_id: str) -> sqlite3.Row | None     # включает колонку recommended_action
add_audit(conn, ts: str, post_id: str, action: str, actor: str, detail: str) -> None
reveal_next(conn, n: int) -> int        # ставит revealed=1 следующим n постам, возвращает число раскрытых
reveal_post(conn, post_id: str) -> None # ДОБАВИТЬ в F0: раскрыть один пост (для F8)
```
- **Удалённые/запрещённые имена** (НЕ использовать): `get_conn`, `insert_extracted`, `get_score`, `insert_audit`, `upsert_post`, `get_recommended_action`, `reveal_post`(без conn), `db.DB_PATH` (путь живёт в `config.DB_PATH`).
- **В роутах** соединение брать из `request.app.state.db` (одно соединение из lifespan). **В standalone-функциях** (graph/pdf/trends/seed) открывать своё `conn = db.connect()` и закрывать в `finally` (sqlite3 `with` НЕ закрывает соединение — только коммитит).
- **Тесты** изолируют БД через monkeypatch `config.DB_PATH` (Path) на временный файл, затем `db.init_db(db.connect())`.

### §0.3 Константы `app/config.py` (источник истины — F0; F1 их ПОТРЕБЛЯЕТ, не переопределяет)
- Канон: `EMBEDDING_MODEL="paraphrase-multilingual-MiniLM-L12-v2"`, `BASE_DIR`, `ARTIFACTS_DIR=APP_DIR/"model"/"artifacts"`, `REVIEW_THRESHOLD=40`, `ESCALATE_THRESHOLD=70`, `CATEGORIES=["gambling","pyramid","fraud","clean"]`.
- **Удалить дубликаты из F1**: `EMBED_MODEL_NAME`, `PROJECT_ROOT`, `MODEL_DIR` — заменить на `EMBEDDING_MODEL`/`BASE_DIR`/`ARTIFACTS_DIR`. Пороги в F1 не переобъявлять.
- **Добавить в F0** `MEDIA_DIR = DATA_DIR / "media"` (нужен F2 `fetch.py`).
- **Whisper**: канон `WHISPER_MODEL="small"`; F2 `audio.py` грузит `config.WHISPER_MODEL` (не литерал `'base'`); `setup.bat` предзагружает ту же модель.

### §0.4 Имена признаков модели (единый словарь)
- Канонические имена сигналов из `HANDCRAFTED_SIGNALS` (F1): `payout_promise`, `casino_betting_brand`, `dm_cta`, `referral`, `promo_code`, `crypto_iban`, `urgency`, `money_emoji`, `visual_gambling`.
- **`visual_gambling` ДОЛЖЕН быть записью в `HANDCRAFTED_SIGNALS`** (реестр — единственный источник ключей признаков), а не отдельным ключом в `build_features`.
- **F3 `explain.py` `_FEATURE_LABELS` кейзится ровно на эти имена** (а не на `casino_brand/betting_brand/telegram`). RU-метки:
  `payout_promise→"Обещание гарантированного дохода"`, `casino_betting_brand→"Упоминание казино/букмекера"`, `dm_cta→"Призыв писать в личку (Telegram/WhatsApp)"`, `referral→"Реферальная схема"`, `promo_code→"Промокод"`, `crypto_iban→"Крипто-кошелёк/реквизиты"`, `urgency→"Срочность/давление"`, `money_emoji→"Демонстрация денег"`, `visual_gambling→"Визуальные маркеры азартных игр"`.
- `build_features` множество `gambling_visual` включает `{casino, roulette, betting_slip, cash_flaunt, luxury_car}`.

### §0.5 Ingestion seam
- **`app/ingestion/fetch.py` определяет `fetch_post(url: str | None = None, upload: tuple[bytes,str] | None = None) -> Post`** (обёртка над `fetch_link`/`handle_upload`, собирает `Post`). F4 `/api/analyze` вызывает `app.ingestion.fetch.fetch_post(...)` **через атрибут модуля** (`import app.ingestion.fetch as fetch_mod; fetch_mod.fetch_post(...)`), чтобы тесты могли monkeypatch'ить один seam. То же для `fetch_telegram_channel` (F8).
- **Извлечение telegram-сущностей — единственная реализация в F2** `extract_entities` (F8 НЕ переписывает telegram-ветку, только переиспользует).

### §0.6 Статика и SPA
- F0 монтирует статику в корень **последней** (после регистрации всех `/api/*`): `app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")`. Тогда `index.html` ссылается на `/app.js` и он отдаётся корректно. `/` отдаёт `index.html` (html=True).

### §0.7 Формы ответов API (авторитетно)
- `GET /api/feed` → `[{post, score, recommended_action}]`, отсортировано по `risk` desc (приоритетная очередь). Порядок-по-риску — единственная реализация во встроенном SQL роута; `get_revealed_posts` (сорт по времени) для ленты НЕ используется.
- `POST /api/analyze` — тело **JSON `{url}`** через `payload: dict | None = Body(default=None)` **ИЛИ** multipart `file: UploadFile | None = File(default=None)`; ветвление по тому, что пришло.
- Узлы графа (F5) могут нести доп. поле `high_risk: bool`, рёбра `type="contains"` — это согласованное расширение контракта.

### §0.8 Начальный скоринг demo/seed-постов
- Загрузчик seed (см. §0.9 — `app/ingestion/seed.py`) при старте **скорит каждый seed-пост** (`score_post`) и upsert'ит в `scores`, иначе INNER JOIN в `/api/feed` их отбросит. demo-посты несут предвычисленные `extracted` → скоринг мгновенный.

### §0.9 Недостающие задачи (полные спеки ниже — добавить как часть F0/данных)

#### Задача D1: `scripts/build_demo.py` — генерация `data/demo_posts.jsonl` с кэш-признаками
**Файлы:** Create `scripts/build_demo.py`; Test `tests/test_build_demo.py`
- [ ] **Шаг 1: Падающий тест** `tests/test_build_demo.py`:
```python
from scripts.build_demo import build_demo_records

def test_records_span_platforms_and_have_extracted():
    recs = build_demo_records()
    assert len(recs) >= 12
    platforms = {r["post"]["platform"] for r in recs}
    assert {"tiktok", "instagram", "youtube"}.issubset(platforms)
    for r in recs:
        assert set(r["post"].keys()) >= {"id","platform","author_handle","url","caption","posted_at","source"}
        ex = r["extracted"]
        assert set(ex.keys()) == {"post_id","caption","transcript","ocr_text","visual_concepts","combined_text","entities"}
        assert ex["post_id"] == r["post"]["id"]

def test_includes_high_risk_examples():
    recs = build_demo_records()
    cats = " ".join(r["extracted"]["combined_text"].lower() for r in recs)
    assert "1xbet" in cats or "казино" in cats
    assert "%" in cats  # обещания дохода
```
- [ ] **Шаг 2: Запустить, ожидать FAIL.** `python -m pytest tests/test_build_demo.py -v` → ModuleNotFoundError.
- [ ] **Шаг 3: Реализация** `scripts/build_demo.py`: функция `build_demo_records() -> list[dict]` возвращает ≥12 записей `{"post": {...}, "extracted": {...}}` по платформам tiktok/instagram/youtube, с реалистичными RU/KZ кэпшнами (реклама казино «1xBet/Mostbet», пирамиды «доход 30% в месяц», реф-схемы, плюс «чистые» примеры), у каждой — заполненный `extracted` (caption/transcript/ocr_text/visual_concepts/combined_text/entities) так, что `combined_text` = склейка из §0.10. `main()` пишет `data/demo_posts.jsonl` (по json-строке на запись, ensure_ascii=False). Использовать `app.extractors.text.extract_entities` для заполнения `entities`.
- [ ] **Шаг 4: Запустить, ожидать PASS.** `python -m pytest tests/test_build_demo.py -v`.
- [ ] **Шаг 5: Сгенерировать данные.** `python -m scripts.build_demo` → создан `data/demo_posts.jsonl`. Commit: `git add scripts/build_demo.py tests/test_build_demo.py && git commit -m "feat(data): demo posts with cached features"` (jsonl игнорируется .gitignore — это ок).

#### Задача D2: `app/ingestion/seed.py` — загрузка demo в БД + кэш + первичный скоринг
**Файлы:** Create `app/ingestion/seed.py`; Test `tests/test_seed.py`
- [ ] **Шаг 1: Падающий тест** `tests/test_seed.py`:
```python
from app import config, db
from app.ingestion import seed

def test_load_seed_inserts_posts_extracted_and_scores(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    conn = db.connect(); db.init_db(conn)
    n = seed.load_seed(conn)
    assert n >= 12
    rows = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert rows == n            # каждый seed-пост скорен
    ex = db.get_extracted(conn, conn.execute("SELECT id FROM posts LIMIT 1").fetchone()[0])
    assert ex is not None        # extracted закэширован в БД
```
- [ ] **Шаг 2: Запустить, ожидать FAIL.** `python -m pytest tests/test_seed.py -v`.
- [ ] **Шаг 3: Реализация** `app/ingestion/seed.py`: `load_seed(conn) -> int` читает `config.DEMO_POSTS_PATH` (если нет — вызывает `scripts.build_demo.build_demo_records()`), для каждой записи: строит `Post` и `Extracted` (dataclass'ы), `db.insert_post(conn, post)`, `db.upsert_extracted(conn, ex)`, затем `score = app.decision.scoring.score_post(post, ex)` (он сам upsert'ит score + audit). Посты по умолчанию `revealed=0`. Возвращает число загруженных. Вызывается из lifespan F0 при пустой БД.
- [ ] **Шаг 4: Запустить, ожидать PASS.** `python -m pytest tests/test_seed.py -v`.
- [ ] **Шаг 5: Commit.** `git add app/ingestion/seed.py tests/test_seed.py && git commit -m "feat(ingestion): seed loader scores demo posts at startup"`.

#### Задача D3: `GET /api/metrics` — отчёт метрик модели (критерий №2 на питче)
**Файлы:** Modify `app/main.py`; Test `tests/test_metrics_route.py`
- [ ] **Шаг 1: Падающий тест** `tests/test_metrics_route.py`:
```python
import json
from fastapi.testclient import TestClient
from app import config
from app.main import app

def test_metrics_route_returns_report(tmp_path, monkeypatch):
    p = tmp_path / "metrics.json"
    p.write_text(json.dumps({"macro_f1": 0.82, "n_train": 480, "n_test": 120,
                             "per_class": {}, "confusion_matrix": []}), encoding="utf-8")
    monkeypatch.setattr(config, "METRICS_PATH", p)
    with TestClient(app) as c:
        r = c.get("/api/metrics")
        assert r.status_code == 200
        assert r.json()["macro_f1"] == 0.82

def test_metrics_route_404_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "METRICS_PATH", tmp_path / "missing.json")
    with TestClient(app) as c:
        assert c.get("/api/metrics").status_code == 404
```
- [ ] **Шаг 2: Запустить, ожидать FAIL.** `python -m pytest tests/test_metrics_route.py -v`.
- [ ] **Шаг 3: Реализация — добавить в `app/main.py`:**
```python
@app.get("/api/metrics")
def api_metrics():
    from app import config
    if not config.METRICS_PATH.exists():
        raise HTTPException(status_code=404, detail="metrics not found — train the model first")
    return json.loads(config.METRICS_PATH.read_text(encoding="utf-8"))
```
(убедиться, что `import json` и `from fastapi import HTTPException` присутствуют в `app/main.py`).
- [ ] **Шаг 4: Запустить, ожидать PASS.** `python -m pytest tests/test_metrics_route.py -v`.
- [ ] **Шаг 5: Commit.** `git add app/main.py tests/test_metrics_route.py && git commit -m "feat(api): /api/metrics serves model metrics report"`.

### §0.10 `combined_text` — канонический порядок склейки
`combined_text = "\n".join(filter(None, [caption, transcript, ocr_text, " ".join(vc.label for vc in visual_concepts)]))`. Использовать одинаково в `pipeline.extract`, `build_demo` и тестах.

### §0.11 `recommended_action` — единый 3-уровневый маппинг (переопределяет F0-тест и F3)
- **Канон:** `risk < 40 → "auto_clear"`, `40 ≤ risk < 70 → "review"`, `risk ≥ 70 → "escalate"`. Значение `"monitor"` остаётся допустимым в enum, но **порогами не производится** (YAGNI). Двух разных маппингов в коде быть не должно.
- `config.action_for_risk(risk)` реализует РОВНО этот 3-веточный маппинг (без полосы monitor); `app/decision/scoring.py` его использует (либо локальный `recommend_action` с идентичной логикой).
- **Поправка к тесту F0 `tests/test_config.py`** (переопределяет посекционный текст F0): ожидания должны быть `action_for_risk(10)=="auto_clear"`, `action_for_risk(20)=="auto_clear"`, `action_for_risk(40)=="review"`, `action_for_risk(55)=="review"`, `action_for_risk(70)=="escalate"`, `action_for_risk(95)=="escalate"` — **убрать кейс `=="monitor"`**. А `action_for_risk` в `config.py` сделать 3-веточным (как `recommend_action` в F3). Тесты F3 (`recommend_action(39)=="auto_clear"` и т.д.) остаются без изменений.

---

## Структура репозитория (карта файлов)

```
AFMHACKATHON/                  (отдельный git-репозиторий — НЕ домашний C:\Users\adlet\.git)
  app/
    __init__.py  config.py  models.py  db.py  main.py
    ingestion/   __init__.py  seed.py  fetch.py
    extractors/  __init__.py  text.py  audio.py  ocr.py  visual.py  pipeline.py
    model/       __init__.py  features.py  train.py  classifier.py  artifacts/{clf.joblib,metrics.json}
    decision/    __init__.py  scoring.py  explain.py
    graph/       build.py
    report/      pdf.py
    analytics/   trends.py
  data/          dataset.jsonl  demo_posts.jsonl  seeds.json  media/  feature_cache/
  web/           index.html  app.js
  scripts/       gen_dataset.py  build_demo.py  dry_run.py
  docs/          superpowers/{specs,plans}/  pitch/{koz-pitch.md,demo-script.md,qa-prep.md}
  tests/         (зеркалит app/)
  requirements.txt  setup.bat  run.bat  README.md
```

---

## Порядок сборки и параллелизм (24 ч, safety-net)

Каждый срез даёт работающее демо; время кончилось — отрезаем сверху.

1. **F0** (каркас, БД, модели, config, main+lifespan, статика) — фундамент, строго первым.
2. **D1, D2 каркас данных** сразу после F0 (нужны для ленты).
3. **F1** (модель) ∥ **F2** (экстракторы) — параллельно, зависят только от F0.
4. **F3** (decision) — после F1+F2. Затем **D2** довязывается (seed скорит через score_post), **D3** (`/api/metrics`).
5. **F4** (консоль) — после F3. Это «звезда» демо → первый приоритет среди UI.
6. **F5** (граф) ∥ **F6** (PDF) ∥ **F7** (тренды) — параллельно, зависят от F3.
7. **F8** (Telegram-датасет) — после F4/F5.
8. **F9** (питч/демо-скрипт/dry-run) — драфтить параллельно весь день, финализировать последним; `scripts/dry_run.py` — пред-защитный smoke-тест всех роутов на 200.

**Критическая цепочка демо:** F0 → (F1∥F2) → F3 → F4. Всё остальное — наслоение. Если время поджимает: оставить F0→F2→F3→F4 + F1-модель с метриками + F5-граф; F6/F7/F8 опциональны.

---

## Self-review (выполнено автором плана)
- **Покрытие спеки:** все 5 критериев и требования трека покрыты — своя модель (F1, метрики через D3), мультимодал (F2), скоринг+объяснение+приоритизация (F3/F4), граф/тренды/PDF/Telegram (F5–F8), этика/масштаб (в спеке + питч F9). Пробелы критика (build_demo, seed-loader, /api/metrics, начальный скоринг) закрыты задачами D1–D3 и §0.8.
- **Плейсхолдеры:** в §0/D1–D3 их нет; в посекционных планах фич код приведён полностью (см. ниже).
- **Согласованность типов/путей:** сведена в §0.2–§0.7 (канонический API БД, имена признаков, seam'ы, формы ответов). При расхождении посекционного текста с §0 — следовать §0.

---

# Посекционные планы фич (F0–F9)

> Ниже — детальные TDD-планы по каждой фиче (как просил заказчик). **Применять с поправкой на §0** (канонические имена/подписи). Известные расхождения, которые §0 уже разрешает: вызовы `db.get_conn()`/бесконтекстные db-вызовы → канон §0.2; повторная инстанциация FastAPI в F3/F4 → §0.1; имена признаков в F3 explain → §0.4; `fetch_post` seam → §0.5; статика `/app.js` → §0.6.


## F0: Каркас и инфраструктура

**Цель:** Создать запускаемый скелет проекта — конфиг, dataclass-контракт, схему SQLite со всеми хелперами, минимальное FastAPI-приложение (раздаёт `web/index.html`, health-route, статика, заглушка тикера фоновой ингестии) и bat-скрипты установки/запуска. Это фундамент, который импортируют все остальные фичи.

**Зависит от:** ничего / can start immediately.

---

### Задача 1: Инициализация репозитория и структуры пакета

**Файлы:**
- Create: `app/__init__.py`
- Create: `app/ingestion/__init__.py`
- Create: `app/extractors/__init__.py`
- Create: `app/model/__init__.py`
- Create: `app/decision/__init__.py`
- Create: `requirements.txt`

- [ ] **Шаг 1: Проверить корень git-репозитория** (на этой машине есть «случайный» репо в `C:\Users\adlet\.git`). Run:
  ```
  git rev-parse --show-toplevel
  ```
  Expected: путь оканчивается на `AFMHACKATHON`. Если показывает домашний каталог — выполнить `git init` в папке проекта и повторить проверку, и только потом коммитить.

- [ ] **Шаг 2: Создать пакеты-заглушки.** Создать пустые файлы `app/__init__.py`, `app/ingestion/__init__.py`, `app/extractors/__init__.py`, `app/model/__init__.py`, `app/decision/__init__.py` (содержимое — одна строка комментария):
  ```python
  # KÓZ package
  ```

- [ ] **Шаг 3: Заполнить `requirements.txt` закреплёнными зависимостями** (версии-минимумы под Python 3.11, Windows):
  ```
  fastapi==0.111.0
  uvicorn[standard]==0.30.1
  python-multipart==0.0.9
  pytest==8.2.2
  httpx==0.27.0
  sentence-transformers==2.7.0
  scikit-learn==1.5.0
  joblib==1.4.2
  yt-dlp==2024.5.27
  faster-whisper==1.0.2
  easyocr==1.7.1
  open-clip-torch==2.24.0
  torch==2.3.0
  reportlab==4.2.0
  ```

- [ ] **Шаг 4: Установить базовые зависимости для запуска тестов F0.** Run:
  ```
  python -m pip install fastapi "uvicorn[standard]" python-multipart pytest httpx
  ```
  Expected: установка без ошибок. (Тяжёлые ML-пакеты ставит `setup.bat` в задаче 7 — для F0-тестов они не нужны.)

- [ ] **Шаг 5: Создать `tests/` и `tests/__init__.py`** (пустой файл), чтобы pytest видел пакет тестов.

- [ ] **Шаг 6: Commit.** Run:
  ```
  git add app/__init__.py app/ingestion/__init__.py app/extractors/__init__.py app/model/__init__.py app/decision/__init__.py requirements.txt tests/__init__.py
  git commit -m "feat(scaffold): package skeleton + pinned requirements"
  ```

---

### Задача 2: `app/config.py` — пути, пороги, имена моделей

**Файлы:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

- [ ] **Шаг 1: Написать падающий тест** `tests/test_config.py`:
  ```python
  from app import config


  def test_thresholds_exact_values():
      assert config.REVIEW_THRESHOLD == 40
      assert config.ESCALATE_THRESHOLD == 70


  def test_embedding_model_name():
      assert config.EMBEDDING_MODEL == "paraphrase-multilingual-MiniLM-L12-v2"


  def test_paths_are_under_base_dir():
      assert config.DB_PATH.parent == config.DATA_DIR
      assert config.WEB_DIR.name == "web"
      assert config.CLF_PATH.name == "clf.joblib"


  def test_recommended_action_mapping():
      assert config.action_for_risk(10) == "auto_clear"
      assert config.action_for_risk(40) == "review"
      assert config.action_for_risk(55) == "review"
      assert config.action_for_risk(70) == "escalate"
      assert config.action_for_risk(95) == "escalate"
      assert config.action_for_risk(20) == "monitor"
  ```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run:
  ```
  python -m pytest tests/test_config.py -v
  ```
  Expected: FAIL — `ModuleNotFoundError` / `AttributeError` (config ещё нет).

- [ ] **Шаг 3: Минимальная реализация** `app/config.py`:
  ```python
  from pathlib import Path

  BASE_DIR = Path(__file__).resolve().parent.parent
  APP_DIR = BASE_DIR / "app"
  DATA_DIR = BASE_DIR / "data"
  WEB_DIR = BASE_DIR / "web"
  ARTIFACTS_DIR = APP_DIR / "model" / "artifacts"

  DB_PATH = DATA_DIR / "koz.db"
  CLF_PATH = ARTIFACTS_DIR / "clf.joblib"
  METRICS_PATH = ARTIFACTS_DIR / "metrics.json"
  CACHE_DIR = DATA_DIR / "feature_cache"

  DEMO_POSTS_PATH = DATA_DIR / "demo_posts.jsonl"
  DATASET_PATH = DATA_DIR / "dataset.jsonl"
  SEEDS_PATH = DATA_DIR / "seeds.json"

  EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
  WHISPER_MODEL = "small"
  OCR_LANGS = ["ru", "en"]
  CLIP_MODEL = "ViT-B-32"
  CLIP_PRETRAINED = "laion2b_s34b_b79k"

  REVIEW_THRESHOLD = 40
  ESCALATE_THRESHOLD = 70

  CATEGORIES = ["gambling", "pyramid", "fraud", "clean"]

  TICK_REVEAL_N = 3
  TICK_INTERVAL_SEC = 5


  def action_for_risk(risk: int) -> str:
      if risk >= ESCALATE_THRESHOLD:
          return "escalate"
      if risk >= REVIEW_THRESHOLD:
          return "review"
      if risk >= 20:
          return "monitor"
      return "auto_clear"
  ```

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run:
  ```
  python -m pytest tests/test_config.py -v
  ```
  Expected: 4 passed.

- [ ] **Шаг 5: Commit.** Run:
  ```
  git add app/config.py tests/test_config.py
  git commit -m "feat(config): paths, thresholds, action_for_risk mapping"
  ```

---

### Задача 3: `app/models.py` — все dataclass из контракта

**Файлы:**
- Create: `app/models.py`
- Test: `tests/test_models.py`

- [ ] **Шаг 1: Написать падающий тест** `tests/test_models.py` (проверяем точные имена полей и типы из контракта):
  ```python
  from dataclasses import fields

  from app.models import (
      Entity,
      VisualConcept,
      Extracted,
      Post,
      FeatureHit,
      Score,
      Edge,
      AuditEntry,
  )


  def _names(cls):
      return [f.name for f in fields(cls)]


  def test_entity_fields():
      assert _names(Entity) == ["type", "value", "normalized"]


  def test_visual_concept_fields():
      assert _names(VisualConcept) == ["label", "score"]


  def test_extracted_fields():
      assert _names(Extracted) == [
          "post_id", "caption", "transcript", "ocr_text",
          "visual_concepts", "combined_text", "entities",
      ]


  def test_post_fields():
      assert _names(Post) == [
          "id", "platform", "author_handle", "url", "caption",
          "posted_at", "media_path", "thumb_url", "source",
      ]


  def test_feature_hit_fields():
      assert _names(FeatureHit) == ["feature", "weight", "evidence"]


  def test_score_fields():
      assert _names(Score) == [
          "post_id", "risk", "category", "class_probs", "top_features",
      ]


  def test_edge_fields():
      assert _names(Edge) == ["source", "target", "type", "weight"]


  def test_audit_entry_defaults():
      assert _names(AuditEntry) == ["ts", "post_id", "action", "actor", "detail"]
      e = AuditEntry(ts="t", post_id="p", action="scored", detail="d")
      assert e.actor == "system"


  def test_construct_full_objects():
      ent = Entity(type="casino_brand", value="1xBet", normalized="1xbet")
      vc = VisualConcept(label="roulette", score=0.9)
      ex = Extracted(
          post_id="p1", caption="c", transcript="t", ocr_text="o",
          visual_concepts=[vc], combined_text="c t o", entities=[ent],
      )
      assert ex.entities[0].normalized == "1xbet"
      sc = Score(
          post_id="p1", risk=80, category="gambling",
          class_probs={"gambling": 0.8}, top_features=[FeatureHit(feature="payout", weight=0.5, evidence="30%")],
      )
      assert sc.risk == 80
  ```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run:
  ```
  python -m pytest tests/test_models.py -v
  ```
  Expected: FAIL — `ModuleNotFoundError: No module named 'app.models'`.

- [ ] **Шаг 3: Минимальная реализация** `app/models.py`:
  ```python
  from dataclasses import dataclass, field


  @dataclass
  class Entity:
      type: str
      value: str
      normalized: str


  @dataclass
  class VisualConcept:
      label: str
      score: float


  @dataclass
  class Extracted:
      post_id: str
      caption: str
      transcript: str
      ocr_text: str
      visual_concepts: list[VisualConcept]
      combined_text: str
      entities: list[Entity]


  @dataclass
  class Post:
      id: str
      platform: str
      author_handle: str
      url: str
      caption: str
      posted_at: str
      media_path: str | None
      thumb_url: str | None
      source: str


  @dataclass
  class FeatureHit:
      feature: str
      weight: float
      evidence: str


  @dataclass
  class Score:
      post_id: str
      risk: int
      category: str
      class_probs: dict[str, float]
      top_features: list[FeatureHit]


  @dataclass
  class Edge:
      source: str
      target: str
      type: str
      weight: float


  @dataclass
  class AuditEntry:
      ts: str
      post_id: str
      action: str
      detail: str
      actor: str = "system"
  ```
  Примечание: в `AuditEntry` поле с дефолтом (`actor`) ставится после полей без дефолта, поэтому порядок в dataclass — `ts, post_id, action, detail, actor`. Тест `test_audit_entry_defaults` сверяет именно этот порядок и дефолт `actor == "system"`.

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run:
  ```
  python -m pytest tests/test_models.py -v
  ```
  Expected: все тесты passed.

- [ ] **Шаг 5: Commit.** Run:
  ```
  git add app/models.py tests/test_models.py
  git commit -m "feat(models): all contract dataclasses"
  ```

---

### Задача 4: `app/db.py` — схема 4 таблиц + connect/init_db

**Файлы:**
- Create: `app/db.py`
- Test: `tests/test_db_schema.py`

- [ ] **Шаг 1: Написать падающий тест** `tests/test_db_schema.py` (используем временный sqlite-файл):
  ```python
  import sqlite3

  from app import db


  def _tables(conn):
      rows = conn.execute(
          "SELECT name FROM sqlite_master WHERE type='table'"
      ).fetchall()
      return {r[0] for r in rows}


  def _cols(conn, table):
      rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
      return [r[1] for r in rows]


  def test_init_db_creates_all_tables(tmp_path):
      dbfile = tmp_path / "t.db"
      conn = db.connect(str(dbfile))
      db.init_db(conn)
      tables = _tables(conn)
      assert {"posts", "extracted", "scores", "audit"} <= tables


  def test_posts_columns(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      cols = _cols(conn, "posts")
      assert cols == [
          "id", "platform", "author_handle", "url", "caption",
          "posted_at", "media_path", "thumb_url", "source", "revealed",
      ]


  def test_extracted_and_scores_columns(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      assert _cols(conn, "extracted") == [
          "post_id", "caption", "transcript", "ocr_text",
          "visual_concepts_json", "combined_text", "entities_json",
      ]
      assert _cols(conn, "scores") == [
          "post_id", "risk", "category", "class_probs_json",
          "top_features_json", "recommended_action", "scored_at",
      ]


  def test_audit_autoincrement_pk(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      assert _cols(conn, "audit") == [
          "id", "ts", "post_id", "action", "actor", "detail",
      ]


  def test_init_db_is_idempotent(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      db.init_db(conn)  # second call must not raise
      assert {"posts", "extracted", "scores", "audit"} <= _tables(conn)
  ```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run:
  ```
  python -m pytest tests/test_db_schema.py -v
  ```
  Expected: FAIL — `ModuleNotFoundError: No module named 'app.db'`.

- [ ] **Шаг 3: Минимальная реализация (часть 1 — connect/init_db)** в `app/db.py`:
  ```python
  import sqlite3
  from app import config

  SCHEMA = """
  CREATE TABLE IF NOT EXISTS posts (
      id TEXT PRIMARY KEY,
      platform TEXT,
      author_handle TEXT,
      url TEXT,
      caption TEXT,
      posted_at TEXT,
      media_path TEXT,
      thumb_url TEXT,
      source TEXT,
      revealed INTEGER DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS extracted (
      post_id TEXT PRIMARY KEY,
      caption TEXT,
      transcript TEXT,
      ocr_text TEXT,
      visual_concepts_json TEXT,
      combined_text TEXT,
      entities_json TEXT
  );
  CREATE TABLE IF NOT EXISTS scores (
      post_id TEXT PRIMARY KEY,
      risk INTEGER,
      category TEXT,
      class_probs_json TEXT,
      top_features_json TEXT,
      recommended_action TEXT,
      scored_at TEXT
  );
  CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT,
      post_id TEXT,
      action TEXT,
      actor TEXT,
      detail TEXT
  );
  """


  def connect(path: str | None = None) -> sqlite3.Connection:
      db_path = path if path is not None else str(config.DB_PATH)
      if db_path != ":memory:":
          config.DATA_DIR.mkdir(parents=True, exist_ok=True)
      conn = sqlite3.connect(db_path, check_same_thread=False)
      conn.row_factory = sqlite3.Row
      conn.execute("PRAGMA journal_mode=WAL;")
      return conn


  def init_db(conn: sqlite3.Connection) -> None:
      conn.executescript(SCHEMA)
      conn.commit()
  ```

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run:
  ```
  python -m pytest tests/test_db_schema.py -v
  ```
  Expected: 5 passed.

- [ ] **Шаг 5: Commit.** Run:
  ```
  git add app/db.py tests/test_db_schema.py
  git commit -m "feat(db): schema init for 4 tables + connect/init_db"
  ```

---

### Задача 5: `app/db.py` — хелперы записи/чтения

**Файлы:**
- Modify: `app/db.py`
- Test: `tests/test_db_helpers.py`

- [ ] **Шаг 1: Написать падающий тест** `tests/test_db_helpers.py`:
  ```python
  import json

  from app import db
  from app.models import Post, Extracted, Entity, VisualConcept, Score, FeatureHit


  def _post(pid="p1", risk_source="seed"):
      return Post(
          id=pid, platform="tiktok", author_handle="@x", url="http://u",
          caption="играй и выигрывай", posted_at="2026-06-24T10:00:00",
          media_path=None, thumb_url=None, source=risk_source,
      )


  def test_insert_post_and_get(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      db.insert_post(conn, _post())
      got = db.get_post(conn, "p1")
      assert got.id == "p1"
      assert got.platform == "tiktok"
      assert got.source == "seed"


  def test_upsert_extracted_roundtrip(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      db.insert_post(conn, _post())
      ex = Extracted(
          post_id="p1", caption="c", transcript="t", ocr_text="o",
          visual_concepts=[VisualConcept(label="casino", score=0.7)],
          combined_text="c t o",
          entities=[Entity(type="casino_brand", value="1xBet", normalized="1xbet")],
      )
      db.upsert_extracted(conn, ex)
      got = db.get_extracted(conn, "p1")
      assert got.visual_concepts[0].label == "casino"
      assert got.entities[0].normalized == "1xbet"


  def test_upsert_score_sets_action(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      db.insert_post(conn, _post())
      sc = Score(
          post_id="p1", risk=85, category="gambling",
          class_probs={"gambling": 0.85, "clean": 0.05},
          top_features=[FeatureHit(feature="casino_brand", weight=0.6, evidence="1xBet")],
      )
      db.upsert_score(conn, sc, recommended_action="escalate", scored_at="2026-06-24T10:01:00")
      row = conn.execute("SELECT risk, category, recommended_action FROM scores WHERE post_id='p1'").fetchone()
      assert row["risk"] == 85
      assert row["recommended_action"] == "escalate"


  def test_add_audit_autoincrement(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      db.add_audit(conn, ts="t1", post_id="p1", action="scored", actor="system", detail="d1")
      db.add_audit(conn, ts="t2", post_id="p1", action="revealed", actor="system", detail="d2")
      rows = conn.execute("SELECT id, action FROM audit ORDER BY id").fetchall()
      assert [r["id"] for r in rows] == [1, 2]
      assert rows[1]["action"] == "revealed"


  def test_revealed_feed_query(tmp_path):
      conn = db.connect(str(tmp_path / "t.db"))
      db.init_db(conn)
      db.insert_post(conn, _post("p1"))
      db.insert_post(conn, _post("p2"))
      conn.execute("UPDATE posts SET revealed=1 WHERE id='p1'")
      conn.commit()
      revealed = db.get_revealed_posts(conn)
      assert [p.id for p in revealed] == ["p1"]
  ```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run:
  ```
  python -m pytest tests/test_db_helpers.py -v
  ```
  Expected: FAIL — `AttributeError: module 'app.db' has no attribute 'insert_post'`.

- [ ] **Шаг 3: Дописать хелперы в `app/db.py`** (добавить в конец файла; импорты `json`, dataclass/asdict — в начало):
  ```python
  import json
  from dataclasses import asdict
  from app.models import Post, Extracted, Entity, VisualConcept, Score


  def insert_post(conn, post: Post) -> None:
      conn.execute(
          "INSERT OR REPLACE INTO posts "
          "(id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source) "
          "VALUES (?,?,?,?,?,?,?,?,?)",
          (post.id, post.platform, post.author_handle, post.url, post.caption,
           post.posted_at, post.media_path, post.thumb_url, post.source),
      )
      conn.commit()


  def get_post(conn, post_id: str) -> Post | None:
      r = conn.execute(
          "SELECT id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source "
          "FROM posts WHERE id=?", (post_id,)).fetchone()
      if r is None:
          return None
      return Post(*[r[k] for k in
                    ("id", "platform", "author_handle", "url", "caption",
                     "posted_at", "media_path", "thumb_url", "source")])


  def get_revealed_posts(conn) -> list[Post]:
      rows = conn.execute(
          "SELECT id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source "
          "FROM posts WHERE revealed=1 ORDER BY posted_at").fetchall()
      return [Post(*[r[k] for k in
                     ("id", "platform", "author_handle", "url", "caption",
                      "posted_at", "media_path", "thumb_url", "source")]) for r in rows]


  def upsert_extracted(conn, ex: Extracted) -> None:
      conn.execute(
          "INSERT OR REPLACE INTO extracted "
          "(post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json) "
          "VALUES (?,?,?,?,?,?,?)",
          (ex.post_id, ex.caption, ex.transcript, ex.ocr_text,
           json.dumps([asdict(v) for v in ex.visual_concepts], ensure_ascii=False),
           ex.combined_text,
           json.dumps([asdict(e) for e in ex.entities], ensure_ascii=False)),
      )
      conn.commit()


  def get_extracted(conn, post_id: str) -> Extracted | None:
      r = conn.execute(
          "SELECT post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json "
          "FROM extracted WHERE post_id=?", (post_id,)).fetchone()
      if r is None:
          return None
      vcs = [VisualConcept(**v) for v in json.loads(r["visual_concepts_json"] or "[]")]
      ents = [Entity(**e) for e in json.loads(r["entities_json"] or "[]")]
      return Extracted(
          post_id=r["post_id"], caption=r["caption"], transcript=r["transcript"],
          ocr_text=r["ocr_text"], visual_concepts=vcs,
          combined_text=r["combined_text"], entities=ents,
      )


  def upsert_score(conn, score: Score, recommended_action: str, scored_at: str) -> None:
      conn.execute(
          "INSERT OR REPLACE INTO scores "
          "(post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at) "
          "VALUES (?,?,?,?,?,?,?)",
          (score.post_id, score.risk, score.category,
           json.dumps(score.class_probs, ensure_ascii=False),
           json.dumps([asdict(f) for f in score.top_features], ensure_ascii=False),
           recommended_action, scored_at),
      )
      conn.commit()


  def get_score_row(conn, post_id: str):
      return conn.execute("SELECT * FROM scores WHERE post_id=?", (post_id,)).fetchone()


  def add_audit(conn, ts: str, post_id: str, action: str, actor: str, detail: str) -> None:
      conn.execute(
          "INSERT INTO audit (ts, post_id, action, actor, detail) VALUES (?,?,?,?,?)",
          (ts, post_id, action, actor, detail),
      )
      conn.commit()


  def reveal_next(conn, n: int) -> int:
      rows = conn.execute(
          "SELECT id FROM posts WHERE revealed=0 ORDER BY posted_at LIMIT ?", (n,)).fetchall()
      ids = [r["id"] for r in rows]
      for pid in ids:
          conn.execute("UPDATE posts SET revealed=1 WHERE id=?", (pid,))
      conn.commit()
      return len(ids)
  ```

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run:
  ```
  python -m pytest tests/test_db_helpers.py -v
  ```
  Expected: 5 passed.

- [ ] **Шаг 5: Commit.** Run:
  ```
  git add app/db.py tests/test_db_helpers.py
  git commit -m "feat(db): insert/upsert/audit/query helpers + reveal_next"
  ```

---

### Задача 6: `web/index.html` плейсхолдер + `app/main.py` (boot, `/`, health, статика, тикер-заглушка)

**Файлы:**
- Create: `web/index.html`
- Create: `app/main.py`
- Test: `tests/test_main_boot.py`

- [ ] **Шаг 1: Создать плейсхолдер** `web/index.html`:
  ```html
  <!doctype html>
  <html lang="ru">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>КӨЗ — AI Media Watch</title>
  </head>
  <body>
    <h1>КӨЗ — мониторинг соцсетей</h1>
    <p>Аналитическая консоль АФМ загружается…</p>
  </body>
  </html>
  ```

- [ ] **Шаг 2: Написать падающий тест** `tests/test_main_boot.py` (smoke: приложение поднимается, `/` отдаёт 200 и наш плейсхолдер, health работает, тикер-заглушка регистрируется):
  ```python
  from fastapi.testclient import TestClient

  from app.main import app, ingestion_tick


  def test_root_serves_index_html():
      client = TestClient(app)
      resp = client.get("/")
      assert resp.status_code == 200
      assert "text/html" in resp.headers["content-type"]
      assert "КӨЗ" in resp.text


  def test_health_route():
      client = TestClient(app)
      resp = client.get("/health")
      assert resp.status_code == 200
      assert resp.json() == {"status": "ok"}


  def test_ingestion_tick_stub_is_callable():
      # background ticker stub must exist and be a no-op-safe coroutine-free callable
      result = ingestion_tick()
      assert result is None
  ```

- [ ] **Шаг 3: Запустить тест (ожидаем провал).** Run:
  ```
  python -m pytest tests/test_main_boot.py -v
  ```
  Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Шаг 4: Минимальная реализация** `app/main.py`:
  ```python
  from contextlib import asynccontextmanager

  from fastapi import FastAPI
  from fastapi.responses import FileResponse, JSONResponse
  from fastapi.staticfiles import StaticFiles

  from app import config, db


  def ingestion_tick() -> None:
      """Заглушка фонового тикера ингестии.

      В F0 ничего не делает (no-op). Реальную логику drip-reveal seed-постов
      (db.reveal_next) подключает фича ингестии. Возвращает None.
      """
      return None


  @asynccontextmanager
  async def lifespan(app: FastAPI):
      conn = db.connect()
      db.init_db(conn)
      app.state.db = conn
      # Тикер фоновой ингестии регистрируется здесь; в F0 — заглушка.
      app.state.ingestion_tick = ingestion_tick
      yield
      conn.close()


  app = FastAPI(title="КӨЗ — AI Media Watch", lifespan=lifespan)


  @app.get("/")
  def index():
      return FileResponse(str(config.WEB_DIR / "index.html"))


  @app.get("/health")
  def health():
      return JSONResponse({"status": "ok"})


  app.mount("/web", StaticFiles(directory=str(config.WEB_DIR)), name="web")
  ```

- [ ] **Шаг 5: Запустить тест (ожидаем успех).** Run:
  ```
  python -m pytest tests/test_main_boot.py -v
  ```
  Expected: 3 passed.

- [ ] **Шаг 6: Прогнать весь набор F0-тестов.** Run:
  ```
  python -m pytest tests/ -v
  ```
  Expected: все тесты (config + models + db_schema + db_helpers + main_boot) passed.

- [ ] **Шаг 7: Commit.** Run:
  ```
  git add web/index.html app/main.py tests/test_main_boot.py
  git commit -m "feat(main): FastAPI boot, / serves index, /health, static mount, ticker stub"
  ```

---

### Задача 7: `setup.bat` и `run.bat`

**Файлы:**
- Create: `setup.bat`
- Create: `run.bat`

- [ ] **Шаг 1: Создать `setup.bat`** (создаёт venv, ставит зависимости, предзагружает модели — Windows cmd):
  ```bat
  @echo off
  setlocal
  cd /d "%~dp0"

  echo [KOZ] Creating virtual environment...
  py -3.11 -m venv .venv || python -m venv .venv

  echo [KOZ] Installing dependencies...
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt

  echo [KOZ] Pre-downloading multilingual embedding model...
  python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')"

  echo [KOZ] Pre-downloading faster-whisper model (small)...
  python -c "from faster_whisper import WhisperModel; WhisperModel('small')"

  echo [KOZ] Pre-downloading EasyOCR models (ru, en)...
  python -c "import easyocr; easyocr.Reader(['ru','en'])"

  echo [KOZ] Pre-downloading open-clip model (ViT-B-32)...
  python -c "import open_clip; open_clip.create_model_and_transforms('ViT-B-32', pretrained='laion2b_s34b_b79k')"

  echo [KOZ] Setup complete.
  endlocal
  ```
  Примечание: имена моделей точно совпадают с `app/config.py` (`EMBEDDING_MODEL`, `WHISPER_MODEL`, `OCR_LANGS`, `CLIP_MODEL`, `CLIP_PRETRAINED`).

- [ ] **Шаг 2: Создать `run.bat`** (активирует venv и поднимает uvicorn):
  ```bat
  @echo off
  setlocal
  cd /d "%~dp0"

  if not exist ".venv\Scripts\activate.bat" (
    echo [KOZ] venv not found. Run setup.bat first.
    exit /b 1
  )

  call .venv\Scripts\activate.bat
  echo [KOZ] Starting server at http://127.0.0.1:8000 ...
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  endlocal
  ```

- [ ] **Шаг 3: Ручная проверка `run.bat`** (smoke без полного setup — базовые web-зависимости уже стоят из задачи 1). Run в одном терминале:
  ```
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  ```
  В другом терминале. Run:
  ```
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
  python -c "import urllib.request; print('КӨЗ' in urllib.request.urlopen('http://127.0.0.1:8000/').read().decode('utf-8'))"
  ```
  Expected: первая строка печатает `b'{"status":"ok"}'`; вторая печатает `True`. Остановить сервер (Ctrl+C).

- [ ] **Шаг 4: Создать `.gitignore`** (чтобы не закоммитить venv, БД, артефакты, кэш) — содержимое:
  ```
  .venv/
  __pycache__/
  *.pyc
  data/koz.db
  data/koz.db-wal
  data/koz.db-shm
  data/feature_cache/
  app/model/artifacts/
  ```

- [ ] **Шаг 5: Commit.** Run:
  ```
  git add setup.bat run.bat .gitignore
  git commit -m "feat(scaffold): setup.bat (deps + model pre-download), run.bat, gitignore"
  ```

---

## F1: Датасет + Своя модель (критерий №2)

**Цель:** Построить ядро проекта — собственный обученный риск-классификатор: сгенерировать датасет ~400–600 строк (RU+KZ) по классам `gambling/pyramid/fraud/clean` с трудными негативами, собрать инженерные признаки + замороженные мультиязычные эмбеддинги, обучить sklearn-голову (LogisticRegression), доказать качество метриками (macro-F1 >= 0.7 + confusion matrix), и отдавать объяснимый `Score` с `top_features` на русском.

**Зависит от:** `app/models.py` (dataclasses `Extracted`, `VisualConcept`, `Entity`, `Score`, `FeatureHit`), `app/config.py` (пути к артефактам), `requirements.txt` (sentence-transformers, scikit-learn, joblib) — это часть базового каркаса (feature F0). Если каркаса ещё нет, начать можно с минимальных заглушек dataclass'ов, но штатно F1 стартует после того, как `app/models.py` и `app/config.py` существуют.

---

### Задача 1: Конфиг путей и порогов для модели

**Файлы:**
- Modify: `app/config.py`

- [ ] **Шаг 1: Добавить пути артефактов модели и пороги в `app/config.py`.** Эти константы потребляются `train.py`, `classifier.py` и `/api/metrics`. Используем `pathlib` и якорим всё от корня проекта.

```python
# app/config.py  (добавить к существующему файлу)
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
DATASET_PATH = DATA_DIR / "dataset.jsonl"

MODEL_DIR = PROJECT_ROOT / "app" / "model"
ARTIFACTS_DIR = MODEL_DIR / "artifacts"
CLF_PATH = ARTIFACTS_DIR / "clf.joblib"
METRICS_PATH = ARTIFACTS_DIR / "metrics.json"

# Замороженный мультиязычный энкодер (open-source, локально)
EMBED_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# Классы модели (порядок фиксирован — используется во всех артефактах)
CATEGORIES = ["gambling", "pyramid", "fraud", "clean"]

# Пороги рекомендованного действия (используются в F-decision)
REVIEW_THRESHOLD = 40
ESCALATE_THRESHOLD = 70
```

- [ ] **Шаг 2: Проверка.** Запустить:

```
python -c "from app.config import CATEGORIES, CLF_PATH, DATASET_PATH; print(CATEGORIES, DATASET_PATH.name)"
```
Ожидаемо: `['gambling', 'pyramid', 'fraud', 'clean'] dataset.jsonl`

- [ ] **Шаг 3: Коммит.**
```
git rev-parse --show-toplevel   # убедиться, что это репозиторий проекта, НЕ домашний C:\Users\adlet\.git
git add app/config.py
git commit -m "feat(config): пути артефактов модели, классы и пороги риска"
```

---

### Задача 2: Реестр инженерных признаков `HANDCRAFTED_SIGNALS` + `build_features`

**Файлы:**
- Create: `app/model/__init__.py`
- Create: `app/model/features.py`
- Test: `tests/test_features.py`

- [ ] **Шаг 1: Написать падающий тест на извлечение признаков из известной строки.** Создать `tests/test_features.py`:

```python
# tests/test_features.py
from app.models import Extracted
from app.model.features import build_features, HANDCRAFTED_SIGNALS


def _ex(text: str) -> Extracted:
    return Extracted(
        post_id="t1",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text=text,
        entities=[],
    )


def test_payout_promise_signal_fires():
    feats = build_features(_ex("Гарантированный доход 30% в месяц без риска!"))
    assert feats["payout_promise"] == 1
    assert feats["urgency"] >= 0


def test_casino_brand_signal_fires():
    feats = build_features(_ex("Заходи на 1xBet и Pin-Up, бонус новичкам"))
    assert feats["casino_betting_brand"] == 1


def test_dm_cta_and_promo_signals():
    feats = build_features(_ex("Пиши в личку @manager, промокод BONUS500"))
    assert feats["dm_cta"] == 1
    assert feats["promo_code"] == 1


def test_clean_text_has_no_scam_signals():
    feats = build_features(_ex("Сегодня в Алматы открылась новая библиотека."))
    assert feats["payout_promise"] == 0
    assert feats["casino_betting_brand"] == 0
    assert feats["dm_cta"] == 0


def test_registry_shape():
    assert len(HANDCRAFTED_SIGNALS) >= 7
    for sig in HANDCRAFTED_SIGNALS:
        assert set(sig.keys()) == {"name", "pattern", "category_hint", "evidence_ru"}
        assert sig["category_hint"] in {"gambling", "pyramid", "fraud", "clean"}


def test_build_features_returns_every_signal_key():
    feats = build_features(_ex("обычный текст"))
    for sig in HANDCRAFTED_SIGNALS:
        assert sig["name"] in feats
```

- [ ] **Шаг 2: Запустить тест (ожидается ошибка импорта/падение).**
```
pytest tests/test_features.py -v
```
Ожидаемо: `ModuleNotFoundError: No module named 'app.model.features'` (или collection error).

- [ ] **Шаг 3: Минимальная реализация.** Создать пустой `app/model/__init__.py`, затем `app/model/features.py`:

```python
# app/model/features.py
import re
from app.models import Extracted

# Каждый сигнал: name (ключ признака), pattern (regex, re.I|re.U), category_hint, evidence_ru.
HANDCRAFTED_SIGNALS = [
    {
        "name": "payout_promise",
        "pattern": re.compile(
            r"(гарантирован\w*\s+доход|доход\s+гарантирован|"
            r"\d{2,3}\s*%\s*(в\s*(месяц|день|неделю)|годовых)|"
            r"кепілдік\s+табыс|айына\s+\d{2,3}\s*%)",
            re.I | re.U,
        ),
        "category_hint": "pyramid",
        "evidence_ru": "Обещание гарантированного/фиксированного дохода",
    },
    {
        "name": "casino_betting_brand",
        "pattern": re.compile(
            r"(1xbet|melbet|pin[-\s]?up|mostbet|parimatch|betcity|"
            r"vulkan|joycasino|1win|olimp|казино|рулетк\w*|букмекер\w*|ставк[аи])",
            re.I | re.U,
        ),
        "category_hint": "gambling",
        "evidence_ru": "Упоминание бренда казино/букмекера или ставок",
    },
    {
        "name": "dm_cta",
        "pattern": re.compile(
            r"(пиши\w*\s+в\s+(личк\w*|директ|лс|телеграм|whatsapp|ватсап)|"
            r"в\s+лс\b|жми\s+на\s+ссылк\w*|переходи\s+по\s+ссылк\w*|"
            r"жеке\s+хабарлама)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Призыв написать в личку / перейти по ссылке (CTA в DM)",
    },
    {
        "name": "referral",
        "pattern": re.compile(
            r"(реферал\w*|реф[-\s]?ссылк\w*|реф[-\s]?код|пригласи\s+друг\w*|"
            r"приведи\s+друг\w*|твоя\s+команда\s+зараб\w*|сетевой\s+маркетинг|"
            r"мачты\w*\s+тарт\w*)",
            re.I | re.U,
        ),
        "category_hint": "pyramid",
        "evidence_ru": "Реферальная / сетевая схема вовлечения",
    },
    {
        "name": "promo_code",
        "pattern": re.compile(
            r"(промокод\w*|промо[-\s]?код\w*|бонус[-\s]?код\w*|"
            r"кодом?\s+[A-Z0-9]{3,12}\b|промокод\s+[A-Z0-9]{3,12}\b)",
            re.I | re.U,
        ),
        "category_hint": "gambling",
        "evidence_ru": "Промокод / бонус-код",
    },
    {
        "name": "crypto_iban",
        "pattern": re.compile(
            r"(\b0x[a-fA-F0-9]{6,}\b|\b(bc1|[13])[a-zA-HJ-NP-Z0-9]{20,}\b|"
            r"\bKZ\d{2}[A-Z0-9]{12,}\b|usdt|tether|trc20|btc[-\s]?кошел\w*|"
            r"крипто[-\s]?кошел\w*)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Крипто-кошелёк / IBAN / реквизиты для перевода",
    },
    {
        "name": "urgency",
        "pattern": re.compile(
            r"(срочно|только\s+сегодня|успей|последн\w*\s+мест\w*|"
            r"количество\s+ограничен\w*|не\s+упусти|пока\s+не\s+поздно|"
            r"тез\s+арада|бүгін\s+ғана)",
            re.I | re.U,
        ),
        "category_hint": "fraud",
        "evidence_ru": "Лексика срочности / искусственный дефицит",
    },
    {
        "name": "money_emoji",
        "pattern": re.compile(r"[\U0001F4B0\U0001F4B5\U0001F4B4\U0001F4B6\U0001F911\U0001F4B8]"),
        "category_hint": "gambling",
        "evidence_ru": "Эмодзи денег / демонстрация богатства",
    },
]


def build_features(extracted: Extracted) -> dict:
    """Бинарные/счётные инженерные признаки по всему доступному тексту поста.

    Возвращает dict {signal_name: int}. combined_text — основной источник; если
    он пуст, собираем из caption/transcript/ocr_text.
    """
    text = extracted.combined_text or " ".join(
        [extracted.caption or "", extracted.transcript or "", extracted.ocr_text or ""]
    )
    feats: dict = {}
    for sig in HANDCRAFTED_SIGNALS:
        feats[sig["name"]] = 1 if sig["pattern"].search(text) else 0
    # визуальные концепты как дополнительный сигнал гемблинга
    gambling_visual = {"casino", "roulette", "betting_slip", "cash_flaunt"}
    feats["visual_gambling"] = (
        1 if any(vc.label in gambling_visual for vc in extracted.visual_concepts) else 0
    )
    return feats
```

- [ ] **Шаг 4: Запустить тест (ожидается успех).**
```
pytest tests/test_features.py -v
```
Ожидаемо: 6 passed.

- [ ] **Шаг 5: Коммит.**
```
git add app/model/__init__.py app/model/features.py tests/test_features.py
git commit -m "feat(model): реестр HANDCRAFTED_SIGNALS и build_features"
```

---

### Задача 3: Энкодер текста (замороженные мультиязычные эмбеддинги)

**Файлы:**
- Modify: `app/model/features.py`
- Test: `tests/test_features.py`

- [ ] **Шаг 1: Дописать падающий тест на вектор признаков.** Добавить в `tests/test_features.py`:

```python
# tests/test_features.py  (добавить)
import numpy as np
from app.model.features import build_feature_vector, embed_text


def test_embed_text_returns_fixed_vector():
    v = embed_text("привет, как дела")
    assert isinstance(v, np.ndarray)
    assert v.ndim == 1
    assert v.shape[0] == 384  # MiniLM-L12-v2 dim


def test_build_feature_vector_combines_embedding_and_handcrafted():
    vec, hand, combined = build_feature_vector(_ex("Гарантированный доход 30% в месяц!"))
    assert isinstance(vec, np.ndarray)
    # длина = эмбеддинг + кол-во инженерных признаков
    assert vec.shape[0] == 384 + len(hand)
    assert hand["payout_promise"] == 1
    assert "доход" in combined
```

- [ ] **Шаг 2: Запустить (ожидается падение).**
```
pytest tests/test_features.py::test_build_feature_vector_combines_embedding_and_handcrafted -v
```
Ожидаемо: `ImportError: cannot import name 'build_feature_vector'`.

- [ ] **Шаг 3: Реализация.** Добавить в `app/model/features.py` (вверху импорты, внизу — функции). Энкодер грузим лениво (singleton), чтобы тяжёлая модель не поднималась при импорте.

```python
# app/model/features.py  (добавить импорты вверху)
import numpy as np
from app.config import EMBED_MODEL_NAME

_ENCODER = None


def _get_encoder():
    global _ENCODER
    if _ENCODER is None:
        from sentence_transformers import SentenceTransformer
        _ENCODER = SentenceTransformer(EMBED_MODEL_NAME)
    return _ENCODER


def embed_text(text: str) -> np.ndarray:
    """Замороженный мультиязычный эмбеддинг строки -> 1D np.ndarray (384,)."""
    enc = _get_encoder()
    return np.asarray(enc.encode(text or "", normalize_embeddings=True), dtype=np.float32)


def build_feature_vector(extracted: Extracted):
    """Фьюзит замороженный эмбеддинг текста с инженерными признаками.

    Возвращает (combined_vec: np.ndarray, handcrafted: dict, combined_text: str).
    """
    combined_text = extracted.combined_text or " ".join(
        [extracted.caption or "", extracted.transcript or "", extracted.ocr_text or ""]
    ).strip()
    emb = embed_text(combined_text)
    hand = build_features(extracted)
    hand_vec = np.asarray([hand[k] for k in sorted(hand.keys())], dtype=np.float32)
    combined_vec = np.concatenate([emb, hand_vec])
    return combined_vec, hand, combined_text
```

- [ ] **Шаг 4: Запустить (ожидается успех; первый прогон скачает/поднимет модель — это ок).**
```
pytest tests/test_features.py -v
```
Ожидаемо: все тесты passed (включая 2 новых).

- [ ] **Шаг 5: Коммит.**
```
git add app/model/features.py tests/test_features.py
git commit -m "feat(model): замороженные эмбеддинги + build_feature_vector (fusion)"
```

---

### Задача 4: Генератор датасета `scripts/gen_dataset.py`

**Файлы:**
- Create: `scripts/gen_dataset.py`
- Test: `tests/test_gen_dataset.py`
- Create (артефакт): `data/dataset.jsonl`

- [ ] **Шаг 1: Падающий тест на форму датасета.** Создать `tests/test_gen_dataset.py`:

```python
# tests/test_gen_dataset.py
import json
from collections import Counter
from scripts.gen_dataset import build_rows


def test_build_rows_volume_and_schema():
    rows = build_rows()
    assert 400 <= len(rows) <= 600
    for r in rows:
        assert set(r.keys()) == {"text", "lang", "label"}
        assert r["lang"] in {"ru", "kk"}
        assert r["label"] in {"gambling", "pyramid", "fraud", "clean"}
        assert isinstance(r["text"], str) and len(r["text"].strip()) > 0


def test_all_classes_present_and_hard_negatives():
    rows = build_rows()
    labels = Counter(r["label"] for r in rows)
    for c in ("gambling", "pyramid", "fraud", "clean"):
        assert labels[c] >= 40, f"класс {c} недопредставлен: {labels[c]}"
    # трудные негативы: clean содержит легальную рекламу / финликбез / новости
    clean_texts = " ".join(r["text"] for r in rows if r["label"] == "clean").lower()
    assert "лиценз" in clean_texts or "финансов" in clean_texts


def test_both_languages_present():
    rows = build_rows()
    langs = Counter(r["lang"] for r in rows)
    assert langs["ru"] >= 100
    assert langs["kk"] >= 40
```

- [ ] **Шаг 2: Запустить (ожидается падение).**
```
pytest tests/test_gen_dataset.py -v
```
Ожидаемо: `ModuleNotFoundError: No module named 'scripts.gen_dataset'`.

- [ ] **Шаг 3: Реализация генератора.** Создать `scripts/__init__.py` (пустой) и `scripts/gen_dataset.py`. Используем шаблоны + слот-заполнители для комбинаторного объёма; класс `clean` обязательно содержит трудные негативы (легальная реклама, финликбез, новости о казино).

```python
# scripts/gen_dataset.py
import itertools
import json
import random
from app.config import DATASET_PATH

random.seed(42)

# --- GAMBLING (RU) ---
GAMBLING_RU = [
    "Заходи на {brand}, первый депозит удвоим! Промокод {code}",
    "{brand} раздаёт бонусы новичкам, регистрируйся по ссылке в профиле",
    "Поднял на ставках {amount} за вечер на {brand} 💰 пиши в личку как",
    "Рулетка {brand} — выигрыш гарантирован, заноси и забирай 🎰",
    "Лучшие коэффициенты только в {brand}, промокод {code} даёт +500%",
]
# --- GAMBLING (KZ) ---
GAMBLING_KZ = [
    "{brand} казиносына кір, бірінші депозитке бонус! Промокод {code}",
    "{brand} ставкаларынан {amount} ұттым 💰 жеке хабарлама жаз",
    "{brand} рулеткасы — ұтыс кепілдендірілген, ақша сал",
]
# --- PYRAMID / HYIP (RU) ---
PYRAMID_RU = [
    "Гарантированный доход {pct}% в месяц! Вложи {amount} — забери вдвое",
    "Наш инвестпроект платит {pct}% годовых без риска, приведи друга — бонус",
    "Финансовая свобода за 3 месяца! Реферальная программа, твоя команда зарабатывает",
    "Вложения от {amount}, доход гарантирован, выводи каждый день. Пиши в личку",
    "Сетевой маркетинг нового поколения: {pct}% в неделю, реф-ссылка в профиле",
]
# --- PYRAMID (KZ) ---
PYRAMID_KZ = [
    "Айына {pct}% кепілдік табыс! {amount} салыңыз — екі есе алыңыз",
    "Инвестжоба {pct}% төлейді, досыңды шақыр — бонус ал",
    "Қаржылық еркіндік 3 айда! Рефералдық бағдарлама, командаң табыс табады",
]
# --- FRAUD / REF-SCHEME (RU) ---
FRAUD_RU = [
    "Срочно! Только сегодня раздаю схему заработка, пиши в личку @{handle}",
    "Переведи {amount} на кошелёк {wallet} и получи x3 обратно за час",
    "Успей! Последние места в закрытый чат, промокод {code}, жми ссылку",
    "Раздаю бесплатно, но переходи по ссылке быстро, количество ограничено",
    "Заработок без вложений! Кинь USDT на {wallet}, верну вдвое. Срочно",
]
# --- FRAUD (KZ) ---
FRAUD_KZ = [
    "Тез арада! Бүгін ғана табыс схемасын беремін, жеке хабарлама жаз @{handle}",
    "{wallet} әмиянына {amount} аудар, бір сағатта x3 қайтарам",
    "Үлгер! Жабық чатқа соңғы орындар, промокод {code}, сілтемеге бас",
]
# --- CLEAN incl. HARD NEGATIVES (RU) ---
CLEAN_RU = [
    "Сегодня в Алматы открылась новая городская библиотека для всех желающих",
    "Минфин напоминает: проверяйте лицензию финансовой организации перед вложением",
    "Урок финансовой грамотности: как составить личный бюджет и подушку безопасности",
    "Новость: регулятор отозвал лицензию у нелегального онлайн-казино в РК",
    "Официальная реклама банка: вклад с лицензией, ставка известна заранее, без обещаний",
    "Прогноз погоды на выходные: ясно, температура до +25 градусов",
    "Курс по инвестициям объясняет риски рынка акций без обещаний гарантий",
    "Букмекерская контора получила лицензию и работает легально по закону РК",
    "Розыгрыш призов от магазина: условия прозрачны, без взносов и переводов",
    "Финликбез: чем финансовая пирамида отличается от законного инвестфонда",
]
# --- CLEAN (KZ) ---
CLEAN_KZ = [
    "Бүгін Алматыда жаңа қалалық кітапхана ашылды",
    "Қаржылық сауаттылық сабағы: жеке бюджетті қалай құру керек",
    "Реттеуші заңсыз онлайн-казинолардың лицензиясын қайтарып алды",
    "Ауа райы болжамы: демалыс күндері ашық, +25 градусқа дейін",
    "Банктің ресми жарнамасы: лицензиясы бар салым, мөлшерлемесі алдын ала белгілі",
]

BRANDS = ["1xBet", "Melbet", "Pin-Up", "Mostbet", "Parimatch", "1win", "Vulkan", "JoyCasino"]
CODES = ["BONUS500", "WIN777", "VIP100", "START300", "GOLD250"]
AMOUNTS = ["50 000 тг", "100 000 тг", "200 000 тг", "1 000 000 тг", "10 000 тг"]
PCTS = ["20", "30", "50", "120", "300"]
WALLETS = ["0xA1b2C3d4E5f6", "TRC20: TXyz123abc456", "bc1qar0srrr7xfkvy", "KZ86125KZT5004100100"]
HANDLES = ["money_pro", "invest_guru", "fast_cash", "crypto_king"]


def _fill(tpl: str) -> str:
    return tpl.format(
        brand=random.choice(BRANDS),
        code=random.choice(CODES),
        amount=random.choice(AMOUNTS),
        pct=random.choice(PCTS),
        wallet=random.choice(WALLETS),
        handle=random.choice(HANDLES),
    )


def _expand(templates, lang, label, n):
    """Раскрывает шаблоны в n уникальных строк через многократное заполнение слотов."""
    rows, seen = [], set()
    attempts = 0
    while len(rows) < n and attempts < n * 20:
        attempts += 1
        text = _fill(random.choice(templates))
        if text not in seen:
            seen.add(text)
            rows.append({"text": text, "lang": lang, "label": label})
    return rows


def build_rows():
    rows = []
    # gambling
    rows += _expand(GAMBLING_RU, "ru", "gambling", 90)
    rows += _expand(GAMBLING_KZ, "kk", "gambling", 30)
    # pyramid
    rows += _expand(PYRAMID_RU, "ru", "pyramid", 90)
    rows += _expand(PYRAMID_KZ, "kk", "pyramid", 30)
    # fraud
    rows += _expand(FRAUD_RU, "ru", "fraud", 90)
    rows += _expand(FRAUD_KZ, "kk", "fraud", 30)
    # clean (трудные негативы)
    rows += _expand(CLEAN_RU, "ru", "clean", 90)
    rows += _expand(CLEAN_KZ, "kk", "clean", 30)
    random.shuffle(rows)
    return rows


def main():
    rows = build_rows()
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATASET_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Записано {len(rows)} строк в {DATASET_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Шаг 4: Запустить тест (ожидается успех).**
```
pytest tests/test_gen_dataset.py -v
```
Ожидаемо: 3 passed. Если объём вышел < 400, увеличить шаблоны/слоты (больше BRANDS/AMOUNTS даёт больше уникальных комбинаций).

- [ ] **Шаг 5: Сгенерировать артефакт и проверить вручную.**
```
python scripts/gen_dataset.py
python -c "import json; rows=[json.loads(l) for l in open('data/dataset.jsonl',encoding='utf-8')]; print('rows:',len(rows)); from collections import Counter; print(Counter(r['label'] for r in rows))"
```
Ожидаемо: `rows: ` число в диапазоне 400–600 и счётчик с непустыми всеми 4 классами.

- [ ] **Шаг 6: Коммит.**
```
git add scripts/__init__.py scripts/gen_dataset.py tests/test_gen_dataset.py data/dataset.jsonl
git commit -m "feat(data): генератор датасета RU+KZ с трудными негативами + dataset.jsonl"
```

---

### Задача 5: Обучение модели `app/model/train.py` + метрики

**Файлы:**
- Create: `app/model/train.py`
- Test: `tests/test_train.py`
- Create (артефакты): `app/model/artifacts/clf.joblib`, `app/model/artifacts/metrics.json`

- [ ] **Шаг 1: Падающий тест на качество и форму метрик.** Создать `tests/test_train.py`:

```python
# tests/test_train.py
import json
from app.config import CLF_PATH, METRICS_PATH, CATEGORIES
from app.model.train import train


def test_train_reaches_sanity_macro_f1():
    metrics = train()  # обучает, сохраняет артефакты, возвращает dict метрик
    assert metrics["macro_f1"] >= 0.7, f"macro-F1 слишком низкий: {metrics['macro_f1']}"


def test_train_writes_artifacts_and_metrics_schema():
    metrics = train()
    assert CLF_PATH.exists()
    assert METRICS_PATH.exists()
    saved = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    assert set(saved.keys()) >= {
        "per_class", "confusion_matrix", "macro_f1", "n_train", "n_test", "labels"
    }
    assert saved["labels"] == CATEGORIES
    for c in CATEGORIES:
        assert set(saved["per_class"][c].keys()) == {"precision", "recall", "f1"}
    # confusion matrix квадратная 4x4
    assert len(saved["confusion_matrix"]) == len(CATEGORIES)
    assert all(len(row) == len(CATEGORIES) for row in saved["confusion_matrix"])
```

- [ ] **Шаг 2: Запустить (ожидается падение).**
```
pytest tests/test_train.py::test_train_writes_artifacts_and_metrics_schema -v
```
Ожидаемо: `ModuleNotFoundError: No module named 'app.model.train'`.

- [ ] **Шаг 3: Реализация.** Создать `app/model/train.py`. Кодируем тексты через `embed_text`, добавляем инженерные признаки из `build_features` (тот же порядок ключей, `sorted`), обучаем `LogisticRegression`, стратифицированный split, метрики через sklearn.

```python
# app/model/train.py
import json
import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_recall_fscore_support, confusion_matrix, f1_score

from app.config import DATASET_PATH, CLF_PATH, METRICS_PATH, CATEGORIES
from app.models import Extracted
from app.model.features import embed_text, build_features


def _load_dataset():
    rows = []
    with open(DATASET_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _row_to_extracted(text: str) -> Extracted:
    return Extracted(
        post_id="train",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text=text,
        entities=[],
    )


def _vectorize(texts):
    feat_keys = None
    X = []
    for t in texts:
        ex = _row_to_extracted(t)
        emb = embed_text(t)
        hand = build_features(ex)
        if feat_keys is None:
            feat_keys = sorted(hand.keys())
        hand_vec = np.asarray([hand[k] for k in feat_keys], dtype=np.float32)
        X.append(np.concatenate([emb, hand_vec]))
    return np.vstack(X), feat_keys


def train():
    rows = _load_dataset()
    texts = [r["text"] for r in rows]
    labels = [r["label"] for r in rows]
    X, feat_keys = _vectorize(texts)
    y = np.asarray(labels)

    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.25, stratify=y, random_state=42
    )
    clf = LogisticRegression(max_iter=2000, C=4.0, class_weight="balanced")
    clf.fit(X_tr, y_tr)

    y_pred = clf.predict(X_te)
    macro = float(f1_score(y_te, y_pred, average="macro", labels=CATEGORIES))
    p, r, f1, _ = precision_recall_fscore_support(
        y_te, y_pred, labels=CATEGORIES, zero_division=0
    )
    cm = confusion_matrix(y_te, y_pred, labels=CATEGORIES).tolist()

    per_class = {
        CATEGORIES[i]: {
            "precision": round(float(p[i]), 4),
            "recall": round(float(r[i]), 4),
            "f1": round(float(f1[i]), 4),
        }
        for i in range(len(CATEGORIES))
    }
    metrics = {
        "labels": CATEGORIES,
        "per_class": per_class,
        "confusion_matrix": cm,
        "macro_f1": round(macro, 4),
        "n_train": int(len(y_tr)),
        "n_test": int(len(y_te)),
    }

    CLF_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Сохраняем классификатор вместе с порядком инженерных признаков и порядком классов.
    joblib.dump(
        {"clf": clf, "feat_keys": feat_keys, "labels": CATEGORIES},
        CLF_PATH,
    )
    METRICS_PATH.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


if __name__ == "__main__":
    m = train()
    print(f"macro-F1={m['macro_f1']} | n_train={m['n_train']} n_test={m['n_test']}")
    print("per-class:", json.dumps(m["per_class"], ensure_ascii=False))
```

- [ ] **Шаг 4: Запустить (ожидается успех; первый прогон поднимет энкодер — может занять 30–90 с).**
```
pytest tests/test_train.py -v
```
Ожидаемо: 2 passed, `macro_f1 >= 0.7`. Если ниже 0.7 — увеличить объём датасета в Задаче 4 и/или поднять `C`, затем перегенерировать датасет и переобучить.

- [ ] **Шаг 5: Записать артефакты в репозиторий.**
```
python app/model/train.py
git add app/model/train.py tests/test_train.py app/model/artifacts/clf.joblib app/model/artifacts/metrics.json
git commit -m "feat(model): обучение LogisticRegression-головы + отчёт метрик"
```

---

### Задача 6: `RiskClassifier` — предсказание + объяснимость (`top_features`)

**Файлы:**
- Create: `app/model/classifier.py`
- Test: `tests/test_classifier.py`

- [ ] **Шаг 1: Падающий тест на `predict() -> Score` и объяснимость.** Создать `tests/test_classifier.py`:

```python
# tests/test_classifier.py
from app.models import Extracted, Score, FeatureHit
from app.model.classifier import RiskClassifier
from app.config import CATEGORIES


def _ex(text: str) -> Extracted:
    return Extracted(
        post_id="p1",
        caption=text,
        transcript="",
        ocr_text="",
        visual_concepts=[],
        combined_text=text,
        entities=[],
    )


def test_predict_returns_valid_score():
    clf = RiskClassifier.load()
    score = clf.predict(_ex("Гарантированный доход 30% в месяц, пиши в личку @money_pro"))
    assert isinstance(score, Score)
    assert score.post_id == "p1"
    assert 0 <= score.risk <= 100
    assert score.category in CATEGORIES
    # class_probs покрывают все классы и нормированы ~1
    assert set(score.class_probs.keys()) == set(CATEGORIES)
    assert abs(sum(score.class_probs.values()) - 1.0) < 1e-3


def test_scam_text_scores_high_and_clean_low():
    clf = RiskClassifier.load()
    scam = clf.predict(_ex("Гарантированный доход 50% в месяц! Вложи и забери вдвое, срочно"))
    clean = clf.predict(_ex("Сегодня в Алматы открылась новая городская библиотека"))
    assert scam.risk > clean.risk
    assert clean.category == "clean"


def test_top_features_are_explainable_in_russian():
    clf = RiskClassifier.load()
    score = clf.predict(_ex("Промокод BONUS500 на 1xBet, рулетка, заноси 💰"))
    assert len(score.top_features) >= 1
    for hit in score.top_features:
        assert isinstance(hit, FeatureHit)
        assert isinstance(hit.feature, str) and hit.feature
        assert isinstance(hit.weight, float)
        assert isinstance(hit.evidence, str) and hit.evidence  # русская строка-доказательство
```

- [ ] **Шаг 2: Запустить (ожидается падение).**
```
pytest tests/test_classifier.py -v
```
Ожидаемо: `ModuleNotFoundError: No module named 'app.model.classifier'`.

- [ ] **Шаг 3: Реализация.** Создать `app/model/classifier.py`. `risk = round(100 * P(not clean))`, блендим с уверенностью топ-категории. `top_features` строим из активных инженерных признаков, взвешенных коэффициентами модели для предсказанного класса, с русскими evidence-строками из реестра.

```python
# app/model/classifier.py
import joblib
import numpy as np

from app.config import CLF_PATH, CATEGORIES
from app.models import Extracted, Score, FeatureHit
from app.model.features import embed_text, build_features, HANDCRAFTED_SIGNALS

# имя признака -> русская строка-доказательство (из реестра)
_EVIDENCE_RU = {sig["name"]: sig["evidence_ru"] for sig in HANDCRAFTED_SIGNALS}
_EVIDENCE_RU["visual_gambling"] = "Визуальные маркеры азартных игр (CLIP)"


class RiskClassifier:
    def __init__(self, clf, feat_keys, labels):
        self.clf = clf
        self.feat_keys = feat_keys
        self.labels = labels
        self.embed_dim = clf.coef_.shape[1] - len(feat_keys)

    @classmethod
    def load(cls, path=None):
        bundle = joblib.load(path or CLF_PATH)
        return cls(bundle["clf"], bundle["feat_keys"], bundle["labels"])

    def _vectorize(self, extracted: Extracted):
        text = extracted.combined_text or extracted.caption or ""
        emb = embed_text(text)
        hand = build_features(extracted)
        hand_vec = np.asarray([hand[k] for k in self.feat_keys], dtype=np.float32)
        return np.concatenate([emb, hand_vec]).reshape(1, -1), hand

    def _top_features(self, hand: dict, category: str) -> list:
        """Активные инженерные признаки × коэффициент модели для предсказанного класса."""
        cls_idx = list(self.clf.classes_).index(category)
        coefs = self.clf.coef_[cls_idx]
        hits = []
        for j, key in enumerate(self.feat_keys):
            if hand.get(key, 0):
                weight = float(coefs[self.embed_dim + j])
                hits.append(
                    FeatureHit(
                        feature=key,
                        weight=round(weight, 4),
                        evidence=_EVIDENCE_RU.get(key, key),
                    )
                )
        hits.sort(key=lambda h: abs(h.weight), reverse=True)
        return hits[:6]

    def predict(self, extracted: Extracted) -> Score:
        X, hand = self._vectorize(extracted)
        proba = self.clf.predict_proba(X)[0]
        classes = list(self.clf.classes_)
        class_probs = {c: float(proba[classes.index(c)]) for c in CATEGORIES}
        category = max(class_probs, key=class_probs.get)
        p_not_clean = 1.0 - class_probs.get("clean", 0.0)
        risk = int(round(100 * p_not_clean))
        top = self._top_features(hand, category)
        return Score(
            post_id=extracted.post_id,
            risk=risk,
            category=category,
            class_probs=class_probs,
            top_features=top,
        )
```

- [ ] **Шаг 4: Запустить (ожидается успех; требует артефактов из Задачи 5).**
```
pytest tests/test_classifier.py -v
```
Ожидаемо: 3 passed.

- [ ] **Шаг 5: Коммит.**
```
git add app/model/classifier.py tests/test_classifier.py
git commit -m "feat(model): RiskClassifier.predict -> Score с объяснимыми top_features"
```

---

### Задача 7: Регрессионный прогон всего модуля модели

**Файлы:**
- Test: (запуск существующих тестов F1)

- [ ] **Шаг 1: Прогнать все тесты feature F1 вместе.**
```
pytest tests/test_features.py tests/test_gen_dataset.py tests/test_train.py tests/test_classifier.py -v
```
Ожидаемо: все passed, `macro_f1 >= 0.7` в логе.

- [ ] **Шаг 2: Smoke-проверка артефактов для `/api/metrics` (F-API будет читать `metrics.json`).**
```
python -c "import json; m=json.load(open('app/model/artifacts/metrics.json',encoding='utf-8')); print('macro_f1=',m['macro_f1'],'n_train=',m['n_train'],'n_test=',m['n_test']); print('classes ok:', m['labels']==['gambling','pyramid','fraud','clean'])"
```
Ожидаемо: печатает `macro_f1=` (>=0.7), корректные счётчики и `classes ok: True`.

- [ ] **Шаг 3: Зафиксировать решение в командной памяти (Octarin).** Вызвать `memory_record_decision`: "F1 KÓZ: собственная модель = замороженные эмбеддинги MiniLM-L12-v2 (384) + 9 инженерных бинарных признаков -> sklearn LogisticRegression(C=4, class_weight=balanced); артефакты в app/model/artifacts/{clf.joblib,metrics.json}; risk=round(100*P(not clean)); top_features = активные признаки × коэффициенты класса с русским evidence", anchor repo=AFMHACKATHON, files=`app/model/features.py, app/model/train.py, app/model/classifier.py`. Это закрывает критерий №2 (модель своя, скоринг локальный) и фиксирует контракт для F-decision/F-API.

---

## F2: Мультимодальные экстракторы + live-fetch + кэш

**Цель:** реализовать слой извлечения признаков по всем модальностям (текст/entities, аудио, OCR, визуал), live-fetch медиа через yt-dlp и оркестратор `pipeline.extract()` с on-disk кэшем; тяжёлые модели лениво загружаются и деградируют до пустого результата, чтобы тесты и демо никогда от них не зависели.

**Зависит от:** F1 (нужны `app/models.py` с dataclasses `Entity`, `VisualConcept`, `Extracted`, `Post`; `app/db.py` со схемой `extracted` + helpers; `app/config.py` с путями). Если F1 ещё нет — заглушки нельзя; F2 стартует ПОСЛЕ F1.

---

### Задача 1: Нормализация текста + базовый каркас text.py

**Файлы:**
- Create: `app/extractors/text.py`
- Test: `tests/test_text_normalize.py`

- [ ] **Шаг 1: Написать падающий тест нормализации.**
```python
# tests/test_text_normalize.py
from app.extractors.text import normalize


def test_normalize_collapses_whitespace_and_strips():
    assert normalize("  Привет\n\n  мир\t! ") == "Привет мир !"


def test_normalize_lowercases_for_matching_but_keeps_text():
    # normalize не ломает кириллицу и не удаляет цифры/проценты
    assert normalize("Доход 30% в МЕСЯЦ") == "Доход 30% в МЕСЯЦ"


def test_normalize_handles_none_and_empty():
    assert normalize(None) == ""
    assert normalize("") == ""
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_text_normalize.py -v` Expected: `ModuleNotFoundError` / `ImportError: cannot import name 'normalize'`.
- [ ] **Шаг 3: Минимальная реализация `normalize`.**
```python
# app/extractors/text.py
import re

_WS_RE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """Схлопывает пробелы, обрезает края. Сохраняет регистр и кириллицу."""
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_text_normalize.py -v` Expected: `3 passed`.
- [ ] **Шаг 5: Коммит.**
```bash
git rev-parse --show-toplevel   # убедиться, что это репо AFMHACKATHON, а не C:\Users\adlet\.git
git add app/extractors/text.py tests/test_text_normalize.py
git commit -m "feat(extractors): add text normalize helper"
```

---

### Задача 2: extract_entities — regex-сущности (telegram/url/promo/phone/payout_promise и др.)

**Файлы:**
- Modify: `app/extractors/text.py`
- Test: `tests/test_extract_entities.py`

- [ ] **Шаг 1: Написать падающий тест (telegram + payout_promise обязательны по ТЗ).**
```python
# tests/test_extract_entities.py
from app.extractors.text import extract_entities
from app.models import Entity


def _types(ents):
    return {e.type for e in ents}


def test_finds_telegram_and_payout_promise():
    text = "Гарантированный доход 30% в месяц! Пиши в личку https://t.me/casino_win_bot"
    ents = extract_entities(text)
    assert isinstance(ents[0], Entity)
    assert "telegram" in _types(ents)
    assert "payout_promise" in _types(ents)


def test_finds_url_promo_phone_crypto_whatsapp_handle():
    text = (
        "Промокод BONUS500 на сайте http://1xstavka.ru "
        "звони +7 701 234 56 78, wa.me/77012345678 "
        "кошелёк bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq пиши @big_money"
    )
    ents = extract_entities(text)
    t = _types(ents)
    assert {"url", "promo_code", "phone", "whatsapp", "crypto_wallet", "handle"} <= t


def test_telegram_normalized_to_handle():
    ents = extract_entities("заходи https://t.me/Casino_Win_Bot")
    tg = [e for e in ents if e.type == "telegram"][0]
    assert tg.normalized == "casino_win_bot"
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_extract_entities.py -v` Expected: `ImportError: cannot import name 'extract_entities'`.
- [ ] **Шаг 3: Реализовать regex-извлечение сущностей.**
```python
# app/extractors/text.py  (добавить ниже normalize)
from app.models import Entity

_TELEGRAM_RE = re.compile(r"(?:https?://)?t\.me/(\w{3,})", re.IGNORECASE)
_WHATSAPP_RE = re.compile(r"(?:https?://)?(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\d{6,15})", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_PROMO_RE = re.compile(r"\b(?:промокод|promo|bonus|бонус)[\s:]*([A-Z0-9]{3,12})\b", re.IGNORECASE)
_CRYPTO_RE = re.compile(r"\b(?:bc1[a-z0-9]{20,}|0x[a-fA-F0-9]{40}|[13][a-km-zA-HJ-NP-Z1-9]{25,34}|T[A-Za-z0-9]{33})\b")
_PHONE_RE = re.compile(r"\+?7[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
_HANDLE_RE = re.compile(r"(?<![\w/.])@([A-Za-z0-9_]{3,32})")
_PAYOUT_RE = re.compile(
    r"(гарантированн\w*\s+доход|доход\s+\d{1,3}\s*%|\d{1,3}\s*%\s*(?:в\s+месяц|в\s+день|годовых)|"
    r"кепілдік\w*\s+табыс|табыс\s+\d{1,3}\s*%)",
    re.IGNORECASE,
)


def extract_entities(text: str) -> list[Entity]:
    """Извлекает все сущности из текста через regex + бренд-словарь (см. Задачу 3)."""
    text = normalize(text)
    ents: list[Entity] = []
    for m in _TELEGRAM_RE.finditer(text):
        h = m.group(1)
        ents.append(Entity(type="telegram", value=m.group(0), normalized=h.lower()))
    for m in _WHATSAPP_RE.finditer(text):
        ents.append(Entity(type="whatsapp", value=m.group(0), normalized=m.group(1)))
    for m in _URL_RE.finditer(text):
        u = m.group(0)
        if "t.me/" in u.lower() or "wa.me/" in u.lower():
            continue
        ents.append(Entity(type="url", value=u, normalized=u.lower().rstrip("/.,)")))
    for m in _PROMO_RE.finditer(text):
        ents.append(Entity(type="promo_code", value=m.group(0), normalized=m.group(1).upper()))
    for m in _CRYPTO_RE.finditer(text):
        ents.append(Entity(type="crypto_wallet", value=m.group(0), normalized=m.group(0)))
    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        ents.append(Entity(type="phone", value=m.group(0), normalized=digits))
    for m in _HANDLE_RE.finditer(text):
        ents.append(Entity(type="handle", value=m.group(0), normalized=m.group(1).lower()))
    for m in _PAYOUT_RE.finditer(text):
        ents.append(Entity(type="payout_promise", value=m.group(0).strip(), normalized=m.group(0).strip().lower()))
    return ents
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_extract_entities.py -v` Expected: `3 passed`.
- [ ] **Шаг 5: Коммит.**
```bash
git add app/extractors/text.py tests/test_extract_entities.py
git commit -m "feat(extractors): regex entity extraction for telegram/url/promo/phone/payout"
```

---

### Задача 3: Бренд-словарь — casino_brand / betting_brand

**Файлы:**
- Modify: `app/extractors/text.py`
- Test: `tests/test_brand_entities.py`

- [ ] **Шаг 1: Написать падающий тест.**
```python
# tests/test_brand_entities.py
from app.extractors.text import extract_entities


def test_detects_casino_brand():
    ents = extract_entities("Заносим в 1WIN и Vavada сегодня!")
    brands = {e.normalized for e in ents if e.type == "casino_brand"}
    assert "1win" in brands
    assert "vavada" in brands


def test_detects_betting_brand():
    ents = extract_entities("Ставки на 1xBet и Melbet")
    brands = {e.normalized for e in ents if e.type == "betting_brand"}
    assert "1xbet" in brands
    assert "melbet" in brands


def test_no_false_brand_on_clean_text():
    ents = extract_entities("Сегодня хорошая погода в Алматы")
    assert not [e for e in ents if e.type in ("casino_brand", "betting_brand")]
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_brand_entities.py -v` Expected: `AssertionError` (бренды не находятся).
- [ ] **Шаг 3: Добавить курируемые бренд-списки + матчинг в extract_entities.**
```python
# app/extractors/text.py  (добавить рядом с regex-константами)
CASINO_BRANDS = [
    "1win", "vavada", "pin-up", "pinup", "joycasino", "casinox", "riobet",
    "playfortuna", "sol casino", "drip casino", "izzi", "champion casino",
    "888 casino", "azino777", "azino", "vulkan", "vulcan",
]
BETTING_BRANDS = [
    "1xbet", "1xstavka", "melbet", "olimp", "olimpbet", "parimatch", "betcity",
    "fonbet", "leon", "marathonbet", "winline", "mostbet",
]


def _match_brands(text_lower: str, brands: list[str], etype: str) -> list[Entity]:
    found: list[Entity] = []
    for brand in brands:
        if re.search(r"(?<!\w)" + re.escape(brand) + r"(?!\w)", text_lower):
            found.append(Entity(type=etype, value=brand, normalized=brand.lower()))
    return found
```
Внутри `extract_entities`, перед `return ents`, добавить:
```python
    text_lower = text.lower()
    ents.extend(_match_brands(text_lower, CASINO_BRANDS, "casino_brand"))
    ents.extend(_match_brands(text_lower, BETTING_BRANDS, "betting_brand"))
    return ents
```
(заменить существующий `return ents` на этот блок).
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_brand_entities.py tests/test_extract_entities.py -v` Expected: все `passed` (регресс старого теста не сломан).
- [ ] **Шаг 5: Коммит.**
```bash
git add app/extractors/text.py tests/test_brand_entities.py
git commit -m "feat(extractors): curated casino/betting brand entity matching"
```

---

### Задача 4: audio.py — ленивый faster-whisper с graceful fallback

**Файлы:**
- Create: `app/extractors/audio.py`
- Test: `tests/test_audio_fallback.py`

- [ ] **Шаг 1: Написать падающий тест (модель не должна импортироваться при импорте модуля; fallback на "").**
```python
# tests/test_audio_fallback.py
import sys
import app.extractors.audio as audio


def test_module_import_does_not_load_whisper():
    # импорт модуля не должен подтягивать faster_whisper в sys.modules
    assert "faster_whisper" not in sys.modules


def test_transcribe_returns_empty_when_model_unavailable(monkeypatch):
    # эмулируем отсутствие модели -> graceful "" без исключения
    monkeypatch.setattr(audio, "_load_model", lambda: None)
    assert audio.transcribe("nonexistent.mp4") == ""


def test_transcribe_empty_path_returns_empty():
    assert audio.transcribe("") == ""
    assert audio.transcribe(None) == ""
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_audio_fallback.py -v` Expected: `ModuleNotFoundError: No module named 'app.extractors.audio'`.
- [ ] **Шаг 3: Реализовать ленивый transcribe.**
```python
# app/extractors/audio.py
"""Аудио -> текст через faster-whisper. Ленивая загрузка, мягкая деградация."""
from app.extractors.text import normalize

_MODEL = None
_LOAD_FAILED = False


def _load_model():
    """Лениво грузит WhisperModel (CPU). При любой ошибке возвращает None навсегда."""
    global _MODEL, _LOAD_FAILED
    if _MODEL is not None:
        return _MODEL
    if _LOAD_FAILED:
        return None
    try:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel("base", device="cpu", compute_type="int8")
        return _MODEL
    except Exception:
        _LOAD_FAILED = True
        return None


def transcribe(media_path: str | None) -> str:
    """Возвращает транскрипт речи или "" если модель/файл недоступны."""
    if not media_path:
        return ""
    model = _load_model()
    if model is None:
        return ""
    try:
        segments, _info = model.transcribe(media_path, language=None)
        return normalize(" ".join(seg.text for seg in segments))
    except Exception:
        return ""
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_audio_fallback.py -v` Expected: `3 passed`.
- [ ] **Шаг 5: Коммит.**
```bash
git add app/extractors/audio.py tests/test_audio_fallback.py
git commit -m "feat(extractors): lazy faster-whisper transcribe with graceful fallback"
```

---

### Задача 5: ocr.py — ленивый easyocr с graceful fallback

**Файлы:**
- Create: `app/extractors/ocr.py`
- Test: `tests/test_ocr_fallback.py`

- [ ] **Шаг 1: Написать падающий тест.**
```python
# tests/test_ocr_fallback.py
import sys
import app.extractors.ocr as ocr


def test_module_import_does_not_load_easyocr():
    assert "easyocr" not in sys.modules


def test_ocr_frames_returns_empty_when_reader_unavailable(monkeypatch):
    monkeypatch.setattr(ocr, "_load_reader", lambda: None)
    assert ocr.ocr_frames(["frame1.png", "frame2.png"]) == ""


def test_ocr_frames_empty_list_returns_empty():
    assert ocr.ocr_frames([]) == ""
    assert ocr.ocr_frames(None) == ""
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_ocr_fallback.py -v` Expected: `ModuleNotFoundError: No module named 'app.extractors.ocr'`.
- [ ] **Шаг 3: Реализовать ленивый ocr_frames (langs ru+en; kk недоступен в easyocr → покрывается ru).**
```python
# app/extractors/ocr.py
"""Текст с кадров через easyocr (RU+EN). Ленивая загрузка, мягкая деградация."""
from app.extractors.text import normalize

_READER = None
_LOAD_FAILED = False


def _load_reader():
    """Лениво грузит easyocr.Reader(['ru','en']). При ошибке -> None навсегда."""
    global _READER, _LOAD_FAILED
    if _READER is not None:
        return _READER
    if _LOAD_FAILED:
        return None
    try:
        import easyocr
        _READER = easyocr.Reader(["ru", "en"], gpu=False)
        return _READER
    except Exception:
        _LOAD_FAILED = True
        return None


def ocr_frames(frames: list | None) -> str:
    """Распознаёт текст на кадрах. Возвращает склейку или "" при недоступности."""
    if not frames:
        return ""
    reader = _load_reader()
    if reader is None:
        return ""
    chunks: list[str] = []
    for frame in frames:
        try:
            for line in reader.readtext(frame, detail=0):
                chunks.append(line)
        except Exception:
            continue
    return normalize(" ".join(chunks))
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_ocr_fallback.py -v` Expected: `3 passed`.
- [ ] **Шаг 5: Коммит.**
```bash
git add app/extractors/ocr.py tests/test_ocr_fallback.py
git commit -m "feat(extractors): lazy easyocr ocr_frames with graceful fallback"
```

---

### Задача 6: visual.py — ленивый open-clip zero-shot с graceful fallback

**Файлы:**
- Create: `app/extractors/visual.py`
- Test: `tests/test_visual_fallback.py`

- [ ] **Шаг 1: Написать падающий тест (fallback -> [], а не "").**
```python
# tests/test_visual_fallback.py
import sys
import app.extractors.visual as visual
from app.models import VisualConcept


def test_module_import_does_not_load_torch_or_openclip():
    assert "open_clip" not in sys.modules


def test_visual_concepts_returns_empty_list_when_model_unavailable(monkeypatch):
    monkeypatch.setattr(visual, "_load_model", lambda: (None, None, None))
    assert visual.visual_concepts(["frame.png"]) == []


def test_visual_concepts_empty_frames_returns_empty_list():
    assert visual.visual_concepts([]) == []
    assert visual.visual_concepts(None) == []


def test_concept_prompts_are_fixed_and_cover_required_labels():
    labels = {label for label, _prompt in visual.CONCEPT_PROMPTS}
    assert {"casino", "roulette", "betting_slip", "cash_flaunt", "luxury_car"} <= labels
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_visual_fallback.py -v` Expected: `ModuleNotFoundError: No module named 'app.extractors.visual'`.
- [ ] **Шаг 3: Реализовать ленивый visual_concepts с фиксированными промптами.**
```python
# app/extractors/visual.py
"""Визуальные концепты через open-clip zero-shot. Ленивая загрузка, мягкая деградация."""
from app.models import VisualConcept

# (label, текстовый промпт) — фиксированный набор концептов
CONCEPT_PROMPTS = [
    ("casino", "a casino interior with slot machines"),
    ("roulette", "a roulette wheel and casino table"),
    ("betting_slip", "a sports betting slip or bookmaker odds screen"),
    ("cash_flaunt", "a person flaunting stacks of cash money"),
    ("luxury_car", "an expensive luxury sports car"),
]
_SCORE_THRESHOLD = 0.30

_MODEL = None
_PREPROCESS = None
_TOKENIZER = None
_LOAD_FAILED = False


def _load_model():
    """Лениво грузит open-clip ViT-B-32. При ошибке -> (None, None, None) навсегда."""
    global _MODEL, _PREPROCESS, _TOKENIZER, _LOAD_FAILED
    if _MODEL is not None:
        return _MODEL, _PREPROCESS, _TOKENIZER
    if _LOAD_FAILED:
        return None, None, None
    try:
        import open_clip
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        model.eval()
        _MODEL, _PREPROCESS, _TOKENIZER = model, preprocess, open_clip.get_tokenizer("ViT-B-32")
        return _MODEL, _PREPROCESS, _TOKENIZER
    except Exception:
        _LOAD_FAILED = True
        return None, None, None


def visual_concepts(frames: list | None) -> list[VisualConcept]:
    """Zero-shot концепты по кадрам. Возвращает [] при недоступности модели."""
    if not frames:
        return []
    model, preprocess, tokenizer = _load_model()
    if model is None:
        return []
    try:
        import torch
        from PIL import Image
        text_tokens = tokenizer([p for _label, p in CONCEPT_PROMPTS])
        with torch.no_grad():
            text_features = model.encode_text(text_tokens)
            text_features /= text_features.norm(dim=-1, keepdim=True)
            best: dict[str, float] = {}
            for frame in frames:
                image = preprocess(Image.open(frame).convert("RGB")).unsqueeze(0)
                img_features = model.encode_image(image)
                img_features /= img_features.norm(dim=-1, keepdim=True)
                probs = (100.0 * img_features @ text_features.T).softmax(dim=-1)[0]
                for (label, _prompt), p in zip(CONCEPT_PROMPTS, probs.tolist()):
                    best[label] = max(best.get(label, 0.0), float(p))
        return [
            VisualConcept(label=label, score=round(score, 3))
            for label, score in best.items()
            if score >= _SCORE_THRESHOLD
        ]
    except Exception:
        return []
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_visual_fallback.py -v` Expected: `4 passed`.
- [ ] **Шаг 5: Коммит.**
```bash
git add app/extractors/visual.py tests/test_visual_fallback.py
git commit -m "feat(extractors): lazy open-clip zero-shot visual_concepts with graceful fallback"
```

---

### Задача 7: fetch.py — yt-dlp link fetch + handle_upload + сэмплинг кадров

**Файлы:**
- Create: `app/ingestion/fetch.py`
- Test: `tests/test_fetch.py`

- [ ] **Шаг 1: Написать падающий тест (yt-dlp ленив; handle_upload пишет файл; sample_frames без OpenCV -> []).**
```python
# tests/test_fetch.py
import sys
import app.ingestion.fetch as fetch


def test_module_import_does_not_load_yt_dlp():
    assert "yt_dlp" not in sys.modules


def test_handle_upload_writes_file_and_returns_media_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MEDIA_DIR", tmp_path)
    media_path, frames, meta = fetch.handle_upload(b"\x00\x01video-bytes", "clip.mp4")
    assert media_path.endswith(".mp4")
    import os
    assert os.path.exists(media_path)
    assert isinstance(frames, list)
    assert isinstance(meta, dict)


def test_fetch_link_returns_empty_media_on_yt_dlp_failure(monkeypatch):
    monkeypatch.setattr(fetch, "_ytdlp_download", lambda url: ("", {}))
    media_path, frames, meta = fetch.fetch_link("https://example.com/badvideo")
    assert media_path == ""
    assert frames == []
    assert meta.get("caption", "") == ""


def test_sample_frames_no_cv2_returns_empty(monkeypatch):
    monkeypatch.setattr(fetch, "_load_cv2", lambda: None)
    assert fetch.sample_frames("any.mp4") == []
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_fetch.py -v` Expected: `ModuleNotFoundError: No module named 'app.ingestion.fetch'`.
- [ ] **Шаг 3: Реализовать fetch.py.**
```python
# app/ingestion/fetch.py
"""Live-получение медиа: yt-dlp по ссылке + обработка загруженного файла.
Возвращает (media_path, frames, meta). Всё лениво и деградирует мягко."""
import os
import uuid

from app.config import MEDIA_DIR  # Path к каталогу медиа (из F1)

_FRAME_COUNT = 5


def _load_cv2():
    """Лениво грузит cv2. При недоступности -> None."""
    try:
        import cv2
        return cv2
    except Exception:
        return None


def sample_frames(media_path: str) -> list:
    """Возвращает до _FRAME_COUNT путей-кадров (PNG). [] если cv2/видео недоступны."""
    if not media_path or not os.path.exists(media_path):
        return []
    cv2 = _load_cv2()
    if cv2 is None:
        return []
    try:
        cap = cv2.VideoCapture(media_path)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        if total <= 0:
            cap.release()
            return []
        step = max(total // _FRAME_COUNT, 1)
        frames: list[str] = []
        base = os.path.splitext(media_path)[0]
        for i in range(_FRAME_COUNT):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i * step)
            ok, img = cap.read()
            if not ok:
                break
            out = f"{base}_frame{i}.png"
            cv2.imwrite(out, img)
            frames.append(out)
        cap.release()
        return frames
    except Exception:
        return []


def _ytdlp_download(url: str):
    """Лениво грузит yt-dlp и скачивает медиа. -> (media_path, meta) или ("", {})."""
    try:
        import yt_dlp
        os.makedirs(MEDIA_DIR, exist_ok=True)
        out_tmpl = os.path.join(str(MEDIA_DIR), f"{uuid.uuid4().hex}.%(ext)s")
        opts = {"outtmpl": out_tmpl, "quiet": True, "noplaylist": True, "format": "mp4/best"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            media_path = ydl.prepare_filename(info)
        meta = {
            "caption": info.get("description") or info.get("title") or "",
            "author_handle": info.get("uploader") or info.get("channel") or "",
            "platform": (info.get("extractor_key") or "").lower(),
            "thumb_url": info.get("thumbnail") or "",
        }
        return media_path, meta
    except Exception:
        return "", {}


def fetch_link(url: str):
    """Скачивает медиа по ссылке, сэмплит кадры. -> (media_path, frames, meta)."""
    media_path, meta = _ytdlp_download(url)
    if not media_path:
        return "", [], {"caption": "", "author_handle": "", "platform": "", "thumb_url": ""}
    frames = sample_frames(media_path)
    meta.setdefault("caption", "")
    meta["url"] = url
    return media_path, frames, meta


def handle_upload(file_bytes: bytes, filename: str):
    """Сохраняет загруженный файл, сэмплит кадры. -> (media_path, frames, meta)."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    ext = os.path.splitext(filename)[1] or ".mp4"
    media_path = os.path.join(str(MEDIA_DIR), f"{uuid.uuid4().hex}{ext}")
    with open(media_path, "wb") as f:
        f.write(file_bytes)
    frames = sample_frames(media_path)
    meta = {"caption": "", "author_handle": "", "platform": "", "thumb_url": ""}
    return media_path, frames, meta
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_fetch.py -v` Expected: `4 passed`.
- [ ] **Шаг 5: Коммит.**
```bash
git add app/ingestion/fetch.py tests/test_fetch.py
git commit -m "feat(ingestion): yt-dlp link fetch + upload handling + frame sampling"
```

---

### Задача 8: pipeline.extract — combined_text + порядок склейки + entity extraction

**Файлы:**
- Create: `app/extractors/pipeline.py`
- Test: `tests/test_pipeline_combined.py`

- [ ] **Шаг 1: Написать падающий тест (порядок склейки combined_text + наличие сущностей).**
```python
# tests/test_pipeline_combined.py
from app.extractors.pipeline import build_combined_text, _build_extracted_from_parts


def test_combined_text_concatenation_order():
    # порядок: caption, transcript, ocr_text, затем labels визуал-концептов
    combined = build_combined_text(
        caption="кэпшн",
        transcript="транскрипт",
        ocr_text="окр",
        visual_labels=["casino", "roulette"],
    )
    assert combined == "кэпшн транскрипт окр casino roulette"


def test_combined_text_skips_empty_parts():
    combined = build_combined_text(
        caption="кэпшн", transcript="", ocr_text="окр", visual_labels=[]
    )
    assert combined == "кэпшн окр"


def test_build_extracted_runs_entity_extraction():
    ex = _build_extracted_from_parts(
        post_id="p1",
        caption="Пиши https://t.me/win_bot, доход 30% в месяц",
        transcript="",
        ocr_text="",
        visual_concepts=[],
    )
    types = {e.type for e in ex.entities}
    assert "telegram" in types
    assert "payout_promise" in types
    assert ex.combined_text == "Пиши https://t.me/win_bot, доход 30% в месяц"
    assert ex.post_id == "p1"
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_pipeline_combined.py -v` Expected: `ModuleNotFoundError: No module named 'app.extractors.pipeline'`.
- [ ] **Шаг 3: Реализовать pipeline-хелперы (без кэша/тяжёлых моделей пока — только склейка + entities).**
```python
# app/extractors/pipeline.py
"""Оркестратор экстракторов. Кэш по post_id; ленивые тяжёлые модели."""
from app.models import Extracted, VisualConcept
from app.extractors.text import extract_entities, normalize


def build_combined_text(
    caption: str, transcript: str, ocr_text: str, visual_labels: list[str]
) -> str:
    """Склейка модальностей в фикс. порядке: caption, transcript, ocr_text, labels."""
    parts: list[str] = []
    for p in (caption, transcript, ocr_text):
        p = normalize(p)
        if p:
            parts.append(p)
    parts.extend(label for label in visual_labels if label)
    return " ".join(parts)


def _build_extracted_from_parts(
    post_id: str,
    caption: str,
    transcript: str,
    ocr_text: str,
    visual_concepts: list[VisualConcept],
) -> Extracted:
    """Собирает Extracted: combined_text + entity extraction по combined_text."""
    caption = normalize(caption)
    transcript = normalize(transcript)
    ocr_text = normalize(ocr_text)
    combined = build_combined_text(
        caption, transcript, ocr_text, [vc.label for vc in visual_concepts]
    )
    return Extracted(
        post_id=post_id,
        caption=caption,
        transcript=transcript,
        ocr_text=ocr_text,
        visual_concepts=visual_concepts,
        combined_text=combined,
        entities=extract_entities(combined),
    )
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_pipeline_combined.py -v` Expected: `3 passed`.
- [ ] **Шаг 5: Коммит.**
```bash
git add app/extractors/pipeline.py tests/test_pipeline_combined.py
git commit -m "feat(extractors): pipeline combined_text builder + entity extraction"
```

---

### Задача 9: pipeline.extract — кэш-путь (НЕ грузит тяжёлые модели) + live-путь + персист

**Файлы:**
- Modify: `app/extractors/pipeline.py`
- Test: `tests/test_pipeline_cache.py`

- [ ] **Шаг 1: Написать падающий тест (кэш отдаёт Extracted БЕЗ загрузки тяжёлых моделей).**
```python
# tests/test_pipeline_cache.py
import app.extractors.audio as audio
import app.extractors.ocr as ocr
import app.extractors.visual as visual
from app.extractors import pipeline
from app.models import Post, Extracted


def _post(pid="cached1"):
    return Post(
        id=pid, platform="tiktok", author_handle="@x", url="http://x",
        caption="доход 50% в месяц", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="seed",
    )


def test_cached_extract_does_not_invoke_heavy_models(monkeypatch):
    # любой вызов тяжёлой модели -> падение теста
    def boom(*a, **k):
        raise AssertionError("heavy model loaded on cache hit")
    monkeypatch.setattr(audio, "_load_model", boom)
    monkeypatch.setattr(ocr, "_load_reader", boom)
    monkeypatch.setattr(visual, "_load_model", boom)

    cached = Extracted(
        post_id="cached1", caption="доход 50% в месяц", transcript="",
        ocr_text="", visual_concepts=[], combined_text="доход 50% в месяц",
        entities=[],
    )
    monkeypatch.setattr(pipeline, "_load_cached", lambda pid: cached)

    result = pipeline.extract(_post("cached1"), use_cache=True)
    assert result is cached
    assert result.combined_text == "доход 50% в месяц"


def test_use_cache_false_skips_cache_lookup(monkeypatch):
    called = {"cache": False}
    def fake_cache(pid):
        called["cache"] = True
        return None
    monkeypatch.setattr(pipeline, "_load_cached", fake_cache)
    # подменяем тяжёлый live-разбор на лёгкий стаб
    monkeypatch.setattr(pipeline, "_run_live", lambda post: Extracted(
        post_id=post.id, caption=post.caption, transcript="", ocr_text="",
        visual_concepts=[], combined_text=post.caption, entities=[]))
    monkeypatch.setattr(pipeline, "_persist", lambda ex: None)
    pipeline.extract(_post("live1"), use_cache=False)
    assert called["cache"] is False
```
- [ ] **Шаг 2: Запустить (ожидаем провал).** Run: `pytest tests/test_pipeline_cache.py -v` Expected: `AttributeError: module 'app.extractors.pipeline' has no attribute 'extract'`.
- [ ] **Шаг 3: Дописать кэш/live/persist + `extract` в pipeline.py.**
```python
# app/extractors/pipeline.py  (добавить импорты вверху)
import json
import os
from app.config import CACHE_DIR  # Path к каталогу кэша (из F1)
from app.db import get_extracted, upsert_extracted  # helpers из F1
from app.extractors.audio import transcribe
from app.extractors.ocr import ocr_frames
from app.extractors.visual import visual_concepts
from app.ingestion.fetch import sample_frames
```
```python
# app/extractors/pipeline.py  (добавить функции в конец файла)
def _load_cached(post_id: str) -> Extracted | None:
    """Сначала on-disk JSON-кэш, затем таблица extracted (из demo-сборки)."""
    cache_file = os.path.join(str(CACHE_DIR), f"{post_id}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                d = json.load(f)
            return Extracted(
                post_id=d["post_id"], caption=d["caption"], transcript=d["transcript"],
                ocr_text=d["ocr_text"],
                visual_concepts=[VisualConcept(**vc) for vc in d["visual_concepts"]],
                combined_text=d["combined_text"],
                entities=[__import__("app.models", fromlist=["Entity"]).Entity(**e)
                          for e in d["entities"]],
            )
        except Exception:
            pass
    return get_extracted(post_id)  # None если в БД нет


def _run_live(post: Post) -> Extracted:
    """Полный мультимодальный разбор для live-постов (тяжёлый путь)."""
    frames = sample_frames(post.media_path) if post.media_path else []
    transcript = transcribe(post.media_path) if post.media_path else ""
    ocr_text = ocr_frames(frames)
    vconcepts = visual_concepts(frames)
    return _build_extracted_from_parts(
        post_id=post.id, caption=post.caption, transcript=transcript,
        ocr_text=ocr_text, visual_concepts=vconcepts,
    )


def _persist(ex: Extracted) -> None:
    """Пишет Extracted в таблицу extracted и в on-disk JSON-кэш."""
    upsert_extracted(ex)
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(str(CACHE_DIR), f"{ex.post_id}.json")
    payload = {
        "post_id": ex.post_id, "caption": ex.caption, "transcript": ex.transcript,
        "ocr_text": ex.ocr_text,
        "visual_concepts": [{"label": vc.label, "score": vc.score} for vc in ex.visual_concepts],
        "combined_text": ex.combined_text,
        "entities": [{"type": e.type, "value": e.value, "normalized": e.normalized}
                     for e in ex.entities],
    }
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def extract(post: Post, use_cache: bool = True) -> Extracted:
    """Главная точка входа. Кэш -> мгновенно; иначе полный разбор + персист."""
    if use_cache:
        cached = _load_cached(post.id)
        if cached is not None:
            return cached
    ex = _run_live(post)
    _persist(ex)
    return ex
```
- [ ] **Шаг 4: Запустить (ожидаем успех).** Run: `pytest tests/test_pipeline_cache.py -v` Expected: `2 passed`.
- [ ] **Шаг 5: Прогнать весь F2-набор (регресс).** Run: `pytest tests/test_text_normalize.py tests/test_extract_entities.py tests/test_brand_entities.py tests/test_audio_fallback.py tests/test_ocr_fallback.py tests/test_visual_fallback.py tests/test_fetch.py tests/test_pipeline_combined.py tests/test_pipeline_cache.py -v` Expected: все `passed`.
- [ ] **Шаг 6: Коммит.**
```bash
git add app/extractors/pipeline.py tests/test_pipeline_cache.py
git commit -m "feat(extractors): pipeline.extract cache-first orchestration with lazy live path"
```

---

**Примечание по контракту:** `app/config.py` должен экспортировать `MEDIA_DIR` и `CACHE_DIR` (Path-объекты), а `app/db.py` — helpers `get_extracted(post_id) -> Extracted | None` и `upsert_extracted(ex: Extracted) -> None` (сериализация `visual_concepts_json` / `entities_json`). Это зона ответственности F1 — если в F1 имена иные, синхронизировать импорты в Задачах 7 и 9, не вводя альтернативных имён.

---

## F3: Decision — скоринг + объяснение + рекомендация + аудит

**Цель:** превратить выход модели в финальный `Score` с `recommended_action`, сохранить его в БД и аудит-лог, сгенерировать человекочитаемое объяснение на русском и отдать всё через `GET /api/post/{id}`.

**Зависит от:** F0 (app/models.py — `Post`, `Extracted`, `Entity`, `Score`, `FeatureHit`, `AuditEntry`; app/db.py — таблицы `scores`, `audit` + хелперы; app/config.py — `REVIEW_THRESHOLD`, `ESCALATE_THRESHOLD`), F1 (app/model/classifier.py — `RiskClassifier.load()` / `.predict()`), F2 (app/extractors — `Extracted` + `entities`).

---

### Задача 1: Вывод recommended_action из порогов риска

**Файлы:**
- Create: `app/decision/__init__.py` (пустой)
- Create: `tests/decision/__init__.py` (пустой)
- Create: `app/decision/scoring.py`
- Test: `tests/decision/test_scoring.py`

- [ ] **Шаг 1: Создать пустые `__init__.py`.**
```bash
touch app/decision/__init__.py tests/decision/__init__.py
```

- [ ] **Шаг 2: Написать падающий тест на маппинг risk -> recommended_action.** Пороги из контракта: `REVIEW_THRESHOLD=40`, `ESCALATE_THRESHOLD=70`. Правило: `risk < REVIEW_THRESHOLD -> "auto_clear"`; `REVIEW_THRESHOLD <= risk < ESCALATE_THRESHOLD -> "review"`; `risk >= ESCALATE_THRESHOLD -> "escalate"`. (Значение `"monitor"` зарезервировано в контракте, но в MVP-маппинге не используется — оставляем три ветки.)
```python
# tests/decision/test_scoring.py
from app.decision.scoring import recommend_action


def test_recommend_action_auto_clear_below_review():
    assert recommend_action(0) == "auto_clear"
    assert recommend_action(39) == "auto_clear"


def test_recommend_action_review_in_middle_band():
    assert recommend_action(40) == "review"
    assert recommend_action(69) == "review"


def test_recommend_action_escalate_at_or_above_escalate():
    assert recommend_action(70) == "escalate"
    assert recommend_action(100) == "escalate"
```

- [ ] **Шаг 3: Запустить — ожидать FAIL (ImportError).**
```bash
pytest tests/decision/test_scoring.py::test_recommend_action_auto_clear_below_review -v
# Expected: ERROR/FAIL — cannot import name 'recommend_action'
```

- [ ] **Шаг 4: Минимальная реализация `recommend_action`.**
```python
# app/decision/scoring.py
from app.config import REVIEW_THRESHOLD, ESCALATE_THRESHOLD


def recommend_action(risk: int) -> str:
    if risk >= ESCALATE_THRESHOLD:
        return "escalate"
    if risk >= REVIEW_THRESHOLD:
        return "review"
    return "auto_clear"
```

- [ ] **Шаг 5: Запустить — ожидать PASS.**
```bash
pytest tests/decision/test_scoring.py -v
# Expected: 3 passed
```

- [ ] **Шаг 6: Коммит.**
```bash
git rev-parse --show-toplevel   # MUST be the AFMHACKATHON project repo, NOT C:/Users/adlet
git add app/decision/__init__.py tests/decision/__init__.py app/decision/scoring.py tests/decision/test_scoring.py
git commit -m "feat(decision): map risk to recommended_action via thresholds"
```

---

### Задача 2: score_post — вызов классификатора, персист скора, аудит

**Файлы:**
- Modify: `app/decision/scoring.py`
- Test: `tests/decision/test_score_post.py`

- [ ] **Шаг 1: Написать падающий тест.** Высокорисковый `Extracted` (промис дохода + бренд казино) → классификатор вернёт `category` из {`gambling`,`pyramid`} и высокий `risk`, значит `recommended_action == "escalate"`; в `scores` появится строка с `recommended_action`; в `audit` появится строка `action == "scored"`, `post_id` совпадает. Чистый `Extracted` → `recommended_action == "auto_clear"` и `risk < REVIEW_THRESHOLD`. Используем монки-патч `RiskClassifier.load`, чтобы тест Decision не зависел от обученного артефакта F1.
```python
# tests/decision/test_score_post.py
import pytest
from app import db
from app.config import REVIEW_THRESHOLD
from app.models import Post, Extracted, Entity, Score, FeatureHit
from app.decision import scoring


class _FakeClassifier:
    def __init__(self, score: Score):
        self._score = score

    @classmethod
    def for_score(cls, score):
        inst = cls(score)
        return inst

    def predict(self, extracted: Extracted) -> Score:
        # echo a fixed score but bind it to the real post_id
        return Score(
            post_id=extracted.post_id,
            risk=self._score.risk,
            category=self._score.category,
            class_probs=self._score.class_probs,
            top_features=self._score.top_features,
        )


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    return dbfile


def _post(pid="p1"):
    return Post(
        id=pid, platform="tiktok", author_handle="@scammer",
        url="https://x/y", caption="cap", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="seed",
    )


def _extracted(pid="p1", text="text"):
    return Extracted(
        post_id=pid, caption=text, transcript="", ocr_text="",
        visual_concepts=[], combined_text=text, entities=[],
    )


def test_high_risk_extracted_escalates_and_persists(fresh_db, monkeypatch):
    high = Score(
        post_id="p1", risk=88, category="gambling",
        class_probs={"gambling": 0.9, "pyramid": 0.05, "fraud": 0.03, "clean": 0.02},
        top_features=[FeatureHit(feature="casino_brand", weight=0.6, evidence="1xBet")],
    )
    monkeypatch.setattr(
        scoring.RiskClassifier, "load",
        classmethod(lambda cls, path=None: _FakeClassifier.for_score(high)),
    )
    db.upsert_post(_post("p1"))
    result = scoring.score_post(_post("p1"), _extracted("p1"))
    assert result.category in {"gambling", "pyramid"}
    assert scoring.recommend_action(result.risk) == "escalate"
    row = db.get_score("p1")
    assert row is not None
    assert row.risk == 88
    assert row.category == "gambling"


def test_clean_extracted_auto_clears(fresh_db, monkeypatch):
    clean = Score(
        post_id="p2", risk=12, category="clean",
        class_probs={"gambling": 0.02, "pyramid": 0.02, "fraud": 0.04, "clean": 0.92},
        top_features=[],
    )
    monkeypatch.setattr(
        scoring.RiskClassifier, "load",
        classmethod(lambda cls, path=None: _FakeClassifier.for_score(clean)),
    )
    db.upsert_post(_post("p2"))
    result = scoring.score_post(_post("p2"), _extracted("p2", "обычный пост"))
    assert result.risk < REVIEW_THRESHOLD
    assert scoring.recommend_action(result.risk) == "auto_clear"


def test_audit_row_written(fresh_db, monkeypatch):
    high = Score(
        post_id="p3", risk=80, category="pyramid",
        class_probs={"gambling": 0.1, "pyramid": 0.8, "fraud": 0.05, "clean": 0.05},
        top_features=[FeatureHit(feature="payout_promise", weight=0.7, evidence="30% в месяц")],
    )
    monkeypatch.setattr(
        scoring.RiskClassifier, "load",
        classmethod(lambda cls, path=None: _FakeClassifier.for_score(high)),
    )
    db.upsert_post(_post("p3"))
    scoring.score_post(_post("p3"), _extracted("p3"))
    audit = db.get_audit("p3")
    assert len(audit) >= 1
    entry = audit[0]
    assert entry.post_id == "p3"
    assert entry.action == "scored"
    assert entry.actor == "system"
```

- [ ] **Шаг 2: Запустить — ожидать FAIL (нет `score_post` / нет `RiskClassifier` в модуле).**
```bash
pytest tests/decision/test_score_post.py -v
# Expected: FAIL — AttributeError: module 'app.decision.scoring' has no attribute 'score_post'
```

- [ ] **Шаг 3: Реализовать `score_post`.** Загружает (кэшированно) классификатор, предсказывает `Score`, вычисляет `recommended_action`, персистит через `db.upsert_score`, пишет `AuditEntry` через `db.insert_audit`. `RiskClassifier` импортируется в модуль на верхнем уровне, чтобы тест мог монки-патчить `scoring.RiskClassifier`.
```python
# app/decision/scoring.py
from datetime import datetime, timezone

from app import db
from app.config import REVIEW_THRESHOLD, ESCALATE_THRESHOLD
from app.models import Post, Extracted, Score, AuditEntry
from app.model.classifier import RiskClassifier

_clf_cache: RiskClassifier | None = None


def recommend_action(risk: int) -> str:
    if risk >= ESCALATE_THRESHOLD:
        return "escalate"
    if risk >= REVIEW_THRESHOLD:
        return "review"
    return "auto_clear"


def _get_classifier() -> RiskClassifier:
    global _clf_cache
    if _clf_cache is None:
        _clf_cache = RiskClassifier.load()
    return _clf_cache


def score_post(post: Post, extracted: Extracted) -> Score:
    clf = _get_classifier()
    score = clf.predict(extracted)
    action = recommend_action(score.risk)
    db.upsert_score(score, recommended_action=action)
    db.insert_audit(AuditEntry(
        ts=datetime.now(timezone.utc).isoformat(),
        post_id=post.id,
        action="scored",
        actor="system",
        detail=f"risk={score.risk} category={score.category} action={action}",
    ))
    return score
```
> Примечание: `db.upsert_score`, `db.get_score`, `db.insert_audit`, `db.get_audit`, `db.upsert_post`, `db.init_db`, `db.DB_PATH` — из контракта F0. Если в F0 сигнатура `upsert_score` не принимает `recommended_action` отдельным параметром (а берёт его из поля), согласовать с F0; здесь предполагаем `recommended_action` как явный аргумент, т.к. его нет в dataclass `Score`, но есть колонка `scores.recommended_action`.

- [ ] **Шаг 4: Запустить — ожидать PASS.**
```bash
pytest tests/decision/test_score_post.py -v
# Expected: 3 passed
```

- [ ] **Шаг 5: Коммит.**
```bash
git add app/decision/scoring.py tests/decision/test_score_post.py
git commit -m "feat(decision): score_post predicts, persists score, writes audit"
```

---

### Задача 3: explain — русские причины из top_features + сущностей

**Файлы:**
- Create: `app/decision/explain.py`
- Test: `tests/decision/test_explain.py`

- [ ] **Шаг 1: Написать падающий тест.** Высокорисковый случай (`top_features` с `payout_promise` + `casino_brand`, сущности `casino_brand`/`betting_brand`/`telegram`) → `>= 2` русских буллета. Чистый случай (нет фич, нет сущностей) → пустой список. Буллеты — строки на русском вида «Обещание дохода: ‘30% в месяц’», «Упоминание букмекера: 1xBet», «Призыв в Telegram: t.me/...».
```python
# tests/decision/test_explain.py
from app.models import Score, FeatureHit, Entity
from app.decision.explain import explain


def test_high_risk_yields_at_least_two_russian_bullets():
    score = Score(
        post_id="p1", risk=85, category="gambling",
        class_probs={"gambling": 0.9, "pyramid": 0.04, "fraud": 0.03, "clean": 0.03},
        top_features=[
            FeatureHit(feature="payout_promise", weight=0.6, evidence="30% в месяц"),
            FeatureHit(feature="casino_brand", weight=0.5, evidence="1xBet"),
        ],
    )
    entities = [
        Entity(type="betting_brand", value="1xBet", normalized="1xbet"),
        Entity(type="telegram", value="t.me/scamchat", normalized="t.me/scamchat"),
    ]
    bullets = explain(score, entities)
    assert isinstance(bullets, list)
    assert len(bullets) >= 2
    assert all(isinstance(b, str) and b.strip() for b in bullets)
    joined = " ".join(bullets)
    assert "30% в месяц" in joined
    assert "1xBet" in joined
    assert "t.me/scamchat" in joined


def test_clean_yields_no_bullets():
    score = Score(
        post_id="p2", risk=10, category="clean",
        class_probs={"gambling": 0.02, "pyramid": 0.02, "fraud": 0.04, "clean": 0.92},
        top_features=[],
    )
    bullets = explain(score, [])
    assert bullets == []
```

- [ ] **Шаг 2: Запустить — ожидать FAIL (ImportError).**
```bash
pytest tests/decision/test_explain.py -v
# Expected: ERROR — cannot import name 'explain'
```

- [ ] **Шаг 3: Реализовать `explain`.** Русские шаблоны по типу фичи и по типу сущности; дедуп по тексту буллета с сохранением порядка.
```python
# app/decision/explain.py
from app.models import Score, Entity

# Русские шаблоны для инженерных признаков модели (FeatureHit.feature)
_FEATURE_LABELS = {
    "payout_promise": "Обещание дохода",
    "casino_brand": "Упоминание казино",
    "betting_brand": "Упоминание букмекера",
    "telegram": "Призыв в Telegram",
    "whatsapp": "Призыв в WhatsApp",
    "promo_code": "Промокод",
    "crypto_wallet": "Крипто-кошелёк / реквизиты",
    "referral": "Реферальная схема",
    "urgency": "Срочность / давление",
}

# Русские шаблоны для извлечённых сущностей (Entity.type)
_ENTITY_LABELS = {
    "casino_brand": "Упоминание казино",
    "betting_brand": "Упоминание букмекера",
    "telegram": "Призыв в Telegram",
    "whatsapp": "Призыв в WhatsApp",
    "promo_code": "Промокод",
    "crypto_wallet": "Крипто-кошелёк / реквизиты",
    "payout_promise": "Обещание дохода",
}


def explain(score: Score, entities: list[Entity]) -> list[str]:
    bullets: list[str] = []
    seen: set[str] = set()

    def add(label: str, value: str) -> None:
        text = f"{label}: ‘{value}’"
        if text not in seen:
            seen.add(text)
            bullets.append(text)

    for fh in score.top_features:
        label = _FEATURE_LABELS.get(fh.feature)
        if label and fh.evidence:
            add(label, fh.evidence)

    for ent in entities:
        label = _ENTITY_LABELS.get(ent.type)
        if label:
            add(label, ent.value)

    return bullets
```

- [ ] **Шаг 4: Запустить — ожидать PASS.**
```bash
pytest tests/decision/test_explain.py -v
# Expected: 2 passed
```

- [ ] **Шаг 5: Коммит.**
```bash
git add app/decision/explain.py tests/decision/test_explain.py
git commit -m "feat(decision): explain renders Russian reasons from features and entities"
```

---

### Задача 4: GET /api/post/{id} отдаёт explanation + recommended_action

**Файлы:**
- Modify: `app/main.py`
- Test: `tests/test_api_post.py`

- [ ] **Шаг 1: Написать падающий тест через FastAPI TestClient.** Сидим пост + extracted + score (с `recommended_action`) в БД, дёргаем `GET /api/post/{id}`, проверяем что в ответе есть ключи `post`, `extracted`, `score`, `explanation` (список), `recommended_action` (одно из 4 значений). Для 404 — несуществующий id.
```python
# tests/test_api_post.py
import pytest
from fastapi.testclient import TestClient
from app import db
from app.models import Post, Extracted, Score, FeatureHit, Entity


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbfile = tmp_path / "api.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    from app.main import app
    return TestClient(app)


def _seed():
    db.upsert_post(Post(
        id="px", platform="telegram", author_handle="@x", url="https://t/x",
        caption="инвестируй 30% в месяц", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="seed",
    ))
    db.upsert_extracted(Extracted(
        post_id="px", caption="инвестируй 30% в месяц", transcript="",
        ocr_text="", visual_concepts=[], combined_text="инвестируй 30% в месяц",
        entities=[Entity(type="telegram", value="t.me/x", normalized="t.me/x")],
    ))
    db.upsert_score(
        Score(
            post_id="px", risk=82, category="pyramid",
            class_probs={"gambling": 0.05, "pyramid": 0.85, "fraud": 0.05, "clean": 0.05},
            top_features=[FeatureHit(feature="payout_promise", weight=0.7, evidence="30% в месяц")],
        ),
        recommended_action="escalate",
    )


def test_get_post_includes_explanation_and_action(client):
    _seed()
    resp = client.get("/api/post/px")
    assert resp.status_code == 200
    body = resp.json()
    assert body["recommended_action"] == "escalate"
    assert isinstance(body["explanation"], list)
    assert len(body["explanation"]) >= 1
    assert body["post"]["id"] == "px"
    assert body["score"]["risk"] == 82


def test_get_post_unknown_returns_404(client):
    resp = client.get("/api/post/nope")
    assert resp.status_code == 404
```

- [ ] **Шаг 2: Запустить — ожидать FAIL.**
```bash
pytest tests/test_api_post.py -v
# Expected: FAIL — route returns 404 always / missing keys explanation,recommended_action
```

- [ ] **Шаг 3: Реализовать/дополнить роут в `app/main.py`.** Читает пост/extracted/score из БД, берёт `recommended_action` из таблицы `scores`, строит `explanation` через `explain(score, extracted.entities)`. 404 если поста нет.
```python
# app/main.py  (добавить импорты и роут)
from fastapi import FastAPI, HTTPException
from dataclasses import asdict

from app import db
from app.decision.explain import explain

app = FastAPI(title="КӨЗ — AI Media Watch")


@app.get("/api/post/{post_id}")
def get_post(post_id: str):
    post = db.get_post(post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="пост не найден")
    extracted = db.get_extracted(post_id)
    score = db.get_score(post_id)
    recommended_action = db.get_recommended_action(post_id)
    explanation = []
    if score is not None and extracted is not None:
        explanation = explain(score, extracted.entities)
    return {
        "post": asdict(post),
        "extracted": asdict(extracted) if extracted is not None else None,
        "score": asdict(score) if score is not None else None,
        "explanation": explanation,
        "recommended_action": recommended_action,
    }
```
> Примечание: `db.get_post`, `db.get_extracted`, `db.get_score`, `db.get_recommended_action`, `db.upsert_extracted`, `db.upsert_score(score, recommended_action=...)` — из F0. Если F0 хранит `recommended_action` внутри возврата `get_score`, заменить `db.get_recommended_action(post_id)` на чтение поля из строки `scores` — согласовать с F0 на этапе интеграции.

- [ ] **Шаг 4: Запустить — ожидать PASS.**
```bash
pytest tests/test_api_post.py -v
# Expected: 2 passed
```

- [ ] **Шаг 5: Ручная проверка через curl (опционально, на запущенном сервере).**
```bash
# Run: uvicorn app.main:app --port 8000  (в отдельном окне, БД с засиженным постом px)
curl -s http://127.0.0.1:8000/api/post/px | python -m json.tool
# Expected: JSON с ключами post/extracted/score/explanation/recommended_action;
#           explanation — непустой список русских строк; recommended_action == "escalate"
```

- [ ] **Шаг 6: Коммит.**
```bash
git add app/main.py tests/test_api_post.py
git commit -m "feat(api): /api/post/{id} returns explanation and recommended_action"
```

---

### Задача 5: Прогон всего модуля Decision

**Файлы:**
- Test: `tests/decision/` + `tests/test_api_post.py`

- [ ] **Шаг 1: Запустить весь набор Decision и API-теста, убедиться что зелёно.**
```bash
pytest tests/decision tests/test_api_post.py -v
# Expected: все тесты passed (8 шт.: 3+3+2 unit + 2 api = соответствует написанным)
```

- [ ] **Шаг 2: Если есть провалы из-за рассинхрона с F0/F1 — починить только сигнатуры db.* (см. примечания), не менять логику score_post/explain.** Повторить прогон до зелёного.

---

## F4: Аналитическая консоль (лента/очередь, drill-down, live-проверка, тикер)

**Цель:** Собрать «звезду демо» — SOC-консоль на FastAPI + Alpine: live-лента приоритетной очереди постов с риск-цветом, drill-down с доказательствами по всем модальностям и объяснением, фоновый тикер для симуляции потока и live-проверка по ссылке/файлу. Бэкенд-маршруты покрываем TDD через `TestClient`, фронт — документированными ручными проверками.

**Зависит от:** F0 (каркас `app/main.py`, `app/config.py`, `app/db.py`, `app/models.py`, статика `/web`), F3 (`app/decision/scoring.py::score_post`, `app/decision/explain.py::explain`); использует F1 (`app/extractors/pipeline.py::extract`, `app/ingestion/seed.py`, `app/ingestion/fetch.py`) и F2 (`app/model/classifier.py::RiskClassifier`). Маршруты `/api/graph`, `/api/trends`, `/api/report/{id}.pdf`, `/api/metrics` реализуются в своих фичах; здесь делаем `/api/feed`, `/api/post/{id}`, `/api/analyze`, `/api/tick` и фронтенд.

---

### Задача 1: GET /api/feed — приоритетная очередь только из revealed постов, сортировка по риску

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\main.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_feed.py`

- [ ] **Шаг 1: Написать падающий тест.** Создать `tests/test_feed.py`. Тест засевает БД двумя revealed постами (risk 85 и 30) и одним нераскрытым (revealed=0, risk 95), затем проверяет, что `/api/feed` отдаёт только два revealed в порядке убывания риска.

```python
import sqlite3
from fastapi.testclient import TestClient
from app.main import app
from app import db


def _seed(monkeypatch, tmp_path):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    conn = db.get_conn()
    cur = conn.cursor()
    for pid, risk, revealed in [("p1", 85, 1), ("p2", 30, 1), ("p3", 95, 0)]:
        cur.execute(
            "INSERT INTO posts (id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source, revealed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, "tiktok", "@a", "http://x", "cap", "2026-06-24T10:00:00", None, None, "seed", revealed),
        )
        cur.execute(
            "INSERT INTO scores (post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, risk, "gambling", "{}", "[]", "review", "2026-06-24T10:00:00"),
        )
    conn.commit()
    conn.close()


def test_feed_returns_only_revealed_sorted_by_risk_desc(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app)
    resp = client.get("/api/feed")
    assert resp.status_code == 200
    data = resp.json()
    ids = [row["post"]["id"] for row in data]
    assert ids == ["p1", "p2"]
    assert data[0]["score"]["risk"] == 85


def test_feed_min_risk_filter(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app)
    resp = client.get("/api/feed?min_risk=50")
    ids = [row["post"]["id"] for row in resp.json()]
    assert ids == ["p1"]
```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run: `pytest tests/test_feed.py -v`. Expected: FAIL — маршрут `/api/feed` ещё не реализован (404 или AttributeError).

- [ ] **Шаг 3: Минимальная реализация.** В `app/main.py` добавить маршрут. Использует `db.get_conn()` (из F0). Возвращает список `{post, score}` (поля строго по контракту моделей `Post`/`Score`).

```python
from fastapi import Query
from app import db


@app.get("/api/feed")
def api_feed(min_risk: int = Query(0), category: str | None = Query(None), limit: int = Query(100)):
    conn = db.get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    sql = (
        "SELECT p.id, p.platform, p.author_handle, p.url, p.caption, p.posted_at,"
        " p.media_path, p.thumb_url, p.source,"
        " s.risk, s.category, s.class_probs_json, s.top_features_json, s.recommended_action"
        " FROM posts p JOIN scores s ON s.post_id = p.id"
        " WHERE p.revealed = 1 AND s.risk >= ?"
    )
    params: list = [min_risk]
    if category:
        sql += " AND s.category = ?"
        params.append(category)
    sql += " ORDER BY s.risk DESC LIMIT ?"
    params.append(limit)
    rows = cur.execute(sql, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "post": {
                "id": r["id"], "platform": r["platform"], "author_handle": r["author_handle"],
                "url": r["url"], "caption": r["caption"], "posted_at": r["posted_at"],
                "media_path": r["media_path"], "thumb_url": r["thumb_url"], "source": r["source"],
            },
            "score": {
                "post_id": r["id"], "risk": r["risk"], "category": r["category"],
                "class_probs": json.loads(r["class_probs_json"]),
                "top_features": json.loads(r["top_features_json"]),
            },
            "recommended_action": r["recommended_action"],
        })
    return out
```

Убедиться, что вверху `app/main.py` есть `import json` и `import sqlite3`.

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run: `pytest tests/test_feed.py -v`. Expected: PASS (2 passed).

- [ ] **Шаг 5: Коммит.** Run:
```bash
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" rev-parse --show-toplevel
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" add app/main.py tests/test_feed.py
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" commit -m "feat(api): GET /api/feed priority queue of revealed posts sorted by risk"
```

---

### Задача 2: GET /api/post/{id} — полная карточка для drill-down

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\main.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_post_detail.py`

- [ ] **Шаг 1: Написать падающий тест.** Создать `tests/test_post_detail.py`. Засеять один пост + extracted + score, проверить форму ответа `{post, extracted, score, explanation, recommended_action}`.

```python
import json
from fastapi.testclient import TestClient
from app.main import app
from app import db


def test_post_detail_shape(monkeypatch, tmp_path):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO posts (id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source, revealed)"
        " VALUES ('p1','tiktok','@a','http://x','Гарантированный доход 30% в месяц','2026-06-24T10:00:00',NULL,NULL,'seed',1)"
    )
    cur.execute(
        "INSERT INTO extracted (post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json)"
        " VALUES ('p1','Гарантированный доход','транскрипт','OCR казино',?,'combined',?)",
        (json.dumps([{"label": "casino", "score": 0.9}]),
         json.dumps([{"type": "payout_promise", "value": "30%", "normalized": "30% в месяц"}])),
    )
    cur.execute(
        "INSERT INTO scores (post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at)"
        " VALUES ('p1',88,'pyramid',?,?,'escalate','2026-06-24T10:00:00')",
        (json.dumps({"pyramid": 0.88, "clean": 0.12}),
         json.dumps([{"feature": "payout_promise", "weight": 0.7, "evidence": "30% в месяц"}])),
    )
    conn.commit()
    conn.close()

    client = TestClient(app)
    resp = client.get("/api/post/p1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["post"]["id"] == "p1"
    assert data["extracted"]["ocr_text"] == "OCR казино"
    assert data["extracted"]["visual_concepts"][0]["label"] == "casino"
    assert data["score"]["risk"] == 88
    assert isinstance(data["explanation"], list) and len(data["explanation"]) >= 1
    assert data["recommended_action"] == "escalate"


def test_post_detail_404(monkeypatch, tmp_path):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    client = TestClient(app)
    assert client.get("/api/post/nope").status_code == 404
```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run: `pytest tests/test_post_detail.py -v`. Expected: FAIL — маршрут не существует.

- [ ] **Шаг 3: Минимальная реализация.** В `app/main.py` добавить маршрут. Объяснение строит `explain(score, entities)` из F3.

```python
from fastapi import HTTPException
from app.models import Score, FeatureHit, Entity
from app.decision.explain import explain


@app.get("/api/post/{post_id}")
def api_post_detail(post_id: str):
    conn = db.get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    p = cur.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if p is None:
        conn.close()
        raise HTTPException(status_code=404, detail="post not found")
    e = cur.execute("SELECT * FROM extracted WHERE post_id = ?", (post_id,)).fetchone()
    s = cur.execute("SELECT * FROM scores WHERE post_id = ?", (post_id,)).fetchone()
    conn.close()

    entities = [Entity(**ent) for ent in (json.loads(e["entities_json"]) if e else [])]
    top_features = [FeatureHit(**f) for f in json.loads(s["top_features_json"])] if s else []
    score = Score(
        post_id=post_id, risk=s["risk"], category=s["category"],
        class_probs=json.loads(s["class_probs_json"]), top_features=top_features,
    ) if s else None

    return {
        "post": {
            "id": p["id"], "platform": p["platform"], "author_handle": p["author_handle"],
            "url": p["url"], "caption": p["caption"], "posted_at": p["posted_at"],
            "media_path": p["media_path"], "thumb_url": p["thumb_url"], "source": p["source"],
        },
        "extracted": {
            "post_id": post_id,
            "caption": e["caption"] if e else "",
            "transcript": e["transcript"] if e else "",
            "ocr_text": e["ocr_text"] if e else "",
            "visual_concepts": json.loads(e["visual_concepts_json"]) if e else [],
            "combined_text": e["combined_text"] if e else "",
            "entities": json.loads(e["entities_json"]) if e else [],
        } if e else None,
        "score": {
            "post_id": post_id, "risk": s["risk"], "category": s["category"],
            "class_probs": json.loads(s["class_probs_json"]),
            "top_features": json.loads(s["top_features_json"]),
        } if s else None,
        "explanation": explain(score, entities) if score else [],
        "recommended_action": s["recommended_action"] if s else None,
    }
```

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run: `pytest tests/test_post_detail.py -v`. Expected: PASS (2 passed).

- [ ] **Шаг 5: Коммит.** Run:
```bash
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" add app/main.py tests/test_post_detail.py
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" commit -m "feat(api): GET /api/post/{id} drill-down detail with explanation"
```

---

### Задача 3: POST /api/tick — раскрытие следующих N seed-постов

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\main.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_tick.py`

- [ ] **Шаг 1: Написать падающий тест.** Создать `tests/test_tick.py`. Засеять 5 нераскрытых seed-постов, вызвать `/api/tick` с `n=2`, проверить, что revealed-счётчик стал 2, при повторном — 4, и что раскрываются самые старые по `posted_at`.

```python
from fastapi.testclient import TestClient
from app.main import app
from app import db


def _seed(monkeypatch, tmp_path):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    conn = db.get_conn()
    cur = conn.cursor()
    for i in range(5):
        cur.execute(
            "INSERT INTO posts (id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source, revealed)"
            " VALUES (?,?,?,?,?,?,?,?,?,0)",
            (f"p{i}", "tiktok", "@a", "http://x", "cap", f"2026-06-24T10:0{i}:00", None, None, "seed"),
        )
    conn.commit()
    conn.close()


def _revealed_count(monkeypatch, tmp_path):
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM posts WHERE revealed = 1").fetchone()[0]
    conn.close()
    return n


def test_tick_reveals_next_n(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app)
    r1 = client.post("/api/tick", json={"n": 2})
    assert r1.status_code == 200
    assert r1.json()["revealed"] == 2
    assert _revealed_count(monkeypatch, tmp_path) == 2
    r2 = client.post("/api/tick", json={"n": 2})
    assert r2.json()["revealed"] == 4
    assert _revealed_count(monkeypatch, tmp_path) == 4
```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run: `pytest tests/test_tick.py -v`. Expected: FAIL — маршрут не существует.

- [ ] **Шаг 3: Минимальная реализация.** В `app/main.py`. Раскрытие самых старых нераскрытых; при раскрытии пишем `AuditEntry` через `db` (action="revealed").

```python
from pydantic import BaseModel
from datetime import datetime, timezone


class TickBody(BaseModel):
    n: int = 3


@app.post("/api/tick")
def api_tick(body: TickBody = TickBody()):
    conn = db.get_conn()
    cur = conn.cursor()
    ids = [row[0] for row in cur.execute(
        "SELECT id FROM posts WHERE revealed = 0 ORDER BY posted_at ASC LIMIT ?", (body.n,)
    ).fetchall()]
    ts = datetime.now(timezone.utc).isoformat()
    for pid in ids:
        cur.execute("UPDATE posts SET revealed = 1 WHERE id = ?", (pid,))
        cur.execute(
            "INSERT INTO audit (ts, post_id, action, actor, detail) VALUES (?,?,?,?,?)",
            (ts, pid, "revealed", "system", "ticker reveal"),
        )
    total = cur.execute("SELECT COUNT(*) FROM posts WHERE revealed = 1").fetchone()[0]
    conn.commit()
    conn.close()
    return {"newly_revealed": ids, "revealed": total}
```

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run: `pytest tests/test_tick.py -v`. Expected: PASS (1 passed).

- [ ] **Шаг 5: Коммит.** Run:
```bash
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" add app/main.py tests/test_tick.py
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" commit -m "feat(api): POST /api/tick reveals next N seed posts with audit"
```

---

### Задача 4: POST /api/analyze — live-проверка по ссылке через пайплайн

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\main.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_analyze.py`

- [ ] **Шаг 1: Написать падающий тест.** Создать `tests/test_analyze.py`. Тяжёлый пайплайн заглушаем через monkeypatch на `app.main.fetch_post`, `app.main.extract`, `app.main.score_post`, чтобы тест был мгновенным и без моделей. Проверяем форму `{post, extracted, score}`.

```python
from fastapi.testclient import TestClient
from app.main import app
from app import db
from app.models import Post, Extracted, Score, FeatureHit
import app.main as main_mod


def test_analyze_url_returns_score(monkeypatch, tmp_path):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()

    fake_post = Post(
        id="live1", platform="tiktok", author_handle="@scam", url="http://x",
        caption="100% доход", posted_at="2026-06-24T10:00:00",
        media_path=None, thumb_url=None, source="live",
    )
    fake_extracted = Extracted(
        post_id="live1", caption="100% доход", transcript="", ocr_text="",
        visual_concepts=[], combined_text="100% доход", entities=[],
    )
    fake_score = Score(
        post_id="live1", risk=91, category="pyramid",
        class_probs={"pyramid": 0.91, "clean": 0.09},
        top_features=[FeatureHit(feature="payout_promise", weight=0.8, evidence="100% доход")],
    )

    monkeypatch.setattr(main_mod, "fetch_post", lambda url=None, upload=None: fake_post)
    monkeypatch.setattr(main_mod, "extract", lambda post, use_cache=True: fake_extracted)
    monkeypatch.setattr(main_mod, "score_post", lambda post, extracted: fake_score)

    client = TestClient(app)
    resp = client.post("/api/analyze", json={"url": "http://x"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["post"]["id"] == "live1"
    assert data["score"]["risk"] == 91
    assert data["score"]["category"] == "pyramid"
    assert data["extracted"]["combined_text"] == "100% доход"
```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run: `pytest tests/test_analyze.py -v`. Expected: FAIL — маршрут не существует.

- [ ] **Шаг 3: Минимальная реализация.** В `app/main.py`. Импортируем `fetch_post` из F1 (`app/ingestion/fetch.py`), `extract` из F1, `score_post` из F3 — на уровне модуля, чтобы monkeypatch подменял имена в `app.main`. Поддерживаем JSON `{url}` и multipart-файл.

```python
from dataclasses import asdict
from fastapi import UploadFile, File
from app.ingestion.fetch import fetch_post
from app.extractors.pipeline import extract
from app.decision.scoring import score_post


@app.post("/api/analyze")
async def api_analyze(payload: dict | None = None, file: UploadFile | None = File(default=None)):
    upload_path = None
    url = None
    if file is not None:
        import tempfile, os
        suffix = os.path.splitext(file.filename or "")[1]
        fd, upload_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(await file.read())
    elif payload:
        url = payload.get("url")

    post = fetch_post(url=url, upload=upload_path)
    extracted = extract(post, use_cache=False)
    score = score_post(post, extracted)

    return {
        "post": asdict(post) if hasattr(post, "__dataclass_fields__") else post,
        "extracted": asdict(extracted) if hasattr(extracted, "__dataclass_fields__") else extracted,
        "score": asdict(score) if hasattr(score, "__dataclass_fields__") else score,
    }
```

- [ ] **Шаг 4: Запустить тест (ожидаем успех).** Run: `pytest tests/test_analyze.py -v`. Expected: PASS (1 passed).

- [ ] **Шаг 5: Коммит.** Run:
```bash
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" add app/main.py tests/test_analyze.py
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" commit -m "feat(api): POST /api/analyze live pipeline returns Score"
```

---

### Задача 5: Фоновый тикер — симуляция непрерывного потока

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\main.py`
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\config.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_ticker.py`

- [ ] **Шаг 1: Написать падающий тест.** Создать `tests/test_ticker.py`. Тестируем чистую функцию `reveal_next(n)` (которую переиспользует и `/api/tick`, и фоновая задача), без реального сна и без asyncio.

```python
from fastapi.testclient import TestClient
from app.main import app, reveal_next
from app import db


def test_reveal_next_is_idempotent_on_empty(monkeypatch, tmp_path):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(dbfile))
    db.init_db()
    conn = db.get_conn()
    cur = conn.cursor()
    for i in range(3):
        cur.execute(
            "INSERT INTO posts (id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source, revealed)"
            " VALUES (?,?,?,?,?,?,?,?,?,0)",
            (f"p{i}", "tiktok", "@a", "http://x", "cap", f"2026-06-24T10:0{i}:00", None, None, "seed"),
        )
    conn.commit()
    conn.close()

    assert reveal_next(2)["revealed"] == 2
    assert reveal_next(5)["revealed"] == 3   # only 1 left -> total 3
    assert reveal_next(1)["revealed"] == 3   # nothing left, stays 3
```

- [ ] **Шаг 2: Запустить тест (ожидаем провал).** Run: `pytest tests/test_ticker.py -v`. Expected: FAIL — `reveal_next` ещё нет.

- [ ] **Шаг 3: Минимальная реализация.** В `app/config.py` добавить константы. В `app/main.py` вынести логику раскрытия в `reveal_next(n)` (рефактор Задачи 3 — `/api/tick` теперь вызывает её), и зарегистрировать фоновую задачу через lifespan/`startup`.

В `app/config.py`:
```python
TICK_INTERVAL_SECONDS = 4
TICK_BATCH = 1
```

В `app/main.py`:
```python
import asyncio
from app import config


def reveal_next(n: int) -> dict:
    conn = db.get_conn()
    cur = conn.cursor()
    ids = [row[0] for row in cur.execute(
        "SELECT id FROM posts WHERE revealed = 0 ORDER BY posted_at ASC LIMIT ?", (n,)
    ).fetchall()]
    ts = datetime.now(timezone.utc).isoformat()
    for pid in ids:
        cur.execute("UPDATE posts SET revealed = 1 WHERE id = ?", (pid,))
        cur.execute(
            "INSERT INTO audit (ts, post_id, action, actor, detail) VALUES (?,?,?,?,?)",
            (ts, pid, "revealed", "system", "ticker reveal"),
        )
    total = cur.execute("SELECT COUNT(*) FROM posts WHERE revealed = 1").fetchone()[0]
    conn.commit()
    conn.close()
    return {"newly_revealed": ids, "revealed": total}


_ticker_task = None


async def _ticker_loop():
    while True:
        await asyncio.sleep(config.TICK_INTERVAL_SECONDS)
        try:
            reveal_next(config.TICK_BATCH)
        except Exception:
            pass


@app.on_event("startup")
async def _start_ticker():
    global _ticker_task
    _ticker_task = asyncio.create_task(_ticker_loop())
```

Изменить `api_tick` из Задачи 3, чтобы тело стало: `return reveal_next(body.n)`.

- [ ] **Шаг 4: Запустить тесты (ожидаем успех).** Run: `pytest tests/test_ticker.py tests/test_tick.py -v`. Expected: PASS (оба файла зелёные — рефактор не сломал `/api/tick`).

- [ ] **Шаг 5: Коммит.** Run:
```bash
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" add app/main.py app/config.py tests/test_ticker.py
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" commit -m "feat(api): background ticker reveals seed posts to simulate stream"
```

---

### Задача 6: Фронтенд-каркас консоли (web/index.html) + Alpine-состояние и лента

**Файлы:**
- Create: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\web\index.html`
- Create: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\web\app.js`

- [ ] **Шаг 1: Создать `web/index.html`.** SOC-console тёмная тема через Tailwind CDN + Alpine CDN. Шапка «КӨЗ — AI Media Watch», лента слева, drill-down справа, блок live-проверки. Цвет риска: красный `>= ESCALATE (70)`, янтарный `>= REVIEW (40)`, иначе зелёный.

```html
<!DOCTYPE html>
<html lang="ru" class="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>КӨЗ — AI Media Watch · АФМ</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <script defer src="/app.js"></script>
</head>
<body class="bg-slate-950 text-slate-100 font-sans" x-data="kozApp()" x-init="init()">
  <header class="flex items-center justify-between px-6 py-3 border-b border-slate-800 bg-slate-900">
    <div class="flex items-center gap-3">
      <span class="text-2xl font-bold text-emerald-400">КӨЗ</span>
      <span class="text-sm text-slate-400">AI Media Watch · мониторинг соцсетей · АФМ</span>
    </div>
    <div class="flex items-center gap-2 text-xs">
      <span class="text-slate-400">Раскрыто постов:</span>
      <span class="font-mono text-emerald-400" x-text="revealedCount"></span>
      <button @click="tick()" class="ml-3 px-3 py-1 rounded bg-slate-700 hover:bg-slate-600">+ поток</button>
    </div>
  </header>

  <main class="grid grid-cols-12 gap-4 p-4">
    <!-- Лента / приоритетная очередь -->
    <section class="col-span-4 space-y-3">
      <div class="flex items-center gap-2 mb-2">
        <h2 class="text-sm font-semibold text-slate-300">Приоритетная очередь</h2>
        <select x-model="categoryFilter" @change="loadFeed()" class="ml-auto bg-slate-800 text-xs rounded px-2 py-1">
          <option value="">все категории</option>
          <option value="gambling">гемблинг</option>
          <option value="pyramid">финпирамида</option>
          <option value="fraud">мошенничество</option>
          <option value="clean">чисто</option>
        </select>
      </div>
      <template x-for="row in feed" :key="row.post.id">
        <div @click="openPost(row.post.id)"
             class="cursor-pointer rounded-lg border border-slate-800 bg-slate-900 p-3 hover:border-emerald-600"
             :class="row.post.id === selectedId ? 'border-emerald-500' : ''">
          <div class="flex items-center justify-between">
            <span class="font-mono text-xs px-2 py-0.5 rounded" :class="riskClass(row.score.risk)" x-text="row.score.risk"></span>
            <span class="text-xs text-slate-400" x-text="row.post.platform"></span>
          </div>
          <div class="mt-1 text-xs text-slate-300" x-text="row.post.author_handle"></div>
          <div class="text-sm truncate" x-text="row.post.caption"></div>
          <div class="mt-1 flex gap-1">
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300" x-text="categoryLabel(row.score.category)"></span>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300" x-text="actionLabel(row.recommended_action)"></span>
          </div>
        </div>
      </template>
    </section>

    <!-- Drill-down -->
    <section class="col-span-5 rounded-lg border border-slate-800 bg-slate-900 p-4 min-h-[60vh]">
      <template x-if="!detail">
        <p class="text-slate-500 text-sm">Выберите пост из очереди для разбора.</p>
      </template>
      <template x-if="detail">
        <div class="space-y-3">
          <div class="flex items-center justify-between">
            <h2 class="text-lg font-semibold" x-text="detail.post.author_handle"></h2>
            <span class="font-mono text-sm px-2 py-1 rounded" :class="riskClass(detail.score.risk)" x-text="'Риск: ' + detail.score.risk"></span>
          </div>
          <video x-show="detail.post.media_path" :src="detail.post.media_path" controls class="w-full rounded"></video>
          <img x-show="!detail.post.media_path && detail.post.thumb_url" :src="detail.post.thumb_url" class="w-full rounded" />

          <div>
            <h3 class="text-xs font-semibold text-slate-400 mb-1">Транскрипт (с подсветкой триггеров)</h3>
            <p class="text-sm bg-slate-950 rounded p-2" x-html="highlight(detail.extracted.transcript, detail.score.top_features)"></p>
          </div>
          <div x-show="detail.extracted.ocr_text">
            <h3 class="text-xs font-semibold text-slate-400 mb-1">Текст с экрана (OCR)</h3>
            <p class="text-sm bg-slate-950 rounded p-2" x-text="detail.extracted.ocr_text"></p>
          </div>
          <div x-show="detail.extracted.visual_concepts.length">
            <h3 class="text-xs font-semibold text-slate-400 mb-1">Визуальные концепты</h3>
            <div class="flex flex-wrap gap-1">
              <template x-for="vc in detail.extracted.visual_concepts" :key="vc.label">
                <span class="text-[11px] px-2 py-0.5 rounded bg-indigo-900 text-indigo-200"
                      x-text="vc.label + ' ' + Math.round(vc.score * 100) + '%'"></span>
              </template>
            </div>
          </div>
          <div x-show="detail.score.top_features.length">
            <h3 class="text-xs font-semibold text-slate-400 mb-1">Сработавшие паттерны</h3>
            <ul class="text-sm list-disc list-inside text-amber-300">
              <template x-for="f in detail.score.top_features" :key="f.feature">
                <li x-text="f.feature + ' — ' + f.evidence"></li>
              </template>
            </ul>
          </div>
          <div>
            <h3 class="text-xs font-semibold text-slate-400 mb-1">Почему опасно</h3>
            <ul class="text-sm list-disc list-inside text-slate-200">
              <template x-for="line in detail.explanation" :key="line"><li x-text="line"></li></template>
            </ul>
          </div>
          <div class="flex items-center gap-3 pt-2">
            <span class="text-sm">Рекомендация: <b x-text="actionLabel(detail.recommended_action)"></b></span>
            <a :href="'/api/report/' + detail.post.id + '.pdf'" target="_blank"
               class="ml-auto px-3 py-1.5 rounded bg-emerald-700 hover:bg-emerald-600 text-sm">Экспорт дела (PDF)</a>
          </div>
        </div>
      </template>
    </section>

    <!-- Live-проверка -->
    <section class="col-span-3 rounded-lg border border-slate-800 bg-slate-900 p-4 space-y-3">
      <h2 class="text-sm font-semibold text-slate-300">Live-проверка</h2>
      <input x-model="liveUrl" placeholder="Вставьте ссылку на пост"
             class="w-full bg-slate-800 rounded px-2 py-1 text-sm" />
      <input type="file" @change="liveFile = $event.target.files[0]" class="w-full text-xs" />
      <button @click="analyze()" :disabled="liveLoading"
              class="w-full px-3 py-2 rounded bg-emerald-700 hover:bg-emerald-600 text-sm disabled:opacity-50"
              x-text="liveLoading ? 'Анализ…' : 'Проверить'"></button>
      <template x-if="liveResult">
        <div class="rounded border border-slate-700 p-2 text-sm space-y-1">
          <div class="font-mono px-2 py-0.5 rounded inline-block" :class="riskClass(liveResult.score.risk)"
               x-text="'Риск ' + liveResult.score.risk + ' · ' + categoryLabel(liveResult.score.category)"></div>
          <ul class="list-disc list-inside text-amber-300">
            <template x-for="f in liveResult.score.top_features" :key="f.feature">
              <li x-text="f.feature + ' — ' + f.evidence"></li>
            </template>
          </ul>
        </div>
      </template>
    </section>
  </main>
</body>
</html>
```

- [ ] **Шаг 2: Создать `web/app.js`.** Состояние Alpine + поллинг ленты каждые ~2с, drill-down, тик, live-проверка. Пороги цвета совпадают с `REVIEW_THRESHOLD=40` / `ESCALATE_THRESHOLD=70`.

```javascript
function kozApp() {
  return {
    feed: [],
    detail: null,
    selectedId: null,
    revealedCount: 0,
    categoryFilter: "",
    liveUrl: "",
    liveFile: null,
    liveResult: null,
    liveLoading: false,

    init() {
      this.loadFeed();
      setInterval(() => this.loadFeed(), 2000);
    },

    async loadFeed() {
      let url = "/api/feed?limit=100";
      if (this.categoryFilter) url += "&category=" + encodeURIComponent(this.categoryFilter);
      const r = await fetch(url);
      this.feed = await r.json();
      this.revealedCount = this.feed.length;
    },

    async tick() {
      await fetch("/api/tick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ n: 1 }),
      });
      this.loadFeed();
    },

    async openPost(id) {
      this.selectedId = id;
      const r = await fetch("/api/post/" + id);
      this.detail = await r.json();
    },

    async analyze() {
      this.liveLoading = true;
      this.liveResult = null;
      try {
        let r;
        if (this.liveFile) {
          const fd = new FormData();
          fd.append("file", this.liveFile);
          r = await fetch("/api/analyze", { method: "POST", body: fd });
        } else {
          r = await fetch("/api/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: this.liveUrl }),
          });
        }
        this.liveResult = await r.json();
      } finally {
        this.liveLoading = false;
      }
    },

    riskClass(risk) {
      if (risk >= 70) return "bg-red-700 text-red-100";
      if (risk >= 40) return "bg-amber-600 text-amber-50";
      return "bg-emerald-700 text-emerald-50";
    },

    categoryLabel(c) {
      return { gambling: "гемблинг", pyramid: "финпирамида", fraud: "мошенничество", clean: "чисто" }[c] || c;
    },

    actionLabel(a) {
      return {
        auto_clear: "авто-очистка", monitor: "мониторинг",
        review: "на проверку", escalate: "эскалация",
      }[a] || a;
    },

    highlight(text, features) {
      if (!text) return "";
      let out = text;
      (features || []).forEach((f) => {
        if (f.evidence) {
          const safe = f.evidence.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          out = out.replace(new RegExp(safe, "gi"), (m) => `<mark class="bg-amber-500 text-black">${m}</mark>`);
        }
      });
      return out;
    },
  };
}
```

- [ ] **Шаг 3: Ручная проверка ленты и тика.** Запустить сервер (`run.bat` или `python -m uvicorn app.main:app --reload`), засеять демо (`python scripts/build_demo.py` из F1). Run: `curl http://127.0.0.1:8000/api/feed`. Expected: JSON-массив с полями `post`/`score`, отсортированный по `score.risk` убыв. Затем `curl -X POST http://127.0.0.1:8000/api/tick -H "Content-Type: application/json" -d "{\"n\":2}"`. Expected: `{"newly_revealed":[...],"revealed":<N>}` и в браузере на `http://127.0.0.1:8000/` через ≤2с появляются новые карточки.

- [ ] **Шаг 4: Ручная проверка drill-down.** В браузере кликнуть верхнюю карточку. Expected: справа панель с транскриптом (триггеры подсвечены жёлтым), OCR-блоком при наличии, чипами визуал-концептов, списком сработавших паттернов, буллетами «Почему опасно», рекомендацией и активной кнопкой «Экспорт дела (PDF)», ведущей на `/api/report/<id>.pdf`.

- [ ] **Шаг 5: Ручная проверка live-проверки и цвета риска.** В блоке Live вставить ссылку, нажать «Проверить». Expected: появляется блок вердикта с цветом по порогам (красный ≥70, янтарный ≥40, иначе зелёный) и списком сработавших паттернов. Цвета карточек ленты совпадают с теми же порогами.

- [ ] **Шаг 6: Коммит.** Run:
```bash
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" add web/index.html web/app.js
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" commit -m "feat(web): SOC console — live feed, drill-down, live-check, ticker controls"
```

---

### Задача 7: Полный прогон бэкенд-тестов фичи F4

**Файлы:**
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_feed.py`, `tests\test_post_detail.py`, `tests\test_tick.py`, `tests\test_analyze.py`, `tests\test_ticker.py`

- [ ] **Шаг 1: Прогнать все тесты F4.** Run: `pytest tests/test_feed.py tests/test_post_detail.py tests/test_tick.py tests/test_analyze.py tests/test_ticker.py -v`. Expected: все зелёные (PASS), регрессий нет.

- [ ] **Шаг 2: Прогнать весь набор для уверенности.** Run: `pytest -q`. Expected: 0 failed (маршруты F4 не сломали тесты F0–F3).

- [ ] **Шаг 3: Коммит (если были мелкие правки).** Если на Шагах 1–2 потребовались исправления:
```bash
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" add -A
git -C "C:/Users/adlet/OneDrive/Документы/AFMHACKATHON" commit -m "test(api): green full F4 console suite"
```
Если правок не было — пропустить коммит.

---

## F5: Граф связей

**Цель:** Построить граф «пост → сущность» из уже извлечённых сущностей (F0/F2) и риск-скоров (F3): два поста, делящие один Telegram/бренд/промокод/кошелёк, попадают в одну связную компоненту, что выявляет координированные сети аккаунтов и реф-ринги. Отдать граф через `GET /api/graph?post_id=&min_risk=` (эго-сеть для поста или весь граф с фильтром по риску) и нарисовать его на фронте через `vis-network` (CDN), раскрашивая узлы по риску/типу.

**Зависит от:** F0 (модели `Edge`, `Entity`, `Score`; `app/db.py`; `app/main.py`), F2 (извлечение `entities` в таблицу `extracted`), F3 (скоры в таблице `scores` для раскраски по риску). Если F2/F3 ещё не готовы — тесты F5 используют свои фикстуры, пишущие напрямую в `extracted`/`scores`, поэтому реализацию можно начинать после F0.

---

### Задача 1: Узлы и рёбра одного поста (контракт node id / Edge)

**Файлы:**
- Create: `app/graph/__init__.py`
- Create: `app/graph/build.py`
- Test: `tests/test_graph_build.py`

- [ ] **Шаг 1: Записать падающий тест.** Один пост с двумя сущностями даёт 1 узел-пост + 2 узла-сущности и 2 ребра `post->entity` с правильной схемой id и формой `Edge`.

```python
# tests/test_graph_build.py
import json
import pytest
from app import db, config
from app.graph.build import build_graph


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(dbfile))
    db.init_db()
    return str(dbfile)


def _insert_post(post_id, risk):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (post_id, "telegram", "@" + post_id, "https://t.me/x", "cap", "2026-06-24T10:00:00", None, None, "telegram"),
        )
        conn.commit()


def _insert_extracted(post_id, entities):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO extracted(post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (post_id, "cap", "", "", "[]", "cap", json.dumps(entities)),
        )
        conn.commit()


def _insert_score(post_id, risk, category="gambling"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO scores(post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (post_id, risk, category, "{}", "[]", "review", "2026-06-24T10:00:00"),
        )
        conn.commit()


def test_single_post_two_entities_node_and_edge_shape(fresh_db):
    _insert_post("p1", 80)
    _insert_score("p1", 80)
    _insert_extracted("p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
        {"type": "casino_brand", "value": "1xBet", "normalized": "1xbet"},
    ])

    graph = build_graph(["p1"])

    node_ids = {n["id"] for n in graph["nodes"]}
    assert "post:p1" in node_ids
    assert "entity:telegram:casinox" in node_ids
    assert "entity:casino_brand:1xbet" in node_ids

    post_node = next(n for n in graph["nodes"] if n["id"] == "post:p1")
    assert post_node["type"] == "post"
    assert post_node["risk"] == 80

    assert len(graph["edges"]) == 2
    e = graph["edges"][0]
    assert set(e.keys()) == {"source", "target", "type", "weight"}
    assert e["source"] == "post:p1"
    assert e["target"].startswith("entity:")
    assert e["type"] == "contains"
    assert e["weight"] == 1.0
```

- [ ] **Шаг 2: Запустить (ожидаем провал).**
  Run: `pytest tests/test_graph_build.py::test_single_post_two_entities_node_and_edge_shape -v`
  Expected: FAIL — `ModuleNotFoundError: No module named 'app.graph.build'`.

- [ ] **Шаг 3: Минимальная реализация.**

```python
# app/graph/__init__.py
```

```python
# app/graph/build.py
"""Построение графа связей пост<->сущность из общих извлечённых сущностей."""
import json

from app import db


def _post_node_id(post_id: str) -> str:
    return f"post:{post_id}"


def _entity_node_id(entity: dict) -> str:
    return f"entity:{entity['type']}:{entity['normalized']}"


def build_graph(post_ids: list[str]) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for post_id in post_ids:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT entities_json FROM extracted WHERE post_id = ?", (post_id,)
            ).fetchone()
            score_row = conn.execute(
                "SELECT risk FROM scores WHERE post_id = ?", (post_id,)
            ).fetchone()
        if row is None:
            continue

        risk = int(score_row["risk"]) if score_row is not None else 0
        pid = _post_node_id(post_id)
        nodes[pid] = {"id": pid, "label": post_id, "type": "post", "risk": risk}

        entities = json.loads(row["entities_json"]) if row["entities_json"] else []
        for ent in entities:
            eid = _entity_node_id(ent)
            if eid not in nodes:
                nodes[eid] = {
                    "id": eid,
                    "label": ent["value"],
                    "type": ent["type"],
                    "risk": 0,
                }
            edges.append(
                {"source": pid, "target": eid, "type": "contains", "weight": 1.0}
            )

    return {"nodes": list(nodes.values()), "edges": edges}
```

- [ ] **Шаг 4: Запустить (ожидаем успех).**
  Run: `pytest tests/test_graph_build.py::test_single_post_two_entities_node_and_edge_shape -v`
  Expected: PASS (1 passed).

- [ ] **Шаг 5: Коммит.**
  Run: `git rev-parse --show-toplevel` (Expected: путь к `AFMHACKATHON`, НЕ `C:/Users/adlet`).
  Run: `git add app/graph/__init__.py app/graph/build.py tests/test_graph_build.py`
  Run: `git commit -m "feat(graph): build_graph emits post/entity nodes and contains edges"`

---

### Задача 2: Общая сущность связывает два поста в одну компоненту

**Файлы:**
- Test: `tests/test_graph_build.py` (Modify — добавить тест)

- [ ] **Шаг 1: Записать падающий тест.** Два поста делят один Telegram-узел → граф связен (один общий узел-сущность, к которому идут оба поста).

```python
# tests/test_graph_build.py  (добавить в конец файла)
def test_shared_telegram_entity_links_two_posts(fresh_db):
    _insert_post("p1", 80)
    _insert_score("p1", 80)
    _insert_extracted("p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
    ])
    _insert_post("p2", 65)
    _insert_score("p2", 65)
    _insert_extracted("p2", [
        {"type": "telegram", "value": "@casinox", "normalized": "casinox"},
    ])

    graph = build_graph(["p1", "p2"])

    # Один общий узел-сущность для двух постов
    entity_nodes = [n for n in graph["nodes"] if n["type"] == "telegram"]
    assert len(entity_nodes) == 1
    shared_id = entity_nodes[0]["id"]
    assert shared_id == "entity:telegram:casinox"

    # Оба поста имеют ребро к общему узлу => связная компонента
    sources_to_shared = {e["source"] for e in graph["edges"] if e["target"] == shared_id}
    assert sources_to_shared == {"post:p1", "post:p2"}

    # Два поста + один общий узел-сущность = 3 узла, 2 ребра
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 2
```

- [ ] **Шаг 2: Запустить (ожидаем успех — дедуп узлов уже реализован в Задаче 1).**
  Run: `pytest tests/test_graph_build.py::test_shared_telegram_entity_links_two_posts -v`
  Expected: PASS (1 passed). Если FAIL — узлы-сущности не дедуплицируются по id; проверить, что `_entity_node_id` нормализует и что `nodes` — dict с ключом по id.

- [ ] **Шаг 3: Коммит.**
  Run: `git add tests/test_graph_build.py`
  Run: `git commit -m "test(graph): shared entity links two posts into one component"`

---

### Задача 3: Эго-сеть для поста (его сущности + co-posts)

**Файлы:**
- Modify: `app/graph/build.py` (добавить `build_ego_graph`)
- Test: `tests/test_graph_build.py` (Modify — добавить тест)

- [ ] **Шаг 1: Записать падающий тест.** Эго-сеть `p1` содержит сам пост, его сущности и со-посты (другие посты, делящие сущность с `p1`), но НЕ посторонние посты.

```python
# tests/test_graph_build.py  (добавить в конец файла)
from app.graph.build import build_ego_graph


def test_ego_graph_returns_entities_and_co_posts(fresh_db):
    # p1 и p2 делят telegram:casinox; p3 не связан
    _insert_post("p1", 80); _insert_score("p1", 80)
    _insert_extracted("p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
        {"type": "promo_code", "value": "WIN100", "normalized": "win100"},
    ])
    _insert_post("p2", 70); _insert_score("p2", 70)
    _insert_extracted("p2", [
        {"type": "telegram", "value": "@casinox", "normalized": "casinox"},
    ])
    _insert_post("p3", 10); _insert_score("p3", 10)
    _insert_extracted("p3", [
        {"type": "casino_brand", "value": "Other", "normalized": "other"},
    ])

    graph = build_ego_graph("p1")
    node_ids = {n["id"] for n in graph["nodes"]}

    # сам пост + его сущности
    assert "post:p1" in node_ids
    assert "entity:telegram:casinox" in node_ids
    assert "entity:promo_code:win100" in node_ids
    # co-post через общую сущность
    assert "post:p2" in node_ids
    # несвязанный пост и его сущность исключены
    assert "post:p3" not in node_ids
    assert "entity:casino_brand:other" not in node_ids
```

- [ ] **Шаг 2: Запустить (ожидаем провал).**
  Run: `pytest tests/test_graph_build.py::test_ego_graph_returns_entities_and_co_posts -v`
  Expected: FAIL — `ImportError: cannot import name 'build_ego_graph'`.

- [ ] **Шаг 3: Минимальная реализация.** Добавить в `app/graph/build.py`:

```python
# app/graph/build.py  (добавить)
def _entities_for(post_id: str) -> list[dict]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT entities_json FROM extracted WHERE post_id = ?", (post_id,)
        ).fetchone()
    if row is None or not row["entities_json"]:
        return []
    return json.loads(row["entities_json"])


def build_ego_graph(post_id: str) -> dict:
    """Эго-сеть поста: его сущности + посты, делящие хотя бы одну сущность."""
    ego_entity_ids = {_entity_node_id(e) for e in _entities_for(post_id)}

    with db.connect() as conn:
        all_post_ids = [r["post_id"] for r in conn.execute(
            "SELECT post_id FROM extracted"
        ).fetchall()]

    co_post_ids = {post_id}
    for other in all_post_ids:
        if other == post_id:
            continue
        other_ids = {_entity_node_id(e) for e in _entities_for(other)}
        if ego_entity_ids & other_ids:
            co_post_ids.add(other)

    return build_graph(sorted(co_post_ids))
```

- [ ] **Шаг 4: Запустить (ожидаем успех).**
  Run: `pytest tests/test_graph_build.py::test_ego_graph_returns_entities_and_co_posts -v`
  Expected: PASS (1 passed).

- [ ] **Шаг 5: Коммит.**
  Run: `git add app/graph/build.py tests/test_graph_build.py`
  Run: `git commit -m "feat(graph): build_ego_graph returns post entities and co-posts"`

---

### Задача 4: Флаг кластеров высокого риска

**Файлы:**
- Modify: `app/graph/build.py` (узлы-сущности получают `risk` = макс. риск инцидентных постов; флаг `high_risk`)
- Test: `tests/test_graph_build.py` (Modify — добавить тест)

- [ ] **Шаг 1: Записать падающий тест.** Узел-сущность, к которой ведут посты с риском 80 и 65, получает `risk == 80` (макс. инцидентного) и `high_risk == True` при `risk >= ESCALATE_THRESHOLD` (70).

```python
# tests/test_graph_build.py  (добавить в конец файла)
def test_entity_node_risk_is_max_incident_post_and_flags_high_risk(fresh_db):
    _insert_post("p1", 80); _insert_score("p1", 80)
    _insert_extracted("p1", [
        {"type": "telegram", "value": "@CasinoX", "normalized": "casinox"},
    ])
    _insert_post("p2", 65); _insert_score("p2", 65)
    _insert_extracted("p2", [
        {"type": "telegram", "value": "@casinox", "normalized": "casinox"},
    ])

    graph = build_graph(["p1", "p2"])
    ent = next(n for n in graph["nodes"] if n["id"] == "entity:telegram:casinox")
    assert ent["risk"] == 80          # макс. риска инцидентных постов
    assert ent["high_risk"] is True   # >= ESCALATE_THRESHOLD (70)

    p2 = next(n for n in graph["nodes"] if n["id"] == "post:p2")
    assert p2["high_risk"] is False   # 65 < 70
```

- [ ] **Шаг 2: Запустить (ожидаем провал).**
  Run: `pytest tests/test_graph_build.py::test_entity_node_risk_is_max_incident_post_and_flags_high_risk -v`
  Expected: FAIL — у узла-сущности `risk == 0`, ключа `high_risk` нет (`KeyError`).

- [ ] **Шаг 3: Минимальная реализация.** Изменить `build_graph` в `app/graph/build.py` так, чтобы узлы-сущности накапливали макс. риск инцидентных постов, и проставить `high_risk` всем узлам в конце:

```python
# app/graph/build.py  -- заменить тело build_graph на это
from app import config


def build_graph(post_ids: list[str]) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    for post_id in post_ids:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT entities_json FROM extracted WHERE post_id = ?", (post_id,)
            ).fetchone()
            score_row = conn.execute(
                "SELECT risk FROM scores WHERE post_id = ?", (post_id,)
            ).fetchone()
        if row is None:
            continue

        risk = int(score_row["risk"]) if score_row is not None else 0
        pid = _post_node_id(post_id)
        nodes[pid] = {"id": pid, "label": post_id, "type": "post", "risk": risk}

        entities = json.loads(row["entities_json"]) if row["entities_json"] else []
        for ent in entities:
            eid = _entity_node_id(ent)
            if eid not in nodes:
                nodes[eid] = {
                    "id": eid,
                    "label": ent["value"],
                    "type": ent["type"],
                    "risk": 0,
                }
            # узел-сущность наследует макс. риск инцидентных постов (кластер)
            nodes[eid]["risk"] = max(nodes[eid]["risk"], risk)
            edges.append(
                {"source": pid, "target": eid, "type": "contains", "weight": 1.0}
            )

    for node in nodes.values():
        node["high_risk"] = node["risk"] >= config.ESCALATE_THRESHOLD

    return {"nodes": list(nodes.values()), "edges": edges}
```

- [ ] **Шаг 4: Запустить (ожидаем успех — весь файл, ничего не сломали).**
  Run: `pytest tests/test_graph_build.py -v`
  Expected: PASS (4 passed).

- [ ] **Шаг 5: Коммит.**
  Run: `git add app/graph/build.py tests/test_graph_build.py`
  Run: `git commit -m "feat(graph): entity nodes carry max incident risk and high_risk flag"`

---

### Задача 5: Эндпоинт `GET /api/graph`

**Файлы:**
- Modify: `app/main.py` (добавить маршрут)
- Test: `tests/test_api_graph.py` (Create)

- [ ] **Шаг 1: Записать падающий тест.** Без `post_id` возвращается весь граф, фильтрованный по `min_risk`; с `post_id` — эго-сеть. Форма ответа: `{nodes, edges}`.

```python
# tests/test_api_graph.py
import json
import pytest
from fastapi.testclient import TestClient
from app import db, config
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    dbfile = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(dbfile))
    db.init_db()

    def add(pid, risk, ents):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source, revealed) "
                "VALUES(?,?,?,?,?,?,?,?,?,1)",
                (pid, "telegram", "@" + pid, "https://t.me/x", "cap", "2026-06-24T10:00:00", None, None, "telegram"),
            )
            conn.execute(
                "INSERT INTO extracted(post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, "cap", "", "", "[]", "cap", json.dumps(ents)),
            )
            conn.execute(
                "INSERT INTO scores(post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, risk, "gambling", "{}", "[]", "review", "2026-06-24T10:00:00"),
            )
            conn.commit()

    add("p1", 80, [{"type": "telegram", "value": "@CasinoX", "normalized": "casinox"}])
    add("p2", 70, [{"type": "telegram", "value": "@casinox", "normalized": "casinox"}])
    add("p3", 10, [{"type": "casino_brand", "value": "Other", "normalized": "other"}])
    return TestClient(app)


def test_graph_whole_filtered_by_min_risk(client):
    resp = client.get("/api/graph?min_risk=50")
    assert resp.status_code == 200
    data = resp.json()
    node_ids = {n["id"] for n in data["nodes"]}
    assert "post:p1" in node_ids
    assert "post:p2" in node_ids
    assert "post:p3" not in node_ids  # risk 10 < 50 отфильтрован
    assert isinstance(data["edges"], list)


def test_graph_ego_for_post(client):
    resp = client.get("/api/graph?post_id=p1")
    assert resp.status_code == 200
    node_ids = {n["id"] for n in resp.json()["nodes"]}
    assert "post:p1" in node_ids
    assert "post:p2" in node_ids       # co-post через общую сущность
    assert "post:p3" not in node_ids
```

- [ ] **Шаг 2: Запустить (ожидаем провал).**
  Run: `pytest tests/test_api_graph.py -v`
  Expected: FAIL — маршрут `/api/graph` ещё не зарегистрирован (404 / KeyError).

- [ ] **Шаг 3: Минимальная реализация.** Добавить в `app/main.py` (рядом с другими `@app.get`):

```python
# app/main.py  (добавить импорт и маршрут)
from app.graph.build import build_graph, build_ego_graph


@app.get("/api/graph")
def api_graph(post_id: str | None = None, min_risk: int = 0):
    if post_id:
        return build_ego_graph(post_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.post_id FROM scores s "
            "JOIN posts p ON p.id = s.post_id "
            "WHERE p.revealed = 1 AND s.risk >= ?",
            (min_risk,),
        ).fetchall()
    return build_graph([r["post_id"] for r in rows])
```

- [ ] **Шаг 4: Запустить (ожидаем успех).**
  Run: `pytest tests/test_api_graph.py -v`
  Expected: PASS (2 passed).

- [ ] **Шаг 5: Коммит.**
  Run: `git add app/main.py tests/test_api_graph.py`
  Run: `git commit -m "feat(graph): GET /api/graph returns ego-network or filtered whole graph"`

---

### Задача 6: Фронтенд — представление графа на vis-network

**Файлы:**
- Modify: `web/index.html` (контейнер графа + CDN-скрипт `vis-network`)
- Modify: `web/app.js` (загрузка `/api/graph`, отрисовка, раскраска, клик→drill-down)

- [ ] **Шаг 1: Подключить CDN и контейнер.** В `web/index.html` в `<head>` добавить:

```html
<script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
```

В тело (раздел «Граф связей», вкладка консоли) добавить контейнер и фильтр:

```html
<div x-show="view === 'graph'" class="p-4">
  <div class="flex items-center gap-3 mb-3">
    <label class="text-sm text-slate-300">Мин. риск</label>
    <input type="range" min="0" max="100" step="10" x-model.number="graphMinRisk" @change="loadGraph()" />
    <span class="text-sm text-slate-400" x-text="graphMinRisk"></span>
  </div>
  <div id="graph-container" style="height: 600px; border: 1px solid #1e293b; border-radius: 8px;"></div>
</div>
```

- [ ] **Шаг 2: Логика загрузки и отрисовки.** В `web/app.js` добавить в объект Alpine-состояния:

```javascript
// web/app.js  (внутри Alpine.data / возвращаемого объекта приложения)
graphMinRisk: 40,
graphNetwork: null,

riskColor(risk) {
  if (risk >= 70) return '#dc2626';   // красный — высокий риск
  if (risk >= 40) return '#f59e0b';   // жёлтый — средний
  return '#16a34a';                   // зелёный — низкий
},

typeShape(type) {
  return type === 'post' ? 'dot' : 'square';
},

async loadGraph() {
  const resp = await fetch(`/api/graph?min_risk=${this.graphMinRisk}`);
  const data = await resp.json();
  const nodes = data.nodes.map(n => ({
    id: n.id,
    label: n.label,
    color: n.type === 'post' ? this.riskColor(n.risk)
                             : (n.high_risk ? '#dc2626' : '#64748b'),
    shape: this.typeShape(n.type),
    _type: n.type,
  }));
  const edges = data.edges.map(e => ({ from: e.source, to: e.target }));

  const container = document.getElementById('graph-container');
  this.graphNetwork = new vis.Network(
    container,
    { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) },
    { physics: { stabilization: true }, nodes: { font: { color: '#e2e8f0' } } }
  );

  this.graphNetwork.on('click', (params) => {
    if (!params.nodes.length) return;
    const nodeId = params.nodes[0];
    if (nodeId.startsWith('post:')) {
      const postId = nodeId.slice('post:'.length);
      this.openPost(postId);   // drill-down существующего поста (F1)
    }
  });
},
```

- [ ] **Шаг 3: Вызвать загрузку при переходе на вкладку.** Там, где переключается `view`, при `view === 'graph'` вызвать `this.loadGraph()` (например, в обработчике кнопки вкладки: `@click="view='graph'; loadGraph()"`).

- [ ] **Шаг 4: Ручная проверка API (без браузера).**
  Run: `python -m uvicorn app.main:app --port 8000` (в отдельном окне; после `setup.bat`/наличия БД с revealed-постами).
  Run: `curl "http://127.0.0.1:8000/api/graph?min_risk=40"`
  Expected: JSON вида `{"nodes":[{"id":"post:...","label":...,"type":"post","risk":...,"high_risk":...}, {"id":"entity:telegram:...",...}], "edges":[{"source":"post:...","target":"entity:...","type":"contains","weight":1.0}, ...]}` — присутствуют узлы `post:` и `entity:`, рёбра `source`→`target` по схеме id.
  Run: `curl "http://127.0.0.1:8000/api/graph?post_id=<любой_revealed_post_id>"`
  Expected: эго-сеть этого поста — сам пост, его сущности и co-posts.
  Ручная проверка UI: открыть `http://127.0.0.1:8000/`, перейти на вкладку «Граф связей» — узлы-посты раскрашены по риску (красный/жёлтый/зелёный), узлы-сущности квадратные (высокий риск — красный), клик по узлу-посту открывает drill-down карточку.

- [ ] **Шаг 5: Коммит.**
  Run: `git add web/index.html web/app.js`
  Run: `git commit -m "feat(graph): vis-network graph view colored by risk/type with post drill-down"`

---

## F6: Экспорт дела (PDF-досье)

**Цель:** По клику сформировать официальное PDF-досье по флагнутому посту: шапка КӨЗ/АФМ, метаданные поста, риск-оценка + категория + рекомендованное действие, доказательства по модальностям (caption, транскрипт, OCR, визуал-концепты, сущности), объяснение и футер human-in-the-loop. Доступно как `GET /api/report/{id}.pdf` с `application/pdf`.

**Зависит от:** F0 (db.py/models.py/config.py + чтение posts/extracted/scores), F3 (score_post → scores + recommended_action, explain), F2 (extracted/entities). Если этих модулей ещё нет — нельзя начинать.

---

### Задача 1: Хелпер чтения дела из БД для отчёта

Собрать в `app/report/pdf.py` загрузку поста, извлечённых признаков и скора по `post_id`. Это изолирует доступ к БД от вёрстки PDF и даёт точку для теста 404.

**Файлы:**
- Create: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\report\pdf.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_report_pdf.py`

- [ ] **Шаг 1: Написать падающий тест на загрузку дела.**
```python
# tests/test_report_pdf.py
import sqlite3
import pytest
from app import db
from app.report.pdf import load_case


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init_db()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source, revealed) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("p1", "tiktok", "@lucky_casino", "https://tiktok.com/@lucky/1",
         "Гарантированный доход 30% в месяц! Пиши в личку", "2026-06-20T10:00:00", None, None, "seed", 1),
    )
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json) "
        "VALUES(?,?,?,?,?,?,?)",
        ("p1", "Гарантированный доход 30% в месяц!", "ставь и выигрывай каждый день",
         "VAVADA BONUS 500%",
         '[{"label": "casino", "score": 0.91}, {"label": "cash_flaunt", "score": 0.77}]',
         "гарантированный доход 30% vavada bonus",
         '[{"type": "casino_brand", "value": "Vavada", "normalized": "vavada"}, '
         '{"type": "payout_promise", "value": "30% в месяц", "normalized": "30% в месяц"}]'),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("p1", 88, "gambling", '{"gambling": 0.88, "pyramid": 0.07, "fraud": 0.03, "clean": 0.02}',
         '[{"feature": "casino_brand", "weight": 0.6, "evidence": "Vavada"}, '
         '{"feature": "payout_promise", "weight": 0.4, "evidence": "30% в месяц"}]',
         "escalate", "2026-06-20T10:01:00"),
    )
    conn.commit()
    conn.close()
    return str(db_path)


def test_load_case_returns_post_extracted_score(seeded_db):
    case = load_case("p1")
    assert case is not None
    assert case.post.platform == "tiktok"
    assert case.extracted.ocr_text == "VAVADA BONUS 500%"
    assert case.score.risk == 88
    assert case.score.recommended_action == "escalate"
    assert case.entities[0].normalized == "vavada"
    assert case.visual_concepts[0].label == "casino"


def test_load_case_unknown_returns_none(seeded_db):
    assert load_case("nope") is None
```
- [ ] **Шаг 2: Запустить тест — ожидать FAIL (ImportError: load_case).**
  Run: `pytest tests/test_report_pdf.py::test_load_case_returns_post_extracted_score -v`
  Expected: `ImportError` / `ModuleNotFoundError` (нет `load_case`).
- [ ] **Шаг 3: Минимальная реализация загрузчика.** Использует существующие хелперы F0 (`db.get_post`, `db.get_extracted`, `db.get_score`); если их сигнатуры в F0 иные — заменить на прямой `db.query`. Здесь беру прямой `sqlite3`-доступ через `db.connect()` (контракт: db.py отдаёт соединение), чтобы не зависеть от точных имён хелперов.
```python
# app/report/pdf.py
from dataclasses import dataclass
import json

from app import db
from app.models import Post, Extracted, Entity, VisualConcept, Score, FeatureHit


@dataclass
class Case:
    post: Post
    extracted: Extracted
    score: Score
    entities: list
    visual_concepts: list


def _row_to_post(r) -> Post:
    return Post(
        id=r["id"], platform=r["platform"], author_handle=r["author_handle"],
        url=r["url"], caption=r["caption"], posted_at=r["posted_at"],
        media_path=r["media_path"], thumb_url=r["thumb_url"], source=r["source"],
    )


def load_case(post_id: str):
    conn = db.connect()
    conn.row_factory = __import__("sqlite3").Row
    try:
        prow = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if prow is None:
            return None
        erow = conn.execute("SELECT * FROM extracted WHERE post_id = ?", (post_id,)).fetchone()
        srow = conn.execute("SELECT * FROM scores WHERE post_id = ?", (post_id,)).fetchone()
        if erow is None or srow is None:
            return None
    finally:
        conn.close()

    visual_concepts = [VisualConcept(**vc) for vc in json.loads(erow["visual_concepts_json"] or "[]")]
    entities = [Entity(**e) for e in json.loads(erow["entities_json"] or "[]")]
    extracted = Extracted(
        post_id=erow["post_id"], caption=erow["caption"], transcript=erow["transcript"],
        ocr_text=erow["ocr_text"], visual_concepts=visual_concepts,
        combined_text=erow["combined_text"], entities=entities,
    )
    top_features = [FeatureHit(**f) for f in json.loads(srow["top_features_json"] or "[]")]
    score = Score(
        post_id=srow["post_id"], risk=srow["risk"], category=srow["category"],
        class_probs=json.loads(srow["class_probs_json"] or "{}"), top_features=top_features,
    )
    score.recommended_action = srow["recommended_action"]
    return Case(post=_row_to_post(prow), extracted=extracted, score=score,
                entities=entities, visual_concepts=visual_concepts)
```
  Примечание: `Score` в контракте не содержит `recommended_action` как поле, поэтому он лежит в таблице `scores` и здесь вешается атрибутом на объект для удобства вёрстки (PDF-слой использует `case.score.recommended_action`). Если F0 объявил `db.connect()` иначе (например `db.get_conn()`) — заменить вызов на фактическое имя из `app/db.py`.
- [ ] **Шаг 4: Запустить тест — ожидать PASS.**
  Run: `pytest tests/test_report_pdf.py -v`
  Expected: оба теста `load_case` — `PASSED`.
- [ ] **Шаг 5: Коммит.**
  Run: `git rev-parse --show-toplevel` (Expected: путь оканчивается на `AFMHACKATHON`, НЕ домашний репозиторий).
  Run: `git add app/report/pdf.py tests/test_report_pdf.py && git commit -m "feat(report): load_case reads post+extracted+score for PDF dossier"`

---

### Задача 2: Русские лейблы для рекомендованного действия и категории

Маппинг кодов (`escalate`/`review`/`monitor`/`auto_clear`, `gambling`/`pyramid`/`fraud`/`clean`) на русские подписи для отображения в досье.

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\report\pdf.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_report_pdf.py`

- [ ] **Шаг 1: Дописать падающий тест на лейблы.**
```python
# tests/test_report_pdf.py  (добавить в конец файла)
from app.report.pdf import action_label_ru, category_label_ru


def test_action_label_ru_covers_all_enum():
    assert action_label_ru("escalate") == "Эскалация"
    assert action_label_ru("review") == "На проверку"
    assert action_label_ru("monitor") == "Наблюдение"
    assert action_label_ru("auto_clear") == "Авто-очистка"
    assert action_label_ru("unknown_code") == "unknown_code"


def test_category_label_ru_covers_all_enum():
    assert category_label_ru("gambling") == "Гемблинг"
    assert category_label_ru("pyramid") == "Финансовая пирамида"
    assert category_label_ru("fraud") == "Мошенничество / реф-схема"
    assert category_label_ru("clean") == "Чисто"
    assert category_label_ru("???") == "???"
```
- [ ] **Шаг 2: Запустить — ожидать FAIL (ImportError на `action_label_ru`).**
  Run: `pytest tests/test_report_pdf.py::test_action_label_ru_covers_all_enum -v`
  Expected: `ImportError`.
- [ ] **Шаг 3: Реализация маппингов.**
```python
# app/report/pdf.py  (добавить рядом с импортами)
ACTION_LABELS_RU = {
    "escalate": "Эскалация",
    "review": "На проверку",
    "monitor": "Наблюдение",
    "auto_clear": "Авто-очистка",
}
CATEGORY_LABELS_RU = {
    "gambling": "Гемблинг",
    "pyramid": "Финансовая пирамида",
    "fraud": "Мошенничество / реф-схема",
    "clean": "Чисто",
}


def action_label_ru(action: str) -> str:
    return ACTION_LABELS_RU.get(action, action)


def category_label_ru(category: str) -> str:
    return CATEGORY_LABELS_RU.get(category, category)
```
- [ ] **Шаг 4: Запустить — ожидать PASS.**
  Run: `pytest tests/test_report_pdf.py -v`
  Expected: все тесты `PASSED`.
- [ ] **Шаг 5: Коммит.**
  Run: `git add app/report/pdf.py tests/test_report_pdf.py && git commit -m "feat(report): russian labels for action and category in dossier"`

---

### Задача 3: Сборка PDF-досье через reportlab (build_case_pdf)

Главная функция: верстает официальное досье и возвращает байты. Шапка КӨЗ/АФМ + дата; метаданные; риск/категория/действие; доказательства; объяснение; футер human-in-the-loop.

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\report\pdf.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_report_pdf.py`

- [ ] **Шаг 1: Падающий тест на `build_case_pdf`.**
```python
# tests/test_report_pdf.py  (добавить в конец)
from app.report.pdf import build_case_pdf


def test_build_case_pdf_returns_pdf_bytes(seeded_db):
    data = build_case_pdf("p1")
    assert isinstance(data, bytes)
    assert len(data) > 0
    assert data.startswith(b"%PDF")


def test_build_case_pdf_unknown_raises_keyerror(seeded_db):
    with pytest.raises(KeyError):
        build_case_pdf("does-not-exist")
```
- [ ] **Шаг 2: Запустить — ожидать FAIL (ImportError на `build_case_pdf`).**
  Run: `pytest tests/test_report_pdf.py::test_build_case_pdf_returns_pdf_bytes -v`
  Expected: `ImportError`.
- [ ] **Шаг 3: Реализация. Использует `explain()` из F3 для буллетов; шрифт регистрируем с поддержкой кириллицы (DejaVuSans, если доступен в reportlab; иначе Helvetica — кириллица в Helvetica может не отрисоваться, поэтому пробуем DejaVu first).**
```python
# app/report/pdf.py  (добавить в конец)
from io import BytesIO
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from app.decision.explain import explain

_FOOTER_RU = "Сформировано системой КӨЗ автоматически, требует проверки аналитиком."
_FONT = "Helvetica"


def _register_font():
    global _FONT
    if _FONT != "Helvetica":
        return
    import os
    candidates = [
        r"C:\Windows\Fonts\DejaVuSans.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("KozBody", path))
                _FONT = "KozBody"
                return
            except Exception:
                continue


def build_case_pdf(post_id: str) -> bytes:
    case = load_case(post_id)
    if case is None:
        raise KeyError(post_id)
    _register_font()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=18 * mm, bottomMargin=18 * mm,
        leftMargin=18 * mm, rightMargin=18 * mm,
        title=f"Досье КӨЗ — {post_id}",
    )
    base = getSampleStyleSheet()
    h_title = ParagraphStyle("KozTitle", parent=base["Title"], fontName=_FONT, fontSize=18, alignment=TA_CENTER)
    h_sub = ParagraphStyle("KozSub", parent=base["Normal"], fontName=_FONT, fontSize=10, alignment=TA_CENTER, textColor="#555555")
    h_section = ParagraphStyle("KozSection", parent=base["Heading2"], fontName=_FONT, fontSize=13)
    body = ParagraphStyle("KozBodyStyle", parent=base["Normal"], fontName=_FONT, fontSize=10, leading=14)

    def esc(s):
        s = "" if s is None else str(s)
        return s.replace("&", "&").replace("<", "<").replace(">", ">")

    story = []
    # --- Шапка ---
    story.append(Paragraph("КӨЗ · АФМ РК", h_title))
    story.append(Paragraph("Досье по материалу · AI Media Watch", h_sub))
    story.append(Paragraph(f"Дата формирования: {datetime.now().strftime('%Y-%m-%d %H:%M')}", h_sub))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=1, color="#222222"))
    story.append(Spacer(1, 10))

    # --- Метаданные поста ---
    story.append(Paragraph("Материал", h_section))
    p = case.post
    story.append(Paragraph(f"<b>Платформа:</b> {esc(p.platform)}", body))
    story.append(Paragraph(f"<b>Аккаунт:</b> {esc(p.author_handle)}", body))
    story.append(Paragraph(f"<b>Ссылка:</b> {esc(p.url)}", body))
    story.append(Paragraph(f"<b>Опубликовано:</b> {esc(p.posted_at)}", body))
    story.append(Spacer(1, 8))

    # --- Риск-оценка ---
    story.append(Paragraph("Риск-оценка", h_section))
    s = case.score
    action = getattr(s, "recommended_action", "")
    story.append(Paragraph(f"<b>Риск:</b> {esc(s.risk)} / 100", body))
    story.append(Paragraph(f"<b>Категория:</b> {esc(category_label_ru(s.category))}", body))
    story.append(Paragraph(f"<b>Рекомендованное действие:</b> {esc(action_label_ru(action))}", body))
    story.append(Spacer(1, 8))

    # --- Доказательства ---
    story.append(Paragraph("Доказательства", h_section))
    ex = case.extracted
    if ex.caption:
        story.append(Paragraph(f"<b>Подпись:</b> {esc(ex.caption)}", body))
    if ex.transcript:
        excerpt = ex.transcript[:600]
        story.append(Paragraph(f"<b>Транскрипт (фрагмент):</b> {esc(excerpt)}", body))
    if ex.ocr_text:
        story.append(Paragraph(f"<b>Текст с экрана (OCR):</b> {esc(ex.ocr_text)}", body))
    if case.visual_concepts:
        vc = ", ".join(f"{esc(c.label)} ({c.score:.2f})" for c in case.visual_concepts)
        story.append(Paragraph(f"<b>Визуальные концепты:</b> {vc}", body))
    if case.entities:
        ents = ", ".join(f"{esc(e.value)} [{esc(e.type)}]" for e in case.entities)
        story.append(Paragraph(f"<b>Сущности:</b> {ents}", body))
    story.append(Spacer(1, 8))

    # --- Объяснение ---
    story.append(Paragraph("Почему помечено", h_section))
    reasons = explain(s, case.entities)
    if not reasons:
        reasons = ["Явных признаков нарушения не выявлено."]
    for r in reasons:
        story.append(Paragraph(f"• {esc(r)}", body))
    story.append(Spacer(1, 14))

    # --- Футер human-in-the-loop ---
    story.append(HRFlowable(width="100%", thickness=0.5, color="#999999"))
    story.append(Spacer(1, 4))
    story.append(Paragraph(_FOOTER_RU, h_sub))

    doc.build(story)
    return buf.getvalue()
```
  Примечание: `explain(score, entities)` — точная сигнатура из контракта (F3). Если `s.category == "clean"` и `explain` вернул пусто — печатаем нейтральную строку, чтобы секция не была пустой.
- [ ] **Шаг 4: Запустить — ожидать PASS.**
  Run: `pytest tests/test_report_pdf.py -v`
  Expected: все тесты `PASSED`, включая `data.startswith(b"%PDF")` и `KeyError` на неизвестном id.
- [ ] **Шаг 5: Коммит.**
  Run: `git add app/report/pdf.py tests/test_report_pdf.py && git commit -m "feat(report): build_case_pdf renders official dossier via reportlab"`

---

### Задача 4: Роут GET /api/report/{id}.pdf

Отдать байты PDF с `Content-Type: application/pdf` и осмысленным именем файла; 404 для неизвестного id.

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\app\main.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_report_route.py`

- [ ] **Шаг 1: Падающий тест на роут (через FastAPI TestClient + та же фикстура БД).**
```python
# tests/test_report_route.py
import sqlite3
import pytest
from fastapi.testclient import TestClient
from app import db
from app.main import app


@pytest.fixture
def client_with_post(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init_db()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO posts(id, platform, author_handle, url, caption, posted_at, media_path, thumb_url, source, revealed) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("p1", "tiktok", "@lucky", "https://tiktok.com/@lucky/1", "Доход 30%", "2026-06-20T10:00:00", None, None, "seed", 1),
    )
    conn.execute(
        "INSERT INTO extracted(post_id, caption, transcript, ocr_text, visual_concepts_json, combined_text, entities_json) "
        "VALUES(?,?,?,?,?,?,?)",
        ("p1", "Доход 30%", "ставь и выигрывай", "VAVADA",
         '[{"label": "casino", "score": 0.9}]', "доход 30 vavada",
         '[{"type": "casino_brand", "value": "Vavada", "normalized": "vavada"}]'),
    )
    conn.execute(
        "INSERT INTO scores(post_id, risk, category, class_probs_json, top_features_json, recommended_action, scored_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("p1", 88, "gambling", '{"gambling": 0.88}',
         '[{"feature": "casino_brand", "weight": 0.6, "evidence": "Vavada"}]', "escalate", "2026-06-20T10:01:00"),
    )
    conn.commit()
    conn.close()
    return TestClient(app)


def test_report_route_returns_pdf_for_existing_post(client_with_post):
    resp = client_with_post.get("/api/report/p1.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    assert "p1" in resp.headers.get("content-disposition", "")


def test_report_route_404_for_unknown_id(client_with_post):
    resp = client_with_post.get("/api/report/nope.pdf")
    assert resp.status_code == 404
```
- [ ] **Шаг 2: Запустить — ожидать FAIL (404 для существующего поста: роута ещё нет, либо несоответствие пути).**
  Run: `pytest tests/test_report_route.py::test_report_route_returns_pdf_for_existing_post -v`
  Expected: FAIL (`assert 404 == 200`).
- [ ] **Шаг 3: Добавить роут в `app/main.py`.** Путь объявлен как `/api/report/{id}.pdf`; в FastAPI literal `.pdf` после параметра пути разрешён через path-сегмент с явным суффиксом — используем `{id}.pdf` напрямую (FastAPI матчит литеральный суффикс).
```python
# app/main.py  (добавить импорты вверху, если их ещё нет)
from fastapi import Response, HTTPException
from app.report.pdf import build_case_pdf


@app.get("/api/report/{id}.pdf")
def get_report_pdf(id: str):
    try:
        pdf_bytes = build_case_pdf(id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Материал не найден")
    headers = {"Content-Disposition": f'inline; filename="koz_dossier_{id}.pdf"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
```
  Примечание: если в проекте FastAPI не матчит `{id}.pdf` напрямую (ранние версии Starlette), заменить на `@app.get("/api/report/{filename}")` с разбором `id = filename[:-4]` и проверкой `filename.endswith(".pdf")` (иначе 404). Сначала пробуем прямой вариант — он проходит на текущих версиях Starlette.
- [ ] **Шаг 4: Запустить — ожидать PASS.**
  Run: `pytest tests/test_report_route.py -v`
  Expected: оба теста `PASSED` (200 + `application/pdf` + `%PDF` + `content-disposition` содержит `p1`; 404 на неизвестном id).
- [ ] **Шаг 5: Регрессия + коммит.**
  Run: `pytest tests/test_report_pdf.py tests/test_report_route.py -v`
  Expected: все тесты `PASSED`.
  Run: `git add app/main.py tests/test_report_route.py && git commit -m "feat(report): GET /api/report/{id}.pdf serves application/pdf dossier"`

---

### Задача 5: Ручная проверка через запущенный сервер

Убедиться, что PDF реально открывается и кириллица читаема (риск: при отсутствии TTF-шрифта Helvetica не отрисует кириллицу).

**Файлы:**
- (без изменений кода; проверка артефакта)

- [ ] **Шаг 1: Запустить сервер.**
  Run: `run.bat` (или `python -m uvicorn app.main:app --port 8000`).
  Expected: сервер слушает `:8000`, в логах нет трейсбеков.
- [ ] **Шаг 2: Скачать PDF существующего (раскрытого, проскоренного) поста.** Взять любой `id` из `GET /api/feed`.
  Run: `curl -s -D - "http://127.0.0.1:8000/api/report/<id>.pdf" -o koz_dossier.pdf`
  Expected: в заголовках `HTTP/1.1 200 OK`, `content-type: application/pdf`, `content-disposition: inline; filename="koz_dossier_<id>.pdf"`; файл `koz_dossier.pdf` создан и начинается с `%PDF`.
- [ ] **Шаг 3: Открыть PDF и проверить содержимое глазами.**
  Run: `start koz_dossier.pdf`
  Expected: видна шапка «КӨЗ · АФМ РК», дата, метаданные поста, риск/категория (на русском)/действие (на русском), секция «Доказательства» (подпись, транскрипт, OCR, концепты, сущности), буллеты «Почему помечено», и футер «Сформировано системой КӨЗ автоматически, требует проверки аналитиком.». Кириллица отрисована корректно (не «квадратики») — иначе вернуться к `_register_font()` и убедиться, что найден `arial.ttf`/`DejaVuSans.ttf`.
- [ ] **Шаг 4: Проверить 404 для несуществующего id.**
  Run: `curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8000/api/report/zzz-unknown.pdf"`
  Expected: вывод `404`.

---

## F7: Аналитика/тренды

**Цель:** Реализовать `app/analytics/trends.py:aggregate()` — сводные SQL-агрегации по скорам/постам/сущностям (по категориям, платформам, рекомендованным действиям, топ брендов казино/букмекеров, гистограмма риска, временной ряд по дням), отдать их через `GET /api/trends`, и отрисовать дашборд трендов на фронте с Chart.js (CDN): столбчатая по категориям, donut по платформам, список топ-брендов, гистограмма риска.

**Зависит от:** F0 (схема БД `app/db.py`, таблицы `posts`/`scores`, `app/config.py`), F3 (скоры с `recommended_action` в таблице `scores`), F2 (сущности в `extracted.entities_json` с типами `casino_brand`/`betting_brand`). Для фронт-задачи также нужен F1 (оболочка `web/index.html` + Alpine + Chart.js CDN). Бэкенд-агрегацию можно начинать, как только существует схема БД (F0).

---

### Задача 1: Контракт результата aggregate() — пустая БД возвращает занулённую структуру

**Файлы:**
- Create: `app/analytics/trends.py`
- Test: `tests/test_trends.py`

- [ ] **Шаг 1: Написать падающий тест на пустую БД.** Тест создаёт временную SQLite-БД через `init_db` из F0, ничего не сидит, вызывает `aggregate(conn)` и проверяет занулённую, но полную структуру (никаких `KeyError`, никакого падения).

```python
# tests/test_trends.py
import sqlite3
import pytest
from app.db import init_db
from app.analytics.trends import aggregate


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    init_db(c)
    yield c
    c.close()


def test_aggregate_empty_db_returns_zeroed_structure(conn):
    result = aggregate(conn)
    # все ключи присутствуют
    assert set(result.keys()) == {
        "total_posts",
        "by_category",
        "by_platform",
        "by_recommended_action",
        "top_brands",
        "risk_histogram",
        "time_series",
    }
    assert result["total_posts"] == 0
    # категории — все четыре класса с нулями
    assert result["by_category"] == {
        "gambling": 0,
        "pyramid": 0,
        "fraud": 0,
        "clean": 0,
    }
    # рекомендованные действия — все четыре с нулями
    assert result["by_recommended_action"] == {
        "auto_clear": 0,
        "monitor": 0,
        "review": 0,
        "escalate": 0,
    }
    # гистограмма риска — фиксированные корзины с нулями
    assert result["risk_histogram"] == {
        "0-19": 0,
        "20-39": 0,
        "40-59": 0,
        "60-79": 0,
        "80-100": 0,
    }
    # платформы и тренды пустые при пустой БД
    assert result["by_platform"] == {}
    assert result["top_brands"] == []
    assert result["time_series"] == []
```

- [ ] **Шаг 2: Запустить тест — ожидать FAIL.**
  Run: `pytest tests/test_trends.py::test_aggregate_empty_db_returns_zeroed_structure -v`
  Expected: FAIL с `ModuleNotFoundError: No module named 'app.analytics.trends'` (или `ImportError`).

- [ ] **Шаг 3: Минимальная реализация — каркас aggregate() с занулённой структурой.**

```python
# app/analytics/trends.py
"""Сводные агрегации (тренды) по постам, скорам и сущностям."""
import sqlite3

CATEGORIES = ["gambling", "pyramid", "fraud", "clean"]
RECOMMENDED_ACTIONS = ["auto_clear", "monitor", "review", "escalate"]
RISK_BUCKETS = ["0-19", "20-39", "40-59", "60-79", "80-100"]


def _empty_result() -> dict:
    return {
        "total_posts": 0,
        "by_category": {c: 0 for c in CATEGORIES},
        "by_platform": {},
        "by_recommended_action": {a: 0 for a in RECOMMENDED_ACTIONS},
        "top_brands": [],
        "risk_histogram": {b: 0 for b in RISK_BUCKETS},
        "time_series": [],
    }


def aggregate(conn: sqlite3.Connection) -> dict:
    """Построить сводный словарь трендов из таблиц posts/scores/extracted."""
    return _empty_result()
```

- [ ] **Шаг 4: Запустить тест — ожидать PASS.**
  Run: `pytest tests/test_trends.py::test_aggregate_empty_db_returns_zeroed_structure -v`
  Expected: PASS (1 passed).

- [ ] **Шаг 5: Закоммитить.**

```bash
git rev-parse --show-toplevel   # убедиться, что это репо afm-media-watch, НЕ домашний C:\Users\adlet\.git
git add app/analytics/trends.py tests/test_trends.py
git commit -m "test(analytics): aggregate() returns zeroed structure on empty db"
```

---

### Задача 2: Счётчики по категориям и рекомендованным действиям

**Файлы:**
- Modify: `app/analytics/trends.py`
- Test: `tests/test_trends.py`

- [ ] **Шаг 1: Добавить хелпер засева и падающий тест на счётчики.** Хелпер вставляет посты и скоры напрямую в таблицы (без вызова модели — это unit-тест агрегации).

```python
# tests/test_trends.py  (добавить после существующего теста)
def _seed_post(conn, post_id, platform, posted_at):
    conn.execute(
        "INSERT INTO posts (id, platform, author_handle, url, caption, "
        "posted_at, media_path, thumb_url, source, revealed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (post_id, platform, "@a", "http://x", "cap", posted_at, None, None, "seed"),
    )


def _seed_score(conn, post_id, risk, category, recommended_action):
    conn.execute(
        "INSERT INTO scores (post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) "
        "VALUES (?, ?, ?, '{}', '[]', ?, ?)",
        (post_id, risk, category, recommended_action, "2026-06-20T10:00:00"),
    )


def test_aggregate_counts_by_category_and_action(conn):
    _seed_post(conn, "p1", "tiktok", "2026-06-20T10:00:00")
    _seed_post(conn, "p2", "tiktok", "2026-06-20T11:00:00")
    _seed_post(conn, "p3", "instagram", "2026-06-21T09:00:00")
    _seed_score(conn, "p1", 85, "gambling", "escalate")
    _seed_score(conn, "p2", 50, "fraud", "review")
    _seed_score(conn, "p3", 10, "clean", "auto_clear")
    conn.commit()

    result = aggregate(conn)
    assert result["total_posts"] == 3
    assert result["by_category"] == {
        "gambling": 1,
        "pyramid": 0,
        "fraud": 1,
        "clean": 1,
    }
    assert result["by_recommended_action"] == {
        "auto_clear": 1,
        "monitor": 0,
        "review": 1,
        "escalate": 1,
    }
```

- [ ] **Шаг 2: Запустить тест — ожидать FAIL.**
  Run: `pytest tests/test_trends.py::test_aggregate_counts_by_category_and_action -v`
  Expected: FAIL — `total_posts` равен 0 (каркас возвращает занулённую структуру).

- [ ] **Шаг 3: Реализовать счётчики.** Заменить тело `aggregate()`.

```python
# app/analytics/trends.py  (заменить функцию aggregate)
def aggregate(conn: sqlite3.Connection) -> dict:
    """Построить сводный словарь трендов из таблиц posts/scores/extracted."""
    result = _empty_result()

    row = conn.execute("SELECT COUNT(*) AS n FROM posts").fetchone()
    result["total_posts"] = row["n"]

    for r in conn.execute(
        "SELECT category, COUNT(*) AS n FROM scores GROUP BY category"
    ):
        if r["category"] in result["by_category"]:
            result["by_category"][r["category"]] = r["n"]

    for r in conn.execute(
        "SELECT recommended_action, COUNT(*) AS n "
        "FROM scores GROUP BY recommended_action"
    ):
        if r["recommended_action"] in result["by_recommended_action"]:
            result["by_recommended_action"][r["recommended_action"]] = r["n"]

    return result
```

- [ ] **Шаг 4: Запустить тесты — ожидать PASS (оба теста файла).**
  Run: `pytest tests/test_trends.py -v`
  Expected: PASS (2 passed) — пустая БД и счётчики проходят.

- [ ] **Шаг 5: Закоммитить.**

```bash
git rev-parse --show-toplevel
git add app/analytics/trends.py tests/test_trends.py
git commit -m "feat(analytics): aggregate counts by category and recommended_action"
```

---

### Задача 3: Разрез по платформам и гистограмма риска

**Файлы:**
- Modify: `app/analytics/trends.py`
- Test: `tests/test_trends.py`

- [ ] **Шаг 1: Падающий тест на платформы и корзины риска.** Использует тот же фикстур `conn` и хелперы `_seed_post`/`_seed_score`.

```python
# tests/test_trends.py  (добавить)
def test_aggregate_by_platform_and_risk_histogram(conn):
    # риски подобраны по одному в каждую корзину + одна доп. в 80-100
    _seed_post(conn, "p1", "tiktok", "2026-06-20T10:00:00")
    _seed_post(conn, "p2", "tiktok", "2026-06-20T11:00:00")
    _seed_post(conn, "p3", "instagram", "2026-06-21T09:00:00")
    _seed_post(conn, "p4", "telegram", "2026-06-21T10:00:00")
    _seed_post(conn, "p5", "youtube", "2026-06-21T11:00:00")
    _seed_score(conn, "p1", 5, "clean", "auto_clear")     # 0-19
    _seed_score(conn, "p2", 25, "clean", "monitor")        # 20-39
    _seed_score(conn, "p3", 55, "fraud", "review")         # 40-59
    _seed_score(conn, "p4", 65, "gambling", "review")      # 60-79
    _seed_score(conn, "p5", 95, "gambling", "escalate")    # 80-100
    conn.commit()

    result = aggregate(conn)
    assert result["by_platform"] == {
        "tiktok": 2,
        "instagram": 1,
        "telegram": 1,
        "youtube": 1,
    }
    assert result["risk_histogram"] == {
        "0-19": 1,
        "20-39": 1,
        "40-59": 1,
        "60-79": 1,
        "80-100": 1,
    }
```

- [ ] **Шаг 2: Запустить тест — ожидать FAIL.**
  Run: `pytest tests/test_trends.py::test_aggregate_by_platform_and_risk_histogram -v`
  Expected: FAIL — `by_platform` пустой, гистограмма занулена.

- [ ] **Шаг 3: Добавить платформы и гистограмму в aggregate().** Вставить перед `return result`.

```python
# app/analytics/trends.py  (внутри aggregate, перед "return result")
    for r in conn.execute(
        "SELECT platform, COUNT(*) AS n FROM posts GROUP BY platform"
    ):
        result["by_platform"][r["platform"]] = r["n"]

    for r in conn.execute(
        """
        SELECT
            CASE
                WHEN risk < 20 THEN '0-19'
                WHEN risk < 40 THEN '20-39'
                WHEN risk < 60 THEN '40-59'
                WHEN risk < 80 THEN '60-79'
                ELSE '80-100'
            END AS bucket,
            COUNT(*) AS n
        FROM scores
        GROUP BY bucket
        """
    ):
        result["risk_histogram"][r["bucket"]] = r["n"]
```

- [ ] **Шаг 4: Запустить тесты — ожидать PASS.**
  Run: `pytest tests/test_trends.py -v`
  Expected: PASS (3 passed).

- [ ] **Шаг 5: Закоммитить.**

```bash
git rev-parse --show-toplevel
git add app/analytics/trends.py tests/test_trends.py
git commit -m "feat(analytics): aggregate by platform and risk histogram buckets"
```

---

### Задача 4: Топ брендов казино/букмекеров (по частоте) и временной ряд по дням

**Файлы:**
- Modify: `app/analytics/trends.py`
- Test: `tests/test_trends.py`

- [ ] **Шаг 1: Падающий тест на топ-бренды и временной ряд.** Бренды лежат в `extracted.entities_json` как список `Entity`-словарей с `type` в `casino_brand`/`betting_brand`; учитываем по `normalized`, считаем только посты, у которых есть запись в `extracted`. Тест добавляет хелпер засева `extracted`.

```python
# tests/test_trends.py  (добавить)
import json


def _seed_extracted(conn, post_id, entities):
    conn.execute(
        "INSERT INTO extracted (post_id, caption, transcript, ocr_text, "
        "visual_concepts_json, combined_text, entities_json) "
        "VALUES (?, '', '', '', '[]', '', ?)",
        (post_id, json.dumps(entities)),
    )


def test_aggregate_top_brands_ordered_and_time_series(conn):
    _seed_post(conn, "p1", "tiktok", "2026-06-20T10:00:00")
    _seed_post(conn, "p2", "tiktok", "2026-06-20T22:00:00")
    _seed_post(conn, "p3", "instagram", "2026-06-21T09:00:00")
    _seed_score(conn, "p1", 85, "gambling", "escalate")
    _seed_score(conn, "p2", 80, "gambling", "escalate")
    _seed_score(conn, "p3", 75, "gambling", "review")
    # "1xbet" встречается в 3 постах, "mostbet" в 1 — порядок по частоте
    _seed_extracted(conn, "p1", [
        {"type": "betting_brand", "value": "1XBET", "normalized": "1xbet"},
        {"type": "casino_brand", "value": "Mostbet", "normalized": "mostbet"},
        {"type": "telegram", "value": "@x", "normalized": "x"},
    ])
    _seed_extracted(conn, "p2", [
        {"type": "betting_brand", "value": "1xBet", "normalized": "1xbet"},
    ])
    _seed_extracted(conn, "p3", [
        {"type": "betting_brand", "value": "1xbet", "normalized": "1xbet"},
    ])
    conn.commit()

    result = aggregate(conn)
    # топ-бренды: только casino_brand/betting_brand, упорядочены по частоте убыв.
    assert result["top_brands"] == [
        {"brand": "1xbet", "count": 3},
        {"brand": "mostbet", "count": 1},
    ]
    # временной ряд по дню posted_at, упорядочен по дате
    assert result["time_series"] == [
        {"day": "2026-06-20", "count": 2},
        {"day": "2026-06-21", "count": 1},
    ]
```

- [ ] **Шаг 2: Запустить тест — ожидать FAIL.**
  Run: `pytest tests/test_trends.py::test_aggregate_top_brands_ordered_and_time_series -v`
  Expected: FAIL — `top_brands` и `time_series` пустые.

- [ ] **Шаг 3: Реализовать топ-бренды (парсинг JSON в Python) и временной ряд (SQL).** Добавить импорты вверху файла и блок перед `return result`.

```python
# app/analytics/trends.py  (вверху файла, после import sqlite3)
import json
from collections import Counter

BRAND_ENTITY_TYPES = {"casino_brand", "betting_brand"}
TOP_BRANDS_LIMIT = 10
```

```python
# app/analytics/trends.py  (внутри aggregate, перед "return result")
    brand_counter: Counter = Counter()
    for r in conn.execute("SELECT entities_json FROM extracted"):
        try:
            entities = json.loads(r["entities_json"]) or []
        except (TypeError, ValueError):
            entities = []
        for e in entities:
            if e.get("type") in BRAND_ENTITY_TYPES:
                key = e.get("normalized") or e.get("value")
                if key:
                    brand_counter[key] += 1
    # упорядочить по частоте убыв., при равенстве — по имени для детерминизма
    result["top_brands"] = [
        {"brand": brand, "count": count}
        for brand, count in sorted(
            brand_counter.items(), key=lambda kv: (-kv[1], kv[0])
        )[:TOP_BRANDS_LIMIT]
    ]

    for r in conn.execute(
        "SELECT substr(posted_at, 1, 10) AS day, COUNT(*) AS n "
        "FROM posts GROUP BY day ORDER BY day"
    ):
        result["time_series"].append({"day": r["day"], "count": r["n"]})
```

- [ ] **Шаг 4: Запустить весь файл тестов — ожидать PASS.**
  Run: `pytest tests/test_trends.py -v`
  Expected: PASS (4 passed).

- [ ] **Шаг 5: Закоммитить.**

```bash
git rev-parse --show-toplevel
git add app/analytics/trends.py tests/test_trends.py
git commit -m "feat(analytics): top casino/betting brands by frequency and daily time series"
```

---

### Задача 5: Эндпоинт GET /api/trends

**Файлы:**
- Modify: `app/main.py`
- Test: `tests/test_api_trends.py`

- [ ] **Шаг 1: Падающий тест эндпоинта через FastAPI TestClient.** Тест засевает БД приложения и проверяет, что `GET /api/trends` отдаёт корректную JSON-структуру. Использует общий способ получения соединения из F0 (`get_conn`/`get_db` в `app/db.py`).

```python
# tests/test_api_trends.py
from fastapi.testclient import TestClient
from app.main import app
from app.db import get_conn

client = TestClient(app)


def _seed(conn):
    conn.execute(
        "INSERT INTO posts (id, platform, author_handle, url, caption, "
        "posted_at, media_path, thumb_url, source, revealed) "
        "VALUES ('p1','tiktok','@a','http://x','cap','2026-06-20T10:00:00',"
        "NULL,NULL,'seed',1)"
    )
    conn.execute(
        "INSERT INTO scores (post_id, risk, category, class_probs_json, "
        "top_features_json, recommended_action, scored_at) "
        "VALUES ('p1', 90, 'gambling', '{}', '[]', 'escalate', "
        "'2026-06-20T10:00:00')"
    )
    conn.commit()


def test_get_trends_returns_aggregated_json():
    conn = get_conn()
    _seed(conn)
    resp = client.get("/api/trends")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_posts"] >= 1
    assert data["by_category"]["gambling"] >= 1
    assert data["by_platform"]["tiktok"] >= 1
    assert "risk_histogram" in data
    assert "top_brands" in data
    assert "time_series" in data
```

- [ ] **Шаг 2: Запустить тест — ожидать FAIL.**
  Run: `pytest tests/test_api_trends.py::test_get_trends_returns_aggregated_json -v`
  Expected: FAIL — маршрут `/api/trends` не зарегистрирован (404).

- [ ] **Шаг 3: Добавить маршрут в app/main.py.** Добавить импорт и обработчик (`get_conn` из F0 даёт соединение с `row_factory=sqlite3.Row`).

```python
# app/main.py  (рядом с остальными импортами)
from app.analytics.trends import aggregate
from app.db import get_conn

# app/main.py  (рядом с остальными @app.get маршрутами)
@app.get("/api/trends")
def get_trends():
    conn = get_conn()
    return aggregate(conn)
```

- [ ] **Шаг 4: Запустить тест — ожидать PASS.**
  Run: `pytest tests/test_api_trends.py -v`
  Expected: PASS (1 passed).

- [ ] **Шаг 5: Закоммитить.**

```bash
git rev-parse --show-toplevel
git add app/main.py tests/test_api_trends.py
git commit -m "feat(api): GET /api/trends serves aggregated analytics"
```

---

### Задача 6: Фронт — дашборд трендов с Chart.js (CDN)

**Файлы:**
- Modify: `web/index.html`
- Modify: `web/app.js`

- [ ] **Шаг 1: Подключить Chart.js CDN и разметку дашборда в `web/index.html`.** Добавить в `<head>` тег Chart.js, в тело — секцию трендов (Alpine `x-show="view === 'trends'"`), привязанную к существующей навигации из F1.

```html
<!-- web/index.html — в <head>, после Tailwind/Alpine CDN -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

<!-- web/index.html — в body, секция экрана «Тренды» -->
<section x-show="view === 'trends'" x-init="$watch('view', v => { if (v === 'trends') loadTrends() })" class="p-6 space-y-6">
  <h2 class="text-xl font-semibold text-slate-100">Тренды и аналитика</h2>
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
    <div class="bg-slate-800 rounded-lg p-4">
      <h3 class="text-sm text-slate-300 mb-2">По категориям</h3>
      <canvas id="chartCategory"></canvas>
    </div>
    <div class="bg-slate-800 rounded-lg p-4">
      <h3 class="text-sm text-slate-300 mb-2">По платформам</h3>
      <canvas id="chartPlatform"></canvas>
    </div>
    <div class="bg-slate-800 rounded-lg p-4">
      <h3 class="text-sm text-slate-300 mb-2">Распределение риска</h3>
      <canvas id="chartRisk"></canvas>
    </div>
    <div class="bg-slate-800 rounded-lg p-4">
      <h3 class="text-sm text-slate-300 mb-2">Топ брендов казино/букмекеров</h3>
      <ul class="space-y-1">
        <template x-for="b in trends.top_brands" :key="b.brand">
          <li class="flex justify-between text-sm text-slate-200">
            <span x-text="b.brand"></span>
            <span class="text-amber-400 font-semibold" x-text="b.count"></span>
          </li>
        </template>
        <li x-show="trends.top_brands.length === 0" class="text-slate-500 text-sm">Нет данных</li>
      </ul>
    </div>
  </div>
</section>
```

- [ ] **Шаг 2: Добавить состояние и загрузчик трендов в `web/app.js`.** В объект Alpine-приложения добавить поле `trends` и метод `loadTrends()`, который фетчит `/api/trends` и рисует три графика Chart.js. Хранить инстансы графиков, чтобы пересоздавать без утечек.

```javascript
// web/app.js — внутри объекта, возвращаемого фабрикой Alpine-приложения
trends: { total_posts: 0, by_category: {}, by_platform: {}, by_recommended_action: {}, top_brands: [], risk_histogram: {}, time_series: [] },
_charts: {},

async loadTrends() {
  const resp = await fetch('/api/trends');
  this.trends = await resp.json();
  this._renderCharts();
},

_renderChart(id, config) {
  if (this._charts[id]) { this._charts[id].destroy(); }
  const el = document.getElementById(id);
  if (el) { this._charts[id] = new Chart(el, config); }
},

_renderCharts() {
  const cat = this.trends.by_category;
  this._renderChart('chartCategory', {
    type: 'bar',
    data: {
      labels: ['Гемблинг', 'Пирамида', 'Мошенничество', 'Чисто'],
      datasets: [{
        label: 'Постов',
        data: [cat.gambling || 0, cat.pyramid || 0, cat.fraud || 0, cat.clean || 0],
        backgroundColor: ['#ef4444', '#f59e0b', '#f97316', '#22c55e'],
      }],
    },
    options: { plugins: { legend: { display: false } } },
  });

  const plat = this.trends.by_platform;
  this._renderChart('chartPlatform', {
    type: 'doughnut',
    data: {
      labels: Object.keys(plat),
      datasets: [{
        data: Object.values(plat),
        backgroundColor: ['#0ea5e9', '#a855f7', '#14b8a6', '#eab308'],
      }],
    },
  });

  const hist = this.trends.risk_histogram;
  const buckets = ['0-19', '20-39', '40-59', '60-79', '80-100'];
  this._renderChart('chartRisk', {
    type: 'bar',
    data: {
      labels: buckets,
      datasets: [{
        label: 'Постов',
        data: buckets.map(b => hist[b] || 0),
        backgroundColor: '#6366f1',
      }],
    },
    options: { plugins: { legend: { display: false } } },
  });
},
```

- [ ] **Шаг 3: Запустить бэкенд и проверить, что API отдаёт данные для графиков.**
  Run: `python -m uvicorn app.main:app --port 8000` (в отдельном окне), затем `curl -s http://127.0.0.1:8000/api/trends`
  Expected: JSON с ключами `total_posts`, `by_category`, `by_platform`, `by_recommended_action`, `top_brands`, `risk_histogram`, `time_series`. После сидирования демо (F0/F8) `by_category` и `by_platform` содержат ненулевые счётчики.

- [ ] **Шаг 4: Ручная проверка дашборда в браузере.**
  Run: открыть `http://127.0.0.1:8000/`, переключиться на вкладку «Тренды».
  Expected: видны столбчатая диаграмма по категориям (4 столбца), donut по платформам, гистограмма риска (5 корзин) и список топ-брендов с числами; при пустой БД графики рисуются с нулями и список показывает «Нет данных» — без ошибок в консоли браузера.

- [ ] **Шаг 5: Закоммитить.**

```bash
git rev-parse --show-toplevel
git add web/index.html web/app.js
git commit -m "feat(web): trends dashboard with Chart.js category/platform/risk charts and top-brands list"
```

---

## F8: Telegram-платформа (датасет + опц. live)

**Цель:** дёшево и безопасно покрыть «и другие платформы» — добавить в `data/demo_posts.jsonl` посты с `platform="telegram"` и предвычисленными признаками, чтобы они без нового рантайм-риска проходили весь пайплайн (скоринг → лента → граф), добавить telegram-специфичные подсказки сущностей и предоставить ОПЦИОНАЛЬНЫЙ, явно помеченный live-путь `fetch_telegram_channel(url)` (публичный web-preview, без авторизации, за try/except, никогда не требуется для демо).

**Зависит от:** F2 (модели `Post`/`Entity`/`Extracted`, `app/db.py`), F3 (`app/extractors/text.py` извлечение сущностей), F4 (`app/decision/scoring.py`, `RiskClassifier`), F5 (`app/graph/build.py`). Эти модули должны существовать первыми.

---

### Задача 1: Telegram-сущность из t.me-ссылки в text-экстракторе

**Файлы:**
- Modify: `app/extractors/text.py`
- Test: `tests/extractors/test_text_telegram.py`

- [ ] **Шаг 1: Написать падающий тест.** Проверяем, что из текста с `t.me`-ссылкой извлекается `Entity(type="telegram")` с нормализованным именем канала.

```python
# tests/extractors/test_text_telegram.py
from app.extractors.text import extract_entities


def test_tme_link_yields_telegram_entity():
    text = "Заносы тут 👉 https://t.me/Casino_Win_KZ пиши в личку"
    ents = extract_entities(text)
    tg = [e for e in ents if e.type == "telegram"]
    assert tg, "ожидали хотя бы одну telegram-сущность"
    assert tg[0].normalized == "casino_win_kz"
    assert tg[0].value in ("https://t.me/Casino_Win_KZ", "t.me/Casino_Win_KZ", "@Casino_Win_KZ")


def test_at_handle_in_telegram_context_yields_telegram_entity():
    text = "Подписывайся: Telegram @VipStavkaBot — бонус 200%"
    ents = extract_entities(text)
    tg = [e for e in ents if e.type == "telegram"]
    assert tg, "ожидали telegram-сущность из @-хэндла в Telegram-контексте"
    assert tg[0].normalized == "vipstavkabot"
```

- [ ] **Шаг 2: Запустить тест (ожидаем fail).**

```
pytest tests/extractors/test_text_telegram.py -v
```
Ожидаемо: FAIL (`t.me` пока даёт `url`/`handle`, но не нормализованную `telegram`-сущность, либо нормализация отличается).

- [ ] **Шаг 3: Минимальная реализация.** В `app/extractors/text.py` добавить выделенный проход для `t.me` ссылок и `@`-хэндлов в telegram-контексте. Вставить в `extract_entities` (или вспомогательную функцию, которую она вызывает) перед общим url/handle-проходом, чтобы telegram имел приоритет.

```python
import re
from app.models import Entity

# t.me/<channel>  либо  telegram.me/<channel>  либо  https://t.me/<channel>/123
_TME_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?P<chan>[A-Za-z0-9_]{3,})",
    re.IGNORECASE,
)
# @handle, встречающийся рядом со словом telegram/телеграм/тг
_TG_CTX_RE = re.compile(
    r"(?:telegram|телеграм|тг)\W{0,12}@(?P<chan>[A-Za-z0-9_]{3,})",
    re.IGNORECASE,
)


def _telegram_entities(text: str) -> list[Entity]:
    found: dict[str, Entity] = {}
    for m in _TME_RE.finditer(text):
        chan = m.group("chan")
        norm = chan.lower()
        found[norm] = Entity(type="telegram", value=m.group(0), normalized=norm)
    for m in _TG_CTX_RE.finditer(text):
        chan = m.group("chan")
        norm = chan.lower()
        found.setdefault(norm, Entity(type="telegram", value="@" + chan, normalized=norm))
    return list(found.values())
```

Затем в `extract_entities` собрать telegram-сущности первыми и не дублировать те же строки как `url`/`handle`:

```python
def extract_entities(text: str) -> list[Entity]:
    text = text or ""
    entities: list[Entity] = []
    tg = _telegram_entities(text)
    entities.extend(tg)
    tg_norms = {e.normalized for e in tg}
    # ... существующие проходы (casino_brand, promo_code, crypto_wallet, url, handle, phone, payout_promise) ...
    # при добавлении handle/url пропускать совпадающие с telegram-каналом:
    #   if normalized in tg_norms: continue
    return entities
```

- [ ] **Шаг 4: Запустить тест (ожидаем pass).**

```
pytest tests/extractors/test_text_telegram.py -v
```
Ожидаемо: 2 passed. Также прогнать `pytest tests/extractors -v`, чтобы не сломать существующие сущности.

- [ ] **Шаг 5: Коммит.**

```
git add app/extractors/text.py tests/extractors/test_text_telegram.py
git commit -m "feat(extractors): извлечение telegram-сущностей из t.me ссылок и @-хэндлов"
```

---

### Задача 2: Хелпер сборки telegram-поста с кэш-признаками в build_demo

**Файлы:**
- Modify: `scripts/build_demo.py`
- Test: `tests/scripts/test_build_demo_telegram.py`

- [ ] **Шаг 1: Написать падающий тест.** Проверяем, что хелпер собирает запись поста+признаков с `platform="telegram"` и `source="telegram"`, telegram-сущность присутствует.

```python
# tests/scripts/test_build_demo_telegram.py
from scripts.build_demo import make_telegram_post


def test_make_telegram_post_shape():
    rec = make_telegram_post(
        post_id="tg_001",
        author_handle="@casino_win_kz",
        caption="🎰 Гарантированный доход 300% за неделю! Жми https://t.me/Casino_Win_KZ",
        posted_at="2026-06-23T10:00:00",
    )
    post = rec["post"]
    extracted = rec["extracted"]
    assert post["id"] == "tg_001"
    assert post["platform"] == "telegram"
    assert post["source"] == "telegram"
    assert post["url"].startswith("https://t.me/")
    assert extracted["post_id"] == "tg_001"
    # telegram-сущность предвычислена в кэше
    types = {e["type"] for e in extracted["entities"]}
    assert "telegram" in types
    assert extracted["combined_text"]  # непустой текст для скоринга
```

- [ ] **Шаг 2: Запустить тест (ожидаем fail).**

```
pytest tests/scripts/test_build_demo_telegram.py -v
```
Ожидаемо: FAIL (`make_telegram_post` не существует).

- [ ] **Шаг 3: Минимальная реализация.** В `scripts/build_demo.py` добавить хелпер. Он строит сериализуемый словарь поста и предвычисленного `Extracted` (включая сущности через реальный экстрактор F3 — никаких тяжёлых моделей).

```python
from app.extractors.text import extract_entities


def make_telegram_post(post_id: str, author_handle: str, caption: str,
                       posted_at: str, channel_url: str | None = None) -> dict:
    handle = author_handle if author_handle.startswith("@") else "@" + author_handle
    url = channel_url or f"https://t.me/{handle.lstrip('@')}"
    entities = extract_entities(caption)
    combined_text = caption  # для telegram нет audio/ocr/visual — текст и есть сигнал
    post = {
        "id": post_id,
        "platform": "telegram",
        "author_handle": handle,
        "url": url,
        "caption": caption,
        "posted_at": posted_at,
        "media_path": None,
        "thumb_url": None,
        "source": "telegram",
    }
    extracted = {
        "post_id": post_id,
        "caption": caption,
        "transcript": "",
        "ocr_text": "",
        "visual_concepts": [],
        "combined_text": combined_text,
        "entities": [
            {"type": e.type, "value": e.value, "normalized": e.normalized}
            for e in entities
        ],
    }
    return {"post": post, "extracted": extracted}
```

- [ ] **Шаг 4: Запустить тест (ожидаем pass).**

```
pytest tests/scripts/test_build_demo_telegram.py -v
```
Ожидаемо: 1 passed.

- [ ] **Шаг 5: Коммит.**

```
git add scripts/build_demo.py tests/scripts/test_build_demo_telegram.py
git commit -m "feat(build_demo): хелпер make_telegram_post с предвычисленными признаками"
```

---

### Задача 3: Эмитировать минимум один telegram-пост в demo_posts.jsonl

**Файлы:**
- Modify: `scripts/build_demo.py`
- Test: `tests/scripts/test_demo_has_telegram.py`

- [ ] **Шаг 1: Написать падающий тест.** Проверяем, что собранный список демо-постов содержит хотя бы один telegram-пост с непустыми сущностями.

```python
# tests/scripts/test_demo_has_telegram.py
from scripts.build_demo import build_demo_records


def test_demo_includes_at_least_one_telegram_post():
    records = build_demo_records()
    tg = [r for r in records if r["post"]["platform"] == "telegram"]
    assert len(tg) >= 1, "в демо-датасете должен быть хотя бы один telegram-пост"
    rec = tg[0]
    assert rec["post"]["source"] == "telegram"
    assert rec["extracted"]["entities"], "у telegram-поста должны быть предвычисленные сущности"
    # рискованный контент: содержит telegram-сущность для участия в графе
    assert any(e["type"] == "telegram" for e in rec["extracted"]["entities"])
```

- [ ] **Шаг 2: Запустить тест (ожидаем fail).**

```
pytest tests/scripts/test_demo_has_telegram.py -v
```
Ожидаемо: FAIL (`build_demo_records` пока не добавляет telegram-постов; либо функция собирает только seed-посты).

- [ ] **Шаг 3: Минимальная реализация.** В функцию-сборщик `build_demo_records()` (которая возвращает список `{"post":..., "extracted":...}` перед записью в jsonl) добавить вызовы `make_telegram_post`. Минимум один рисковый telegram-пост, переиспользующий тот же telegram-канал, что и какой-нибудь tiktok/instagram-пост, — чтобы граф связал их по общей сущности.

```python
def build_demo_records() -> list[dict]:
    records: list[dict] = []
    # ... существующие seed-посты (tiktok/instagram/youtube) ...

    # --- Telegram-платформа (P4): дёшево покрываем «и другие платформы» ---
    records.append(make_telegram_post(
        post_id="tg_001",
        author_handle="@Casino_Win_KZ",
        caption=("🎰💰 Гарантированный доход 300% за неделю! Вывод сразу. "
                 "Реальные заносы каждый день. Пиши в личку 👉 https://t.me/Casino_Win_KZ "
                 "Промокод KOZ300"),
        posted_at="2026-06-23T09:15:00",
    ))
    records.append(make_telegram_post(
        post_id="tg_002",
        author_handle="@VipStavka",
        caption=("Финансовая свобода с нашим клубом 📈 Заносим по 500 000 ₸ в день. "
                 "Реферальная программа: приведи друга — получи бонус. "
                 "Канал: https://t.me/Casino_Win_KZ"),
        posted_at="2026-06-23T11:40:00",
    ))
    return records
```

> Примечание: оба telegram-поста и (по возможности) уже существующий tiktok/instagram-пост ссылаются на канал `Casino_Win_KZ` → общая `telegram`-сущность → ребро в графе F5.

- [ ] **Шаг 4: Запустить тест (ожидаем pass).**

```
pytest tests/scripts/test_demo_has_telegram.py -v
```
Ожидаемо: 1 passed.

- [ ] **Шаг 5: Перегенерировать датасет и проверить вручную.**

```
python scripts/build_demo.py
python -c "import json; rows=[json.loads(l) for l in open('data/demo_posts.jsonl',encoding='utf-8')]; print('telegram posts:', sum(1 for r in rows if r['post']['platform']=='telegram'))"
```
Ожидаемо: печатает `telegram posts: 2` (или больше).

- [ ] **Шаг 6: Коммит.**

```
git add scripts/build_demo.py tests/scripts/test_demo_has_telegram.py data/demo_posts.jsonl
git commit -m "feat(build_demo): эмитировать telegram-посты с общей сущностью для графа"
```

---

### Задача 4: Telegram-пост скорится и попадает в /api/feed

**Файлы:**
- Test: `tests/integration/test_telegram_feed.py`

- [ ] **Шаг 1: Написать падающий тест.** Интеграционный сквозняк: загрузить telegram-пост из `build_demo`, прогнать через `score_post` (F4), записать в БД, раскрыть (`revealed=1`), запросить `/api/feed` — telegram-пост присутствует со скором.

```python
# tests/integration/test_telegram_feed.py
import json
from fastapi.testclient import TestClient

from app.main import app
from app import db
from app.models import Post, Extracted, VisualConcept, Entity
from app.decision.scoring import score_post


def _to_post(d: dict) -> Post:
    return Post(**d)


def _to_extracted(d: dict) -> Extracted:
    return Extracted(
        post_id=d["post_id"], caption=d["caption"], transcript=d["transcript"],
        ocr_text=d["ocr_text"],
        visual_concepts=[VisualConcept(**v) for v in d["visual_concepts"]],
        combined_text=d["combined_text"],
        entities=[Entity(**e) for e in d["entities"]],
    )


def test_telegram_post_scores_and_appears_in_feed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    from scripts.build_demo import build_demo_records
    tg = next(r for r in build_demo_records() if r["post"]["platform"] == "telegram")
    post = _to_post(tg["post"])
    extracted = _to_extracted(tg["extracted"])
    db.insert_post(post)
    db.insert_extracted(extracted)
    score = score_post(post, extracted)
    assert 0 <= score.risk <= 100
    db.reveal_post(post.id)  # revealed=1

    client = TestClient(app)
    resp = client.get("/api/feed?limit=50")
    assert resp.status_code == 200
    ids = [row["post"]["id"] for row in resp.json()]
    assert post.id in ids, "telegram-пост должен попасть в ленту после раскрытия"
    row = next(r for r in resp.json() if r["post"]["id"] == post.id)
    assert row["post"]["platform"] == "telegram"
    assert "score" in row and "risk" in row["score"]
```

> Имена хелперов БД (`insert_post`, `insert_extracted`, `reveal_post`, `init_db`, `DB_PATH`) и сигнатура `/api/feed` — из контракта F2/main. Если фактические имена в F2 отличаются, подставить их без изменения проверяемого поведения.

- [ ] **Шаг 2: Запустить тест (ожидаем fail).**

```
pytest tests/integration/test_telegram_feed.py -v
```
Ожидаемо: FAIL до того, как Задачи 1–3 смержены и БД-хелперы доступны; затем проверяет сквозной поток.

- [ ] **Шаг 3: Минимальная реализация.** Кода новой логики не требуется — telegram-пост уже течёт через общий пайплайн (Задачи 1–3 + существующие F2/F4). Если тест падает из-за того, что `/api/feed` не возвращает `platform` в объекте `post`, убедиться, что сериализация поста в `app/main.py` включает все поля `Post` (включая `platform`). Правка точечная — добавить недостающее поле в ответ feed, не меняя контракт.

- [ ] **Шаг 4: Запустить тест (ожидаем pass).**

```
pytest tests/integration/test_telegram_feed.py -v
```
Ожидаемо: 1 passed.

- [ ] **Шаг 5: Коммит.**

```
git add tests/integration/test_telegram_feed.py
git commit -m "test(integration): telegram-пост скорится и попадает в /api/feed"
```

---

### Задача 5: Telegram-посты связываются в build_graph по общей сущности

**Файлы:**
- Test: `tests/graph/test_graph_telegram.py`

- [ ] **Шаг 1: Написать падающий тест.** Проверяем, что два telegram-поста, делящие канал `casino_win_kz`, соединены через общий узел сущности в `build_graph` (F5).

```python
# tests/graph/test_graph_telegram.py
from app import db
from app.models import Post, Extracted, VisualConcept, Entity
from app.graph.build import build_graph


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "g.db"))
    db.init_db()
    from scripts.build_demo import build_demo_records
    recs = [r for r in build_demo_records() if r["post"]["platform"] == "telegram"][:2]
    for r in recs:
        p = Post(**r["post"])
        e = Extracted(
            post_id=r["extracted"]["post_id"], caption=r["extracted"]["caption"],
            transcript=r["extracted"]["transcript"], ocr_text=r["extracted"]["ocr_text"],
            visual_concepts=[VisualConcept(**v) for v in r["extracted"]["visual_concepts"]],
            combined_text=r["extracted"]["combined_text"],
            entities=[Entity(**en) for en in r["extracted"]["entities"]],
        )
        db.insert_post(p)
        db.insert_extracted(e)
    return [r["post"]["id"] for r in recs]


def test_telegram_posts_share_entity_node(tmp_path, monkeypatch):
    ids = _seed(tmp_path, monkeypatch)
    graph = build_graph(ids)
    node_ids = {n["id"] for n in graph["nodes"]}
    # узел telegram-сущности присутствует
    tg_node = "entity:telegram:casino_win_kz"
    assert tg_node in node_ids, f"ожидали узел общей telegram-сущности, узлы: {node_ids}"
    # оба поста соединены с этим узлом
    touching = {
        (e["source"], e["target"]) for e in graph["edges"]
        if tg_node in (e["source"], e["target"])
    }
    post_nodes_linked = {
        n for pair in touching for n in pair if n.startswith("post:")
    }
    assert len(post_nodes_linked) >= 2, "оба telegram-поста должны делить узел сущности"
```

- [ ] **Шаг 2: Запустить тест (ожидаем fail).**

```
pytest tests/graph/test_graph_telegram.py -v
```
Ожидаемо: FAIL, если узел сущности именуется иначе или Задачи 1–3 не смержены. Имя узла `entity:<type>:<normalized>` фиксировано контрактом (Edge), поэтому формат `entity:telegram:casino_win_kz` обязателен.

- [ ] **Шаг 3: Минимальная реализация.** Новый код не нужен: `build_graph` (F5) строит рёбра из общих сущностей `extracted.entities`, а `telegram`-сущность уже извлечена (Задача 1) и закэширована (Задача 2). Если тест падает, проверить, что F5 не фильтрует `type == "telegram"` из набора сущностей-узлов — telegram должен быть в списке типов, формирующих узлы. При необходимости расширить allow-list типов сущностей в `app/graph/build.py`, включив `"telegram"` (точечная правка, без смены контракта).

- [ ] **Шаг 4: Запустить тест (ожидаем pass).**

```
pytest tests/graph/test_graph_telegram.py -v
```
Ожидаемо: 1 passed.

- [ ] **Шаг 5: Коммит.**

```
git add tests/graph/test_graph_telegram.py
git commit -m "test(graph): telegram-посты связываются по общей t.me сущности"
```

---

### Задача 6: Опциональный live-fetch публичного telegram-канала (best-effort)

**Файлы:**
- Modify: `app/ingestion/fetch.py`
- Test: `tests/ingestion/test_fetch_telegram.py`

- [ ] **Шаг 1: Написать падающий тест.** Две проверки: (а) при недоступной сети/ошибке функция деградирует — возвращает `[]` и не пробрасывает исключение; (б) при подсунутом HTML web-preview парсит публичные сообщения в `Post` с `platform="telegram"`, `source="live"`.

```python
# tests/ingestion/test_fetch_telegram.py
from app.ingestion.fetch import fetch_telegram_channel

SAMPLE_HTML = """
<div class="tgme_widget_message" data-post="Casino_Win_KZ/12">
  <div class="tgme_widget_message_text">Гарантированный доход 300% жми t.me/Casino_Win_KZ</div>
  <a class="tgme_widget_message_date" href="https://t.me/Casino_Win_KZ/12">
    <time datetime="2026-06-23T09:15:00+00:00"></time>
  </a>
</div>
"""


def test_fetch_failure_degrades_to_empty(monkeypatch):
    def boom(*a, **k):
        raise OSError("сеть недоступна")
    monkeypatch.setattr("app.ingestion.fetch._http_get", boom)
    posts = fetch_telegram_channel("https://t.me/Casino_Win_KZ")
    assert posts == [], "при ошибке сети должен вернуться пустой список, без исключения"


def test_fetch_parses_public_preview(monkeypatch):
    monkeypatch.setattr("app.ingestion.fetch._http_get", lambda url: SAMPLE_HTML)
    posts = fetch_telegram_channel("https://t.me/Casino_Win_KZ")
    assert len(posts) >= 1
    p = posts[0]
    assert p.platform == "telegram"
    assert p.source == "live"
    assert "300%" in p.caption
    assert p.url.startswith("https://t.me/Casino_Win_KZ")
    assert p.author_handle == "@Casino_Win_KZ"
```

- [ ] **Шаг 2: Запустить тест (ожидаем fail).**

```
pytest tests/ingestion/test_fetch_telegram.py -v
```
Ожидаемо: FAIL (`fetch_telegram_channel` и `_http_get` не существуют).

- [ ] **Шаг 3: Минимальная реализация.** В `app/ingestion/fetch.py` добавить функции. Парсинг — стандартный `html.parser` (без новых зависимостей); сеть — `urllib.request`. Весь путь за `try/except`, никогда не требуется для демо.

```python
import re
import urllib.request
from html.parser import HTMLParser
from html import unescape
from app.models import Post

_TGME_PREVIEW = "https://t.me/s/{channel}"


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (KOZ media-watch)"})
    with urllib.request.urlopen(req, timeout=8) as resp:  # nosec - публичный web-preview
        return resp.read().decode("utf-8", errors="replace")


class _TgPreviewParser(HTMLParser):
    """Best-effort парсер публичного t.me/s/<channel> превью."""
    def __init__(self):
        super().__init__()
        self._in_text = 0
        self._buf: list[str] = []
        self.messages: list[str] = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if "tgme_widget_message_text" in cls:
            self._in_text += 1
            self._buf = []

    def handle_endtag(self, tag):
        if self._in_text and tag == "div":
            self._in_text -= 1
            if self._in_text == 0:
                txt = unescape("".join(self._buf)).strip()
                if txt:
                    self.messages.append(txt)

    def handle_data(self, data):
        if self._in_text:
            self._buf.append(data)


def _channel_from_url(url: str) -> str:
    m = re.search(r"t\.me/(?:s/)?(?P<chan>[A-Za-z0-9_]+)", url)
    return m.group("chan") if m else url.rstrip("/").split("/")[-1]


def fetch_telegram_channel(url: str, limit: int = 10) -> list[Post]:
    """ОПЦИОНАЛЬНЫЙ live-путь: публичный web-preview Telegram, без авторизации.
    Best-effort: при любой ошибке возвращает [] и НЕ ломает запрос.
    Для демо не требуется — ядро работает на кэш-датасете."""
    try:
        channel = _channel_from_url(url)
        html = _http_get(_TGME_PREVIEW.format(channel=channel))
        parser = _TgPreviewParser()
        parser.feed(html)
        posts: list[Post] = []
        for i, text in enumerate(parser.messages[:limit]):
            posts.append(Post(
                id=f"tglive_{channel}_{i}",
                platform="telegram",
                author_handle="@" + channel,
                url=f"https://t.me/{channel}",
                caption=text,
                posted_at="",
                media_path=None,
                thumb_url=None,
                source="live",
            ))
        return posts
    except Exception:
        # деградация: понятное пустое поведение, запрос не падает
        return []
```

> Примечание: тест мокает `_http_get`, поэтому реальной сети при прогоне тестов нет. `posted_at=""` допустимо — формат строки сохраняется; при наличии `<time datetime=...>` можно дополнить, но для MVP не обязательно.

- [ ] **Шаг 4: Запустить тест (ожидаем pass).**

```
pytest tests/ingestion/test_fetch_telegram.py -v
```
Ожидаемо: 2 passed.

- [ ] **Шаг 5: Коммит.**

```
git add app/ingestion/fetch.py tests/ingestion/test_fetch_telegram.py
git commit -m "feat(ingestion): опц. best-effort live-fetch публичного telegram-канала"
```

---

### Задача 7: Деградация live-fetch не ломает /api/analyze

**Файлы:**
- Modify: `app/main.py`
- Test: `tests/integration/test_analyze_telegram_degrade.py`

- [ ] **Шаг 1: Написать падающий тест.** Если в `/api/analyze` передана `t.me`-ссылка, а live-fetch недоступен, запрос должен вернуть понятное сообщение (HTTP 200 c `detail`/`message` или 422 с русским пояснением), а не 500.

```python
# tests/integration/test_analyze_telegram_degrade.py
from fastapi.testclient import TestClient
from app.main import app
from app import ingestion  # noqa
import app.ingestion.fetch as fetch_mod


def test_analyze_telegram_live_failure_returns_clear_message(monkeypatch):
    monkeypatch.setattr(fetch_mod, "fetch_telegram_channel", lambda url, limit=10: [])
    client = TestClient(app)
    resp = client.post("/api/analyze", json={"url": "https://t.me/Casino_Win_KZ"})
    assert resp.status_code in (200, 422)
    body = resp.json()
    text = str(body).lower()
    assert ("telegram" in text or "канал" in text or "сообщени" in text), \
        "ожидали понятное русское сообщение о деградации live-fetch"
    assert "500" not in str(resp.status_code)
```

- [ ] **Шаг 2: Запустить тест (ожидаем fail).**

```
pytest tests/integration/test_analyze_telegram_degrade.py -v
```
Ожидаемо: FAIL (маршрут `/api/analyze` пока не распознаёт t.me-ссылки и/или падает при пустом результате).

- [ ] **Шаг 3: Минимальная реализация.** В обработчике `POST /api/analyze` в `app/main.py` добавить ветку для t.me-ссылок: вызвать `fetch_telegram_channel`; если пусто — вернуть понятный русский ответ без 500.

```python
from fastapi import HTTPException
from app.ingestion.fetch import fetch_telegram_channel

# внутри обработчика /api/analyze, после получения url из тела запроса:
if url and ("t.me/" in url or "telegram.me/" in url):
    posts = fetch_telegram_channel(url)
    if not posts:
        raise HTTPException(
            status_code=422,
            detail="Не удалось получить публичные сообщения Telegram-канала. "
                   "Live-разбор Telegram доступен только для открытых каналов; "
                   "основной анализ работает на загруженном видео или ссылке TikTok/Instagram.",
        )
    # взять первое публичное сообщение как пост для разбора
    post = posts[0]
    # далее — общий пайплайн extract -> score_post (как для прочих источников)
    # extracted = extract(post); score = score_post(post, extracted); return {...}
```

> Точечная правка одной ветки. Никакого исключения наружу: либо валидный разбор первого публичного сообщения, либо понятный 422 на русском. Демо не зависит от этой ветки.

- [ ] **Шаг 4: Запустить тест (ожидаем pass).**

```
pytest tests/integration/test_analyze_telegram_degrade.py -v
```
Ожидаемо: 1 passed.

- [ ] **Шаг 5: Полный прогон F8-тестов и коммит.**

```
pytest tests/extractors/test_text_telegram.py tests/scripts/test_build_demo_telegram.py tests/scripts/test_demo_has_telegram.py tests/integration/test_telegram_feed.py tests/graph/test_graph_telegram.py tests/ingestion/test_fetch_telegram.py tests/integration/test_analyze_telegram_degrade.py -v
git add app/main.py tests/integration/test_analyze_telegram_degrade.py
git commit -m "feat(api): /api/analyze деградирует при недоступном telegram live-fetch без 500"
```
Ожидаемо: все F8-тесты passed.

---

## F9: Питч, демо-скрипт и dry-run/фоллбэки

**Цель:** Подготовить материалы защиты (3-минутный питч под 5 критериев, click-by-click демо-скрипт с лестницей фоллбэков, Q&A-заготовки) и автоматический предзащитный smoke-тест `scripts/dry_run.py`, который поднимает приложение, тикает ленту и проверяет, что все ключевые эндпоинты отвечают 200.

**Зависит от:** для финального прогона `scripts/dry_run.py` нужны F1 (app/main.py + все роуты + ticker), F2-F7 (наполнение данными, модель, граф, PDF, тренды) — то есть фактически вся интеграция. НО все три документа (pitch / demo-script / qa-prep) можно писать параллельно, не дожидаясь кода. dry_run пишем через TDD против контракта API сразу, прогон с реальным сервером — в самом конце.

---

### Задача 1: Питч-скрипт под 5 критериев + структура слайдов

**Файлы:**
- Create: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\docs\pitch\koz-pitch.md`

- [ ] **Шаг 1: Создать `docs/pitch/koz-pitch.md`** — 3-минутный скрипт, где каждый блок явно привязан к одному из 5 критериев оценки, плюс план слайдов. Содержимое:

```markdown
# Питч «КӨЗ» (KÓZ) — AI Media Watch · 3 минуты

> Хронометраж жёсткий: 5 блоков, ~3:00. Один спикер. Слайды переключаются по таймкодам.

## Тайминг и привязка к критериям (каждый критерий = 20 баллов)

### 0:00–0:30 — Проблема + актуальность (Критерий №1: актуальность + обоснование AI)
«В соцсетях Казахстана ежедневно публикуются тысячи роликов с рекламой нелицензированных
онлайн-казино, финансовых пирамид и реферальных схем. Вручную мониторить TikTok, Instagram
и Telegram физически невозможно — это и есть обоснование AI: масштаб не разбирается руками.
Мы сделали "КӨЗ" — каз. "глаз" — ИИ-глаз АФМ над соцсетями.»

### 0:30–1:15 — Решение + лента (Критерий №5: качество презентации; задел под №1)
«Вот аналитическая консоль. Посты "капают" в ленту в реальном времени — это симуляция
непрерывного потока. Каждый автоматически оценивается нашей моделью и встаёт в приоритетную
очередь по риску: красный → жёлтый → зелёный. Аналитик сразу видит, что проверять первым.»
(Показать сортировку по риску, бейджи категории и платформы.)

### 1:15–2:00 — Своя модель + метрики + мультимодал + drill-down (Критерий №2: собственная AI-модель, независимая от внешних сервисов)
«Кликаем по посту с верхним риском. Скоринг даёт НАША обученная модель — мультиязычные
эмбеддинги RU+KZ плюс наши инженерные признаки и обученная нами голова-классификатор.
Никаких внешних платных API в скоринге — всё локально. Вот метрики: F1 по классам и confusion
matrix. В drill-down — доказательства по всем модальностям: транскрипт (Whisper), текст с
экрана (OCR), визуальные концепты (CLIP), сработавшие паттерны и объяснение "почему опасно"
на русском. Жмём "Экспорт дела" — готовое PDF-досье.»

### 2:00–2:30 — Граф связей + live (Критерий №2 продолжение + №1: скрытые взаимосвязи)
«Граф связей: эти аккаунты пушат один и тот же бренд казино через один Telegram-канал и
один промокод — координированная сеть, видно сразу. И живой путь: вставляем ссылку или
грузим видео — реальный мультимодальный разбор прямо на сцене → вердикт.»

### 2:30–3:00 — Этика, масштаб, призыв (Критерий №3: ИБ+этика; Критерий №4: реализуемость+масштаб)
«Этика встроена: только публичные данные, минимизация PII, human-in-the-loop — система
приоритизирует для проверки, а не блокирует и не обвиняет автоматически; решение за аналитиком.
Каждый флаг объясним, все решения пишутся в аудит-лог, порог калибруется в пользу precision.
Масштаб: модульные stateless-экстракторы, путь MVP на SQLite → прод на Postgres + очередь +
объектное хранилище. КӨЗ закрывает формулировку трека один-в-один: собрать → разобрать →
оценить риск → объяснить → приоритизировать.»

## План слайдов (5 слайдов + титул)
0. **Титул** — «КӨЗ (KÓZ) · AI Media Watch · АФМ AI Hackathon 2026».
1. **Проблема** — масштаб мошеннической рекламы в соцсетях РК; почему руками нельзя → AI обоснован.
2. **Решение** — скриншот консоли: лента + приоритетная очередь по риску.
3. **Своя модель + метрики** — схема (эмбеддинги + наши признаки + наша голова), F1 по классам, confusion matrix; явно: «без внешних API в скоринге».
4. **Мультимодал + граф** — drill-down с доказательствами по модальностям + граф координированной сети.
5. **Этика + масштаб** — публичные данные, human-in-the-loop, объяснимость, аудит-лог; путь MVP→прод.

## Привязка критерий → артефакт (чек перед защитой)
- №1 Актуальность+AI → блоки 0:00 и 2:00, слайд 1.
- №2 Своя модель → блок 1:15, слайд 3, экран /api/metrics.
- №3 Этика+ИБ → блок 2:30, слайд 5, аудит-лог + объяснимость.
- №4 Масштабируемость → блок 2:30, слайд 5, схема MVP→прод.
- №5 Презентация → вся консоль + сквозной демо без сбоев (см. demo-script.md).
```

- [ ] **Шаг 2: Проверить** — Run: `python -c "import pathlib,sys; t=pathlib.Path(r'docs/pitch/koz-pitch.md').read_text(encoding='utf-8'); req=['Критерий №1','Критерий №2','Критерий №3','Критерий №4','Критерий №5','План слайдов']; missing=[k for k in req if k not in t]; print('OK' if not missing else 'MISSING: '+str(missing)); sys.exit(0 if not missing else 1)"` — Expected: печатает `OK`, exit code 0 (все 5 критериев и план слайдов присутствуют).
- [ ] **Шаг 3: Коммит** — `git add docs/pitch/koz-pitch.md` затем `git commit -m "docs(pitch): 3-min pitch script mapped to 5 judging criteria + slide outline"`.

---

### Задача 2: Click-by-click демо-скрипт + лестница фоллбэков

**Файлы:**
- Create: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\docs\pitch\demo-script.md`

- [ ] **Шаг 1: Создать `docs/pitch/demo-script.md`** — точный пошаговый прогон демо за 3 минуты с конкретными кликами/эндпоинтами и лестница деградации. Содержимое:

```markdown
# Демо-скрипт «КӨЗ» — пошагово (3 минуты)

## Подготовка (до выхода на сцену, НЕ в зачёт времени)
1. Запустить smoke-тест: `python scripts/dry_run.py` — должно напечатать `DRY-RUN OK` и вернуть 0.
2. Поднять приложение: `run.bat` (поднимает venv + uvicorn на http://127.0.0.1:8000).
3. Открыть в браузере `http://127.0.0.1:8000/` — видна лента с уже раскрытыми постами.
4. Подготовить во второй вкладке `http://127.0.0.1:8000/api/metrics` (метрики модели).
5. Если есть сеть — заранее открыть страницу с тестовой ссылкой для live-разбора (см. Шаг 4).

## Прогон (3:00)

### Блок 1 (0:00–0:30) — Проблема + лента
- Кадр: главный экран `/`, лента отсортирована по риску.
- Действие: нажать кнопку «Тик» (вызывает `POST /api/tick`) 1–2 раза — новые посты «капают» в ленту и авто-скорятся.
- Сказать: масштаб проблемы → почему AI → приоритетная очередь.

### Блок 2 (0:30–1:45) — Drill-down + доказательства + PDF
- Действие: кликнуть верхний (красный) пост → открывается карточка `GET /api/post/{id}`.
- Показать: транскрипт, OCR-текст, визуал-концепты, список сработавших паттернов (top_features),
  блок «Почему опасно» (объяснение на русском), рекомендованное действие (escalate/review).
- Действие: нажать «Экспорт дела (PDF)» → скачивается `GET /api/report/{id}.pdf`. Открыть PDF.
- Переключиться на вкладку метрик `GET /api/metrics`: показать F1 по классам + confusion matrix.
- Сказать: «модель наша, обучена нами, без внешних API в скоринге».

### Блок 3 (1:45–2:15) — Граф связей
- Действие: открыть экран графа (тянет `GET /api/graph?min_risk=70`).
- Показать: кластер — несколько post-узлов, связанных общими entity-узлами
  (один бренд казино / один Telegram / один промокод) → координированная сеть.

### Блок 4 (2:15–2:45) — Live-разбор (ОПЦИОНАЛЬНО, если есть сеть)
- Действие: на экране «Live-проверка» вставить ссылку → `POST /api/analyze` с `{url}`.
  ИЛИ загрузить видео-файл (multipart) → тот же `POST /api/analyze`.
- Показать: реальный мультимодальный разбор → пост + extracted + score (вердикт).
- ЕСЛИ нет сети / модели не отвечают → ПРОПУСТИТЬ, перейти к блоку 5 (см. лестницу ниже).

### Блок 5 (2:45–3:00) — Тренды + этика + масштаб
- Действие: открыть экран трендов (`GET /api/trends`) — динамика по категориям/платформам, топ брендов.
- Сказать: human-in-the-loop, объяснимость, аудит-лог, путь MVP→прод.

## Лестница фоллбэков (что отрезаем при проблемах, сверху вниз)
| Условие | Действие на сцене |
|---|---|
| Нет сети / live-fetch не идёт | Пропустить Блок 4 (`POST /api/analyze` с URL). Демо целиком на кэше — все seed-посты уже имеют пред-вычисленные признаки, скоринг мгновенный. |
| Тяжёлые модели (Whisper/CLIP) не встали | Не запускать live-upload вообще. Кэш-датасет (Блоки 1–3,5) не зависит от тяжёлых моделей. |
| Граф тормозит/пуст | Пропустить Блок 3, усилить Блок 2 (drill-down + PDF + метрики — это ядро критерия №2). |
| Совсем нет сервера / падение | Открыть заранее снятые скриншоты консоли + готовый PDF-досье + `koz-pitch.md`. Рассказать по слайдам. |
| Кончается время | Минимальный зачётный путь: Блок 1 (лента) → Блок 2 (drill-down + метрики + PDF). Это закрывает критерии №2 и №5. |

## Гарантированный офлайн-режим (cache-only)
- Демо НЕ требует сети: seed-посты и их признаки закэшированы в `data/demo_posts.jsonl`.
- Модели предзагружены на этапе `setup.bat`. Любой эндпоинт из Блоков 1–3,5 отвечает локально.
- Единственный сетевой путь — Блок 4 (`POST /api/analyze` по URL); он всегда опционален.
```

- [ ] **Шаг 2: Проверить** — Run: `python -c "import pathlib,sys; t=pathlib.Path(r'docs/pitch/demo-script.md').read_text(encoding='utf-8'); req=['POST /api/tick','GET /api/post/','GET /api/report/','GET /api/metrics','GET /api/graph','POST /api/analyze','GET /api/trends','Лестница фоллбэков','cache-only']; missing=[k for k in req if k not in t]; print('OK' if not missing else 'MISSING: '+str(missing)); sys.exit(0 if not missing else 1)"` — Expected: печатает `OK`, exit code 0 (все эндпоинты контракта и блок фоллбэков на месте).
- [ ] **Шаг 3: Коммит** — `git add docs/pitch/demo-script.md` затем `git commit -m "docs(pitch): click-by-click 3-min demo run + fallback ladder (cache-only / skip live / skip heavy models)"`.

---

### Задача 3: Q&A-заготовки для жюри

**Файлы:**
- Create: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\docs\pitch\qa-prep.md`

- [ ] **Шаг 1: Создать `docs/pitch/qa-prep.md`** — ожидаемые вопросы жюри с чёткими ответами, особенно по «своей модели», ложным срабатываниям/этике и масштабу/стоимости. Содержимое:

```markdown
# Q&A-заготовки для защиты «КӨЗ»

## 1. «Модель действительно ваша / независимая от внешних сервисов?» (критерий №2)
Да. Скоринг полностью локальный, без внешних платных API. Whisper, EasyOCR, CLIP и
эмбеддинги — это локальные open-source ФИЧЕРИЗАТОРЫ, а не сервисы принятия решения.
Оцениваемая модель — это обученная нами голова-классификатор (sklearn LogisticRegression)
поверх фьюзнутых признаков: мультиязычные эмбеддинги (заморожены) + наши инженерные признаки.
Код обучения — `app/model/train.py`, артефакт — `app/model/artifacts/clf.joblib`,
метрики (F1 по классам, confusion matrix, n_train/n_test) — на эндпоинте `GET /api/metrics`.
LLM (Claude/GPT) в скоринге НЕ участвует.

## 2. «А эмбеддинги — это же чужая модель?»
Эмбеддинги заморожены и служат только векторизатором текста (как TF-IDF, только мультиязычный).
Решение о риске принимает обученная НАМИ голова на НАШИХ признаках и НАШЕМ датасете RU+KZ.
Это стандартная и честная практика transfer learning; «своя модель» = обученный нами классификатор.

## 3. «Как боретесь с ложными срабатываниями? Это же этический риск.» (критерий №3)
Несколько уровней: (а) в датасет специально добавлены «трудные» негативы — легальная реклама,
финликбез, новости о казино, — чтобы модель не штамповала ложные флаги; (б) порог калибруем
в пользу precision (REVIEW_THRESHOLD=40, ESCALATE_THRESHOLD=70 в `app/config.py`);
(в) human-in-the-loop: система приоритизирует для проверки, НЕ блокирует и НЕ обвиняет
автоматически — финальное решение за аналитиком/юристом; (г) каждый флаг объясним
(`app/decision/explain.py`), без «чёрного ящика»; (д) все решения пишутся в аудит-лог (таблица `audit`).

## 4. «Что с персональными данными / законностью сбора?» (критерий №3)
Только публичные данные. Минимизация PII: храним публичный хэндл и контент, не профилируем
личность. Материалы, противоречащие закону РК и этике, не используются. Привязка к мандату АФМ.

## 5. «Как это масштабируется и сколько стоит?» (критерий №4)
Архитектура модульная: stateless-воркеры экстракторов + очередь задач, батчинг инференса.
Горизонтальное масштабирование экстракторов и сервинга модели. Путь: MVP (SQLite, in-process,
один ноутбук) → прод (Postgres + очередь + объектное хранилище медиа). Узкое место — тяжёлый
мультимодальный разбор (Whisper/CLIP) на видео; решается батчингом и GPU/CPU-пулом, оценивается
в видео/час. Лёгкий путь (текст+OCR+скоринг) дёшев и масштабируется линейно.

## 6. «Это же просто симуляция, а не реальный мониторинг соцсетей?»
Поток постоянного мониторинга мы СИМУЛИРУЕМ seed-лентой (честно, по YAGNI на 24ч), но live-путь
настоящий: одиночная ссылка через yt-dlp и загрузка файла дают реальный мультимодальный разбор
(`POST /api/analyze`). Реальный непрерывный краулер под нагрузкой — это инженерия инфраструктуры,
а не AI, и относится к этапу прода.

## 7. «Покажите, что модель не переобучена / метрики честные.»
Стратифицированный train/test split, метрики считаются на отложенном test (`GET /api/metrics`:
per-class precision/recall/f1, confusion_matrix, n_train, n_test). Датасет ~400–600 размеченных
RU+KZ текстов на реальных публичных паттернах.

## 8. «Почему именно эти классы?»
`гемблинг` / `финпирамида (HYIP)` / `мошенничество (реф-схема)` / `чисто` — ровно типы
противоправного контента из положения трека плюс класс «чисто» с трудными негативами против
ложных срабатываний. Категории в коде: `gambling` / `pyramid` / `fraud` / `clean`.

## 9. «Что если модели не запустятся на вашем ноутбуке во время демо?»
Есть лестница фоллбэков (см. `demo-script.md`): демо на кэше работает всегда без сети и без
тяжёлых моделей; live-разбор опционален. Предзащитный smoke-тест `scripts/dry_run.py`
гоняем до выхода — он проверяет, что все ключевые эндпоинты отвечают 200.
```

- [ ] **Шаг 2: Проверить** — Run: `python -c "import pathlib,sys; t=pathlib.Path(r'docs/pitch/qa-prep.md').read_text(encoding='utf-8'); req=['действительно ваша','ложными срабатываниями','масштабируется','аудит-лог','REVIEW_THRESHOLD=40','ESCALATE_THRESHOLD=70','/api/metrics']; missing=[k for k in req if k not in t]; print('OK' if not missing else 'MISSING: '+str(missing)); sys.exit(0 if not missing else 1)"` — Expected: печатает `OK`, exit code 0 (ключевые вопросы и опорные факты присутствуют, пороги совпадают с контрактом).
- [ ] **Шаг 3: Коммит** — `git add docs/pitch/qa-prep.md` затем `git commit -m "docs(pitch): jury Q&A prep (own-model independence, false positives/ethics, scalability/cost)"`.

---

### Задача 4: TDD — модуль проверки эндпоинтов `check_endpoints` для dry_run

**Файлы:**
- Create: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\scripts\dry_run.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_dry_run.py`

Сначала тестируем чистую функцию проверки списка эндпоинтов против FastAPI-приложения через `TestClient` (без реального сетевого сервера) — это тестируемо без зависимости от живого uvicorn. Полный CLI-прогон против live-сервера добавим в Задаче 5.

- [ ] **Шаг 1: Failing-тест** — создать `tests/test_dry_run.py`:

```python
from fastapi import FastAPI
from scripts.dry_run import REQUIRED_ENDPOINTS, check_endpoints


def _make_stub_app() -> FastAPI:
    app = FastAPI()

    @app.get("/")
    def root():
        return "ok"

    @app.post("/api/tick")
    def tick():
        return {"revealed": 1}

    @app.get("/api/feed")
    def feed():
        return [{"post": {"id": "p1"}, "score": {"risk": 90}}]

    @app.get("/api/post/{id}")
    def post(id: str):
        return {"post": {"id": id}}

    @app.get("/api/graph")
    def graph():
        return {"nodes": [], "edges": []}

    @app.get("/api/trends")
    def trends():
        return {"by_category": {}}

    @app.get("/api/metrics")
    def metrics():
        return {"n_train": 1, "n_test": 1}

    return app


def test_required_endpoints_listed():
    paths = [m for (m, _p) in REQUIRED_ENDPOINTS]
    assert ("POST", "/api/tick") in REQUIRED_ENDPOINTS
    assert ("GET", "/api/feed") in REQUIRED_ENDPOINTS
    assert ("GET", "/api/metrics") in REQUIRED_ENDPOINTS
    assert "GET" in paths and "POST" in paths


def test_check_endpoints_all_pass():
    from fastapi.testclient import TestClient

    client = TestClient(_make_stub_app())
    results = check_endpoints(client)
    assert results, "expected non-empty results"
    assert all(status == 200 for (_label, status) in results)


def test_check_endpoints_detects_failure():
    from fastapi.testclient import TestClient

    app = FastAPI()  # has no routes -> everything 404

    @app.get("/")
    def root():
        return "ok"

    client = TestClient(app)
    results = check_endpoints(client)
    statuses = {label: status for (label, status) in results}
    assert statuses["GET /api/feed"] == 404
```

- [ ] **Шаг 2: Запустить (ожидаем FAIL)** — Run: `pytest tests/test_dry_run.py -v` — Expected: FAIL с `ModuleNotFoundError: No module named 'scripts.dry_run'` (либо `ImportError: cannot import name 'check_endpoints'`).
- [ ] **Шаг 3: Минимальная реализация** — создать `scripts/dry_run.py`:

```python
"""Предзащитный smoke-тест «КӨЗ»: проверяет, что ключевые эндпоинты отвечают 200.

check_endpoints(client) принимает любой объект с методами .get()/.post()
(httpx.Client или fastapi.testclient.TestClient) и возвращает [(label, status_code)].
"""
from __future__ import annotations

# (method, path) — path с {id} подставляется первым post_id из /api/feed.
REQUIRED_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/"),
    ("POST", "/api/tick"),
    ("GET", "/api/feed"),
    ("GET", "/api/post/{id}"),
    ("GET", "/api/graph"),
    ("GET", "/api/trends"),
    ("GET", "/api/metrics"),
]


def _first_post_id(client) -> str | None:
    try:
        resp = client.get("/api/feed?limit=1")
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data:
            return data[0]["post"]["id"]
    except Exception:
        return None
    return None


def check_endpoints(client) -> list[tuple[str, int]]:
    """Дёргает каждый обязательный эндпоинт, возвращает [(label, status_code)]."""
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
```

- [ ] **Шаг 4: Запустить (ожидаем PASS)** — Run: `pytest tests/test_dry_run.py -v` — Expected: `3 passed`.
- [ ] **Шаг 5: Коммит** — `git add scripts/dry_run.py tests/test_dry_run.py` затем `git commit -m "feat(dry-run): check_endpoints smoke-test helper + REQUIRED_ENDPOINTS registry"`.

---

### Задача 5: TDD — CLI dry_run против реального app (TestClient + main()) и инструкция по live-прогону

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\scripts\dry_run.py`
- Test: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\tests\test_dry_run.py`

Теперь `main()` поднимает РЕАЛЬНОЕ `app.main:app` через `TestClient`, тикает ленту (`POST /api/tick`) перед проверками (чтобы появились раскрытые посты), прогоняет `check_endpoints`, печатает таблицу и `DRY-RUN OK`/`DRY-RUN FAILED`, возвращает 0/1. Это позволяет гонять smoke-тест без отдельного uvicorn-процесса. Зависит от F1+данных: import `app.main` должен резолвиться.

- [ ] **Шаг 1: Failing-тест** — дописать в `tests/test_dry_run.py`:

```python
def test_main_returns_zero_against_stub(monkeypatch):
    import scripts.dry_run as dr

    stub = _make_stub_app()

    # main() должен уметь брать клиент через инъекцию, не дёргая реальный app.main
    from fastapi.testclient import TestClient

    rc = dr.main(client=TestClient(stub))
    assert rc == 0


def test_main_returns_one_on_failure():
    import scripts.dry_run as dr
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/")
    def root():
        return "ok"

    rc = dr.main(client=TestClient(app))
    assert rc == 1
```

- [ ] **Шаг 2: Запустить (ожидаем FAIL)** — Run: `pytest tests/test_dry_run.py::test_main_returns_zero_against_stub -v` — Expected: FAIL с `AttributeError: module 'scripts.dry_run' has no attribute 'main'`.
- [ ] **Шаг 3: Реализация** — добавить в конец `scripts/dry_run.py`:

```python
def _build_client():
    """Поднимает реальное FastAPI-приложение «КӨЗ» через TestClient."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def main(client=None) -> int:
    """Бутит app, тикает ленту, проверяет эндпоинты, печатает отчёт, возвращает код возврата."""
    own_client = False
    if client is None:
        client = _build_client()
        own_client = True

    # Тик ленты — раскрываем seed-посты, чтобы /api/feed и /api/post/{id} были непустыми.
    try:
        client.post("/api/tick")
        client.post("/api/tick")
    except Exception:
        pass

    results = check_endpoints(client)

    print("=== КӨЗ dry-run smoke-test ===")
    ok = True
    for label, status in results:
        mark = "OK " if status == 200 else "FAIL"
        if status != 200:
            ok = False
        print(f"[{mark}] {label} -> {status}")

    if own_client and hasattr(client, "close"):
        client.close()

    if ok:
        print("DRY-RUN OK")
        return 0
    print("DRY-RUN FAILED")
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
```

- [ ] **Шаг 4: Запустить (ожидаем PASS)** — Run: `pytest tests/test_dry_run.py -v` — Expected: `5 passed`.
- [ ] **Шаг 5: Коммит** — `git add scripts/dry_run.py tests/test_dry_run.py` затем `git commit -m "feat(dry-run): main() boots app via TestClient, ticks feed, asserts 200s, exits 0/1"`.

---

### Задача 6: Финальный интеграционный прогон dry_run против live-сборки + чек-лист в README

**Файлы:**
- Modify: `C:\Users\adlet\OneDrive\Документы\AFMHACKATHON\README.md`

Документируем точные команды предзащитного smoke-теста с ожидаемыми 200-ми. Эта задача выполняется ПОСЛЕ финальной интеграции (F1–F7 готовы), когда `import app.main` и данные на месте.

- [ ] **Шаг 1: Реальный прогон** — Run: `python scripts/dry_run.py` — Expected: печатает таблицу, где каждая строка `[OK ] <METHOD> <path> -> 200` для `/`, `POST /api/tick`, `GET /api/feed`, `GET /api/post/{id}`, `GET /api/graph`, `GET /api/trends`, `GET /api/metrics`, и последней строкой `DRY-RUN OK`, exit code 0. Если какой-то эндпоинт != 200 — чинить соответствующую фичу (это и есть назначение smoke-теста), затем перезапустить.
- [ ] **Шаг 2: Прогон против живого сервера (контроль перед сценой)** — в одном терминале: `run.bat` (поднимает uvicorn на `http://127.0.0.1:8000`). В другом проверить ключевые эндпоинты curl-ом: Run: `curl -s -o NUL -w "tick:%{http_code} feed:" -X POST http://127.0.0.1:8000/api/tick && curl -s -o NUL -w "%{http_code} metrics:" "http://127.0.0.1:8000/api/feed?limit=1" && curl -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8000/api/metrics` — Expected: `tick:200 feed:200 metrics:200`.
- [ ] **Шаг 2b (fallback без curl, PowerShell)** — Run: `powershell -Command "@('http://127.0.0.1:8000/','http://127.0.0.1:8000/api/feed?limit=1','http://127.0.0.1:8000/api/trends','http://127.0.0.1:8000/api/metrics') | ForEach-Object { try { $r=Invoke-WebRequest -UseBasicParsing $_; Write-Host ('{0} -> {1}' -f $_, $r.StatusCode) } catch { Write-Host ('{0} -> {1}' -f $_, $_.Exception.Response.StatusCode.value__) } }"` — Expected: каждая строка оканчивается `-> 200`.
- [ ] **Шаг 3: Дописать чек-лист в `README.md`** — добавить раздел с точными командами и ожидаемыми кодами:

```markdown
## Предзащитный smoke-тест (dry-run)

Перед демо обязательно прогнать (поднимает app через TestClient, тикает ленту, проверяет эндпоинты):

```
python scripts/dry_run.py
```

Ожидаемый вывод — все строки `[OK ] ... -> 200` и финальная `DRY-RUN OK` (exit code 0):

```
[OK ] GET / -> 200
[OK ] POST /api/tick -> 200
[OK ] GET /api/feed -> 200
[OK ] GET /api/post/{id} -> 200
[OK ] GET /api/graph -> 200
[OK ] GET /api/trends -> 200
[OK ] GET /api/metrics -> 200
DRY-RUN OK
```

Если хоть один эндпоинт вернул не 200 — печатается `DRY-RUN FAILED` (exit code 1); чинить соответствующую фичу и перезапускать.

Проверка против живого сервера (опционально, как на сцене): `run.bat`, затем открыть
`http://127.0.0.1:8000/`. Лестница фоллбэков и порядок кликов — в `docs/pitch/demo-script.md`.
```

- [ ] **Шаг 4: Проверить документацию** — Run: `python -c "import pathlib,sys; t=pathlib.Path(r'README.md').read_text(encoding='utf-8'); req=['python scripts/dry_run.py','DRY-RUN OK','GET /api/metrics -> 200','POST /api/tick -> 200']; missing=[k for k in req if k not in t]; print('OK' if not missing else 'MISSING: '+str(missing)); sys.exit(0 if not missing else 1)"` — Expected: печатает `OK`, exit code 0.
- [ ] **Шаг 5: Коммит** — `git add README.md` затем `git commit -m "docs(dry-run): document pre-defense smoke-test commands + expected 200s checklist"`.