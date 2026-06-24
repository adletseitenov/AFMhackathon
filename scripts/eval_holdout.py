"""Оценка модели на ФИКСИРОВАННОМ реалистичном held-out наборе (data/eval_holdout.jsonl).

Этот набор НЕ участвует в обучении (gen_dataset.py его не читает), поэтому точность
на нём измеряет РЕАЛЬНУЮ обобщающую способность детектинга финпирамид/мошенничества/
криптосхем — в отличие от synthetic-split macro_f1, который раздувается из-за
запоминания шаблонных фраз. Это «честный термометр» ночных улучшений модели.

Запуск: python -m scripts.eval_holdout
Печатает per-category accuracy, threat-recall (доля угроз, не отнесённых к clean),
clean-specificity, список ошибок, и строку SUMMARY_JSON {...} для машинного чтения.
"""

import json
import sys
from pathlib import Path

try:  # Windows-консоль (cp1252) не печатает кириллицу/«→» — форсируем UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.models import Extracted
from app.model.classifier import RiskClassifier

EVAL_PATH = Path(__file__).resolve().parent.parent / "data" / "eval_holdout.jsonl"
CATS = ["gambling", "pyramid", "fraud", "clean"]


def _ex(text: str) -> Extracted:
    return Extracted(post_id="eval", caption=text, transcript="", ocr_text="",
                     visual_concepts=[], combined_text=text, entities=[])


def evaluate() -> dict:
    clf = RiskClassifier.load()
    rows = [json.loads(ln) for ln in EVAL_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]

    per = {c: {"total": 0, "correct": 0} for c in CATS}
    misses = []
    risky_total = risky_caught = 0
    clean_total = clean_kept = 0

    for r in rows:
        gold, text = r["label"], r["text"]
        sc = clf.predict(_ex(text))
        pred = sc.category
        per.setdefault(gold, {"total": 0, "correct": 0})
        per[gold]["total"] += 1
        if pred == gold:
            per[gold]["correct"] += 1
        if gold != "clean":
            risky_total += 1
            if pred != "clean":
                risky_caught += 1
            if pred != gold:
                misses.append((gold, pred, sc.risk, text[:72]))
        else:
            clean_total += 1
            if pred == "clean":
                clean_kept += 1
            else:
                misses.append((gold, pred, sc.risk, text[:72]))

    overall_correct = sum(per[c]["correct"] for c in CATS)
    overall_total = sum(per[c]["total"] for c in CATS)

    print("=== per-category exact-match accuracy (eval_holdout) ===")
    for c in CATS:
        t, k = per[c]["total"], per[c]["correct"]
        if t:
            print(f"  {c:9s}: {k}/{t}  ({round(100*k/t)}%)")
    print(f"  OVERALL : {overall_correct}/{overall_total} ({round(100*overall_correct/max(overall_total,1))}%)")
    print("=== detection ===")
    print(f"  threat-recall (угроза -> не clean): {risky_caught}/{risky_total} ({round(100*risky_caught/max(risky_total,1))}%)")
    print(f"  clean-specificity (clean -> clean): {clean_kept}/{clean_total} ({round(100*clean_kept/max(clean_total,1))}%)")
    print(f"=== misclassified ({len(misses)}) ===")
    for g, p, risk, t in misses:
        print(f"  [{g}->{p} r{risk}] {t}")

    summary = {
        "per_category": {c: per[c] for c in CATS},
        "overall_acc": round(overall_correct / max(overall_total, 1), 4),
        "threat_recall": round(risky_caught / max(risky_total, 1), 4),
        "clean_specificity": round(clean_kept / max(clean_total, 1), 4),
        "n_miss": len(misses),
        "n_total": overall_total,
    }
    print("SUMMARY_JSON " + json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    evaluate()
