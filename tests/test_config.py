from app import config


def test_thresholds_exact_values():
    assert config.REVIEW_THRESHOLD == 40
    assert config.ESCALATE_THRESHOLD == 70


def test_categories():
    assert config.CATEGORIES == ["gambling", "pyramid", "fraud", "clean"]


def test_no_embedding_model_constant():
    # Поправка A1: собственная модель torch-free (sklearn), EMBEDDING_MODEL удалён.
    assert not hasattr(config, "EMBEDDING_MODEL")


def test_paths_are_under_base_dir():
    assert config.DB_PATH.parent == config.DATA_DIR
    assert config.WEB_DIR.name == "web"
    assert config.CLF_PATH.name == "clf.joblib"
    assert config.METRICS_PATH.name == "metrics.json"
    assert config.MEDIA_DIR == config.DATA_DIR / "media"
    assert config.CACHE_DIR == config.DATA_DIR / "feature_cache"
    assert config.DEMO_POSTS_PATH == config.DATA_DIR / "demo_posts.jsonl"
    assert config.DATASET_PATH == config.DATA_DIR / "dataset.jsonl"


def test_recommended_action_mapping():
    # Поправка A4 / §0.11: 3-веточный маппинг, без полосы "monitor".
    assert config.action_for_risk(10) == "auto_clear"
    assert config.action_for_risk(20) == "auto_clear"
    assert config.action_for_risk(39) == "auto_clear"
    assert config.action_for_risk(40) == "review"
    assert config.action_for_risk(55) == "review"
    assert config.action_for_risk(69) == "review"
    assert config.action_for_risk(70) == "escalate"
    assert config.action_for_risk(95) == "escalate"
