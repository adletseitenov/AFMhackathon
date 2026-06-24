# Active-learning hook: train._load_analyst_labels reads data/analyst_labels.jsonl
# (one {"text","label"} per line), appends valid rows, skips malformed lines, and
# returns [] when the file is absent. Contract with the verdict agent.
import json

from app.config import CATEGORIES
from app.model import train as train_mod


def test_absent_file_returns_empty(tmp_path):
    missing = tmp_path / "nope.jsonl"
    assert train_mod._load_analyst_labels(missing) == []


def test_reads_valid_labels(tmp_path):
    p = tmp_path / "analyst_labels.jsonl"
    lines = [
        {"text": "переведи на кошелёк и удвой", "label": "fraud"},
        {"text": "гарантированный доход 50% в месяц", "label": "pyramid"},
        {"text": "сегодня хорошая погода", "label": "clean"},
    ]
    p.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in lines), encoding="utf-8")
    rows = train_mod._load_analyst_labels(p)
    assert len(rows) == 3
    for r in rows:
        assert set(r.keys()) >= {"text", "label"}
        assert r["label"] in CATEGORIES


def test_skips_malformed_lines(tmp_path):
    p = tmp_path / "analyst_labels.jsonl"
    content = "\n".join([
        json.dumps({"text": "ставки на 1xbet", "label": "gambling"}, ensure_ascii=False),
        "{ this is not valid json",                       # broken JSON
        json.dumps({"text": "no label here"}),            # missing label
        json.dumps({"label": "fraud"}),                   # missing text
        json.dumps({"text": "", "label": "clean"}),       # empty text
        json.dumps({"text": "bad label", "label": "spam"}),  # label not in CATEGORIES
        json.dumps(["not", "a", "dict"]),                 # not an object
        "",                                                # blank line
        json.dumps({"text": "финиш ок", "label": "clean"}, ensure_ascii=False),
    ])
    p.write_text(content, encoding="utf-8")
    rows = train_mod._load_analyst_labels(p)
    # only the two well-formed rows survive
    assert len(rows) == 2
    labels = {r["label"] for r in rows}
    assert labels == {"gambling", "clean"}
