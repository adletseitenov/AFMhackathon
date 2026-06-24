# AGENTS.md — guide for AI agents working on КӨЗ

> Read this first. It is the onboarding contract for any AI coding agent
> (Claude Code / Cursor / Codex) touching this repo. Past-session memory and
> hard-won lessons live in [`docs/agent-memory.md`](docs/agent-memory.md) —
> read that too before non-trivial work.

## What this is

**КӨЗ** (KÓZ — Kazakh for "eye") is an **AI analyst console for Kazakhstan's
Agency of Financial Monitoring (АФМ)**, built for the **АФМ AI Hackathon 2026**,
track **"AI Media Watch"**. It detects **illegal online gambling and financial
pyramids** in social-media video (TikTok / Instagram / YouTube / Telegram),
content in **Russian + Kazakh**.

The pipeline: **autonomously find real posts → multimodal extraction → score with
our own model → priority queue → human analyst decides.** The system prioritizes
for review; it never auto-blocks or auto-accuses (human-in-the-loop).

Defense: **June 25 2026** (3-min pitch + 2-min Q&A). Pitch materials in `docs/pitch/`.

## Golden rules (do not break these)

1. **Scoring = our own sklearn model only (judging criterion #2).** The risk
   classifier is **TORCH-FREE**: TF-IDF (word + char_wb) + handcrafted signals →
   LogisticRegression. **Never** add an LLM or external API to the *scoring* path.
   Whisper / EasyOCR / open-CLIP are **feature extractors** (give text/visual in),
   not deciders.
2. **Don't break the offline demo.** Frontend libs are vendored to `web/vendor/`
   (works with no network). The cache-only path (seed posts + cached features) must
   keep working without heavy models or internet.
3. **Frontend is no-build.** `web/app.js` MUST load *before* `web/vendor/alpine.min.js`
   (both `defer`, document order) and MUST end with `window.kozApp = kozApp`. The
   pastel risk palette is **locked** (escalate `#FDEBEC/#9F2F2D`, review
   `#FBF3DB/#956400`, clean `#EDF3EC/#346538`; canvas `#FBFBFA`, ink `#111111`).
4. **Never edit `app/main.py` to add a feature.** Routes are auto-discovered: drop a
   `router = APIRouter()` into any `app/**/routes.py` and it's mounted automatically
   (`pkgutil.walk_packages`). `main.py` is core only.
5. **All user-facing strings are in Russian.**
6. **Verify the git repo root before `git add`.** There is a stray git repo at
   `C:\Users\adlet\.git`; always confirm you're inside this project
   (`git rev-parse --show-toplevel`).

## Quick start

```bash
run.bat                         # venv + uvicorn on 127.0.0.1:8000
                                # sets KOZ_AUTO_DISCOVER=1 and KOZ_DEEP_DISCOVER=1
# or, manually:
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Verification (run all before committing):

```bash
python -m pytest tests -q       # full suite (currently 266 pass)
python -m scripts.dry_run       # endpoint smoke test -> "DRY-RUN OK"
node --check web/app.js         # frontend syntax
python -m app.model.train       # retrain -> app/model/artifacts/{clf.joblib,metrics.json}
```

The ASGI app is **`app.main:app`** (not `application`).

## Architecture

```
DISCOVERY / MONITORING  →  EXTRACTION  →  OWN MODEL  →  QUEUE  →  ANALYST
 (finds real posts)        (multimodal)    (scoring)    (by risk)  (decides)
```

- **FastAPI + uvicorn**, single `app` in `app/main.py`. The lifespan opens **one**
  sqlite connection at `app.state.db`, optionally seeds demo posts, and starts three
  background tasks: the ingestion ticker, the watchlist scan loop, and the discovery
  loop (the last two gated by env flags).
- **SQLite** (stdlib, connection-first API in `app/db.py`). DB at `data/koz.db`
  (runtime-generated, gitignored).
- **Background jobs** (`app/jobs/`): a ThreadPoolExecutor queue. `run_job(kind, fn)`
  returns a `job_id`; `fn(report)` calls `report(stage, progress)`; the UI polls
  `GET /api/jobs/{id}` and shows a progress bar. Long endpoints (`/api/analyze`,
  `/api/discover`) return `{job_id}` immediately.
- **Frontend**: single-page Alpine.js app in `web/` (`index.html` + `app.js` +
  `styles.css`), Tailwind/Alpine/Chart.js/vis-network vendored to `web/vendor/`.

## File map

| Path | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, lifespan, background loops, auto-router, static mounts. **Core — avoid editing for features.** |
| `app/config.py` | Canonical constants: paths, thresholds (`REVIEW_THRESHOLD=40`, `ESCALATE_THRESHOLD=70`), `CATEGORIES`, extractor model names. |
| `app/db.py` | SQLite helpers (connection-first). |
| `app/models.py` | Dataclasses: `Post`, `Extracted`, `Score`, `FeatureHit`, `Entity`. |
| `app/model/features.py` | `HANDCRAFTED_SIGNALS` registry + `build_features` + `_texts_to_signal_dicts`. **Signals live here.** |
| `app/model/classifier.py` | `RiskClassifier.load()/predict()` → `Score` with explainable `top_features`. |
| `app/model/train.py` | Builds + trains the sklearn Pipeline; writes `clf.joblib` + `metrics.json`. |
| `app/decision/scoring.py` | `score_post()` — runs the model, persists score + recommendation + audit. |
| `app/decision/explain.py` | `_FEATURE_LABELS` (RU) + `explain()` — human-readable "why dangerous". |
| `app/extractors/` | `pipeline.extract` orchestrates `audio` (Whisper), `ocr` (EasyOCR), `visual` (CLIP), `text` (entities). Lazy-import heavy libs. |
| `app/ingestion/fetch.py` | `fetch_link`/`fetch_post` via yt-dlp; SSRF guard; ffmpeg + JS-runtime wiring. |
| `app/ingestion/scan.py` | Real Telegram channel scan. |
| `app/discovery/` | Autonomous search: `youtube.py` (ytsearch), `web.py` (DDG Telegram/video links), `discover.py` (orchestrator, platform-scope + deep), `routes.py` (`POST /api/discover`). |
| `app/watchlist/` | Channels under monitoring + periodic scan + per-channel stats. |
| `app/graph/`, `app/analytics/`, `app/report/` | Connection graph, trends, PDF case export. |
| `web/` | The console (no-build Alpine SPA). |
| `scripts/gen_dataset.py` | Synthetic RU+KZ training dataset generator. |
| `scripts/dry_run.py` | Endpoint smoke test. |
| `docs/pitch/` | Defense materials (demo-script, qa-prep, koz-pitch, deck.html). |

## The model (criterion #2) — one paragraph

`combined_text` (caption + Whisper transcript + OCR text) feeds a sklearn
`FeatureUnion`: word TF-IDF `(1,2)` + char_wb TF-IDF `(3,5)` + a DictVectorizer over
**14 handcrafted regex signals** (incl. 5 Kazakhstan-specific: Kaspi/Halyk transfers,
tenge amounts, +7 7XX phones, local brands 1хбет/мелбет/Финико, Kazakh gambling
lexicon). Head: `LogisticRegression(class_weight='balanced', C=4.0)` over 4 classes
`gambling/pyramid/fraud/clean`. `risk = round(100 * (1 - P(clean)))`; action thresholds
40 (review) / 70 (escalate). `top_features` are the active signals weighted by the
LogReg coefficient for the predicted class → explainable, with Russian evidence
strings. Held-out **macro-F1 ≈ 0.95**, non-degenerate confusion matrix. Trains locally
in ~3s; artifact committed so it runs on clone. Full detail: `app/model/`.

## Conventions

- **Auto-router**: feature modules expose `router` in `app/**/routes.py`; never touch
  `main.py`.
- **Module-attribute seams for tests**: call cross-module functions via the imported
  module (e.g. `import app.ingestion.fetch as fetch_mod; fetch_mod.fetch_link(...)`)
  so tests can monkeypatch without network/heavy libs.
- **`real_only` feed filter**: `GET /api/feed?real_only=1` keeps only
  `source IN ('live','discovered')` (hides synthetic seed). The UI defaults to it.
- **SSRF guard** (`app/ingestion/fetch.py`): platform host allowlist + private/loopback
  /IPv4-mapped-IPv6 rejection, fail-closed on DNS failure. Keep it for any outbound fetch.

### Environment variables

| Var | Effect |
|---|---|
| `KOZ_AUTO_DISCOVER=1` | Enables the periodic background discovery loop (off in tests/offline). |
| `KOZ_DEEP_DISCOVER=1` | Background discovery also deep-analyzes the top-1 fresh post (real Whisper/OCR/CLIP). |
| `KOZ_FFMPEG_DIR` | Override the dir containing ffmpeg+ffprobe (yt-dlp needs both). |
| `KOZ_NODE_BIN` | Override the Node binary used as yt-dlp's JS runtime. |

## Gotchas — hard-won, don't rediscover

- **cv2 + Cyrillic paths**: `cv2.imread/imwrite` silently fail on non-ASCII paths
  (repo lives under `…\Документы\…`). Use `cv2.imencode/imdecode` with
  `np.fromfile/tofile`; pass numpy arrays to EasyOCR, not paths.
- **joblib pickle location**: `_texts_to_signal_dicts` MUST stay in
  `app/model/features.py` (not in the `train.py` entrypoint), else joblib stores a
  `__main__.*` reference and the artifact won't unpickle under uvicorn/pytest.
- **yt-dlp + YouTube (2026)**: needs a **JS runtime (Node)** *and* the **`yt-dlp-ejs`**
  challenge solver, else many videos return "This video is not available".
  `fetch._js_runtimes()` wires Node; `yt-dlp-ejs` is in `requirements-extractors.txt`.
- **Windows cp1252 stdout**: a Cyrillic `print()` crashes with `'charmap' codec`.
  `main.py` reconfigures `sys.stdout/stderr` to UTF-8 at import — keep that.
- **Static mount order**: `/data/media` (downloaded clips) is mounted **before** the
  catch-all `/` so the drill-down `<video>` doesn't 404.
- **Heavy extractors are optional**: torch/whisper/easyocr/open-clip/cv2 are
  lazy-imported; without them the text path still works (transcript/OCR/visual are
  empty). Heavy deps: `requirements-extractors.txt` (torch CPU index).
- **TikTok**: needs `curl_cffi` (impersonation) and rate-limits downloads per IP;
  file upload is the reliable analysis path.

## API surface (key endpoints)

`GET /health` · `GET /api/metrics` · `GET /api/feed?min_risk=&category=&platform=&real_only=&limit=`
· `GET /api/post/{id}` · `POST /api/analyze` (→ job) · `POST /api/discover` (→ job)
· `GET /api/jobs/{id}` · `GET /api/graph` · `GET /api/trends` · `GET /api/report/{id}.pdf`
· `GET /api/watchlist` · `GET /api/watchlist/stats` · `POST /api/scan/telegram`.

## How to extend safely

- **New endpoint/feature** → new `app/<feature>/routes.py` with a `router`. Add tests
  under `tests/<feature>/`.
- **New detection signal** → add to `HANDCRAFTED_SIGNALS` in `features.py` (additive —
  don't rename existing keys, the committed model depends on them), add a RU label in
  `explain.py`, add positive + hard-negative rows in `gen_dataset.py`, then
  `python -m app.model.train` and confirm macro-F1 stays ≥ 0.88 with a non-degenerate
  confusion matrix.
- **Always** finish with `pytest` + `dry_run` green and (for UI) `node --check` + 0
  console errors.
