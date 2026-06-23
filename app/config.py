"""Канонический конфиг КӨЗ — пути, пороги, имена моделей экстракторов.

Источник истины для всех фич (F1–F9 ПОТРЕБЛЯЮТ эти константы, не переопределяют).

ВАЖНО (поправка A1): собственная модель = ТОЛЬКО scikit-learn (TF-IDF FeatureUnion
+ handcrafted-сигналы + LogisticRegression). НИКАКОГО EMBEDDING_MODEL / torch.
"""

from pathlib import Path

# --- Базовые директории ---
BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
ARTIFACTS_DIR = APP_DIR / "model" / "artifacts"
MEDIA_DIR = DATA_DIR / "media"

# --- Пути файлов ---
DB_PATH = DATA_DIR / "koz.db"
CLF_PATH = ARTIFACTS_DIR / "clf.joblib"
METRICS_PATH = ARTIFACTS_DIR / "metrics.json"
CACHE_DIR = DATA_DIR / "feature_cache"

DEMO_POSTS_PATH = DATA_DIR / "demo_posts.jsonl"
DATASET_PATH = DATA_DIR / "dataset.jsonl"
SEEDS_PATH = DATA_DIR / "seeds.json"

# --- Имена моделей экстракторов (опциональные тяжёлые библиотеки, lazy-import) ---
WHISPER_MODEL = "small"
OCR_LANGS = ["ru", "en"]
CLIP_MODEL = "ViT-B-32"
CLIP_PRETRAINED = "laion2b_s34b_b79k"

# --- Пороги риска ---
REVIEW_THRESHOLD = 40
ESCALATE_THRESHOLD = 70

# --- Категории классификатора ---
CATEGORIES = ["gambling", "pyramid", "fraud", "clean"]

# --- Тикер фоновой ингестии (drip-reveal seed-постов) ---
TICK_REVEAL_N = 3
TICK_INTERVAL_SEC = 5


def action_for_risk(risk: int) -> str:
    """Единый 3-уровневый маппинг риска в рекомендацию (§0.11, поправка A4).

    risk < 40            -> "auto_clear"
    40 <= risk < 70      -> "review"
    risk >= 70           -> "escalate"

    Полосы "monitor" порогами НЕ производится (YAGNI).
    """
    if risk >= ESCALATE_THRESHOLD:
        return "escalate"
    if risk >= REVIEW_THRESHOLD:
        return "review"
    return "auto_clear"
