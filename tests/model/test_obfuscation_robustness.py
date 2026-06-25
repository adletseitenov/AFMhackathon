"""Регрессия: устойчивость детектинга к ОБФУСКАЦИИ текста (приём мошенников).

normalize_obfuscated снимает разрядку «к а з и н о», гомоглифы «1хбет»/«дoхoд»,
leetspeak «к4з1н0», soft-hyphen/zero-width, точки между буквами. Эти обфусцированные
угрозы должны оставаться НЕ-clean (risk >= REVIEW) — иначе де-обфускация
регрессировала. Защищает ночные переобучения модели от потери этой устойчивости.
"""

from app import config
from app.models import Extracted
from app.model.classifier import RiskClassifier

# Обфусцированные угрозы (gambling/pyramid/fraud/crypto, RU + KZ).
_OBFUSCATED_THREATS = [
    "К а з и н о, гарантированный д о х о д 300% в месяц, заноси",       # разрядка
    "1хбет промокод, з а н о с и и забери вдвое сегодня",                # cyr-x гомоглиф
    "Гaрaнтирoвaнный дoхoд 50% в мeсяц, врклaд бeз рискa",               # latin a/o/e
    "кру­ти сл0ты, з4н0с на к4з1н0, выв0д быстр0",                  # leet + soft hyphen
    "Пере­веди­те на безопас­ный счёт, сооб­щите код из смс",  # soft hyphen
    "Б.и.н.а.р.н.а.я м.а.т.р.и.ц.а, реинвест удваивает капитал",         # точки
    "и н в е с т и ц и я, айына 30% кепілдік табыс, досыңды шақыр",      # KZ разрядка
    "Уд­во­им ваш би­т­ко­ин, кинь на кошелёк, x2 за час",  # soft hyphen крипто
    "В ы   в ы и г р а л и приз! Оплатите доставку по ссылке",           # разрядка приз
    "Ш у ғ ы л! Картаңыз бұғатталды, смс кодын айтыңыз",                 # KZ фишинг разрядка
    "к​а​з​и​н​о занос гарантирован вдвое",     # zero-width
]


def _predict(clf, text):
    ex = Extracted(post_id="o", caption=text, transcript="", ocr_text="",
                   visual_concepts=[], combined_text=text, entities=[])
    return clf.predict(ex)


def test_obfuscated_threats_still_detected():
    clf = RiskClassifier.load()
    misses = []
    for text in _OBFUSCATED_THREATS:
        sc = _predict(clf, text)
        if sc.category == "clean" or sc.risk < config.REVIEW_THRESHOLD:
            misses.append((text[:40], sc.risk, sc.category))
    assert not misses, f"обфускация снизила детектинг (де-обфускация регрессировала): {misses}"
