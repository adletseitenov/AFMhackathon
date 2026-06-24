"""D1 — генератор демо-датасета КӨЗ: `data/demo_posts.jsonl` с кэш-признаками.

`build_demo_records()` отдаёт >=14 записей вида {"post": {...}, "extracted": {...}}
по платформам tiktok/instagram/youtube с реалистичными RU/KZ кэпшнами:
реклама казино/букмекеров (1xBet/Mostbet/Vavada), финансовые пирамиды
(«доход 30% в месяц»), реферальные/мошеннические схемы, плюс «чистые» примеры
(легальная реклама, фин-грамотность, новости, бытовой контент).

У каждой записи — полностью заполненный `extracted` (caption/transcript/ocr_text/
visual_concepts/combined_text/entities). `combined_text` склеивается канонически
(§0.10), `entities` заполняются единственной реализацией `extract_entities` (§0.5).
demo-посты несут предвычисленные признаки → seed-скоринг мгновенный (§0.8),
никаких тяжёлых моделей при старте.

`main()` пишет JSONL (по строке на запись, ensure_ascii=False) в config.DEMO_POSTS_PATH.

ВАЖНО: print в main() ASCII-санитизируется — путь репозитория содержит кириллицу
(«Документы»), а консоль Windows по умолчанию cp1252.
"""

import json
import sys

from app import config
from app.extractors.text import extract_entities


def _combined_text(caption: str, transcript: str, ocr_text: str,
                   visual_labels: list[str]) -> str:
    """Каноническая склейка §0.10: caption, transcript, ocr_text, метки визуала."""
    labels = " ".join(visual_labels)
    parts = [caption, transcript, ocr_text, labels]
    return "\n".join(p for p in parts if p)


def _record(post_id: str, platform: str, author_handle: str, url: str,
            caption: str, posted_at: str, *, transcript: str = "",
            ocr_text: str = "", visual_concepts: "list[dict] | None" = None,
            media_path: "str | None" = None,
            thumb_url: "str | None" = None,
            source: str = "seed") -> dict:
    """Собирает одну запись {"post", "extracted"} с предвычисленными признаками.

    `source` по умолчанию "seed" (исходная форма для tiktok/instagram/youtube);
    telegram-посты передают source="telegram" (§F8).
    """
    visual_concepts = visual_concepts or []
    visual_labels = [vc["label"] for vc in visual_concepts]
    combined = _combined_text(caption, transcript, ocr_text, visual_labels)
    entities = [
        {"type": e.type, "value": e.value, "normalized": e.normalized}
        for e in extract_entities(combined)
    ]
    return {
        "post": {
            "id": post_id,
            "platform": platform,
            "author_handle": author_handle,
            "url": url,
            "caption": caption,
            "posted_at": posted_at,
            "media_path": media_path,
            "thumb_url": thumb_url,
            "source": source,
        },
        "extracted": {
            "post_id": post_id,
            "caption": caption,
            "transcript": transcript,
            "ocr_text": ocr_text,
            "visual_concepts": visual_concepts,
            "combined_text": combined,
            "entities": entities,
        },
    }


def build_demo_records() -> list[dict]:
    """>=14 demo-записей по 3 платформам: казино/пирамиды/мошенничество + чистые."""
    recs: list[dict] = []

    # --- 1. Казино-реклама (gambling) — tiktok ---
    recs.append(_record(
        "demo-001", "tiktok", "@bonus_king",
        "https://www.tiktok.com/@bonus_king/video/7300000000000000001",
        "Заноси на 1xBet и забери х2! Промокод BONUS500, рулетка крутится 💰🎰",
        "2026-06-24T09:01:00",
        transcript="Привет, заходи на 1xBet по промокоду, удвоим твой первый депозит, "
                   "крути рулетку и выигрывай прямо сейчас.",
        ocr_text="1XBET ПРОМОКОД BONUS500 БОНУС 500%",
        visual_concepts=[
            {"label": "casino", "score": 0.93},
            {"label": "roulette", "score": 0.88},
        ],
    ))

    # --- 2. Букмекер Mostbet (gambling) — instagram ---
    recs.append(_record(
        "demo-002", "instagram", "@stavka_pro",
        "https://www.instagram.com/p/Cstavka001/",
        "Ставки на Mostbet — выигрываю каждый день! Срочно регистрируйся по ссылке "
        "в профиле, бонус только сегодня 🎰💸",
        "2026-06-24T09:05:00",
        transcript="Сегодня снова выигрыш на Mostbet, ставлю на футбол, успей "
                   "зарегистрироваться пока даётся бонус.",
        ocr_text="MOSTBET СТАВКИ БОНУС +100%",
        visual_concepts=[
            {"label": "betting_slip", "score": 0.9},
            {"label": "cash_flaunt", "score": 0.84},
        ],
    ))

    # --- 3. Казино Vavada (gambling) — youtube ---
    recs.append(_record(
        "demo-003", "youtube", "@slots_master",
        "https://www.youtube.com/watch?v=vavada00001",
        "Казино Vavada дало занос на слотах! Жми на ссылку и забирай фриспины 🎰",
        "2026-06-24T09:09:00",
        transcript="Сегодня заносим в казино Vavada, ловим бонусные вращения на слотах, "
                   "ссылка в описании, переходи и регистрируйся.",
        ocr_text="VAVADA CASINO 200 ФРИСПИНОВ",
        visual_concepts=[
            {"label": "casino", "score": 0.95},
            {"label": "roulette", "score": 0.7},
        ],
    ))

    # --- 4. Финансовая пирамида (pyramid) — tiktok ---
    recs.append(_record(
        "demo-004", "tiktok", "@invest_guru_kz",
        "https://www.tiktok.com/@invest_guru_kz/video/7300000000000000004",
        "Гарантированный доход 30% в месяц! Вложи и забери вдвое. Пиши в личку "
        "@invest_guru_kz, пока есть места 🚀",
        "2026-06-24T09:13:00",
        transcript="Наш фонд даёт гарантированный доход тридцать процентов в месяц "
                   "без риска, вложения от ста долларов, удвоим твой вклад.",
        ocr_text="ДОХОД 30% В МЕСЯЦ ГАРАНТИЯ",
        visual_concepts=[{"label": "cash_flaunt", "score": 0.91}],
    ))

    # --- 5. Пирамида с реф-схемой (pyramid) — instagram ---
    recs.append(_record(
        "demo-005", "instagram", "@team_money_flow",
        "https://www.instagram.com/p/Cteam005/",
        "Пассивный доход 25% в месяц! Приведи друга — получи реферальный бонус. "
        "Реферальная программа уже работает, заходи в команду 💵",
        "2026-06-24T09:17:00",
        transcript="Это сетевой маркетинг нового поколения, приведи друга и твоя "
                   "команда зарабатывает, пассивный доход двадцать пять процентов в месяц.",
        ocr_text="ПАССИВНЫЙ ДОХОД РЕФЕРАЛЬНАЯ ПРОГРАММА",
        visual_concepts=[{"label": "cash_flaunt", "score": 0.78}],
    ))

    # --- 6. Пирамида на казахском (pyramid) — youtube ---
    recs.append(_record(
        "demo-006", "youtube", "@kepildik_tabys",
        "https://www.youtube.com/watch?v=keptab0006",
        "Кепілдік табыс айына 40%! Досыңды шақыр, команда табыс әкеледі. Жеке "
        "хабарлама жаз 📈",
        "2026-06-24T09:21:00",
        transcript="Біздің жоба айына қырық пайыз кепілдік табыс береді, досыңды "
                   "шақыр, рефералдық бағдарлама арқылы команда табыс әкеледі.",
        ocr_text="КЕПІЛДІК ТАБЫС 40%",
        visual_concepts=[{"label": "cash_flaunt", "score": 0.8}],
    ))

    # --- 7. Мошенничество: крипто-«удвоитель» (fraud) — tiktok ---
    recs.append(_record(
        "demo-007", "tiktok", "@crypto_x2_bot",
        "https://www.tiktok.com/@crypto_x2_bot/video/7300000000000000007",
        "Удвоим твой USDT за час! Срочно переведи на кошелёк "
        "0x9f8e7d6c5b4a39281706f5e4d3c2b1a0f9e8d7c6 — успей пока акция!",
        "2026-06-24T09:25:00",
        transcript="Отправь USDT на наш кошелёк и через час получишь вдвое больше, "
                   "акция только сегодня, не упусти шанс.",
        ocr_text="USDT X2 TRC20 СРОЧНО",
        visual_concepts=[{"label": "cash_flaunt", "score": 0.6}],
    ))

    # --- 8. Мошенничество: «работа на дому» + telegram (fraud) — instagram ---
    recs.append(_record(
        "demo-008", "instagram", "@easy_money_job",
        "https://www.instagram.com/p/Cjob008/",
        "Заработок из дома без вложений! Пиши в личку, переходи в Telegram "
        "https://t.me/easy_money_job — успей, мест мало 💸",
        "2026-06-24T09:29:00",
        transcript="Простая работа из дома, две тысячи в день, никаких вложений, "
                   "пиши в телеграм, объясню всё лично.",
        ocr_text="РАБОТА ИЗ ДОМА 2000 В ДЕНЬ",
        visual_concepts=[],
    ))

    # --- 9. Мошенничество: фишинг-выплата (fraud) — youtube ---
    recs.append(_record(
        "demo-009", "youtube", "@gov_payout_help",
        "https://www.youtube.com/watch?v=payout0009",
        "Государство возвращает деньги! Получи выплату 200 000 тг, жми на ссылку "
        "https://vyplata-kz.ru/get и заполни форму. Только сегодня!",
        "2026-06-24T09:33:00",
        transcript="Каждый гражданин может получить выплату, перейдите по ссылке, "
                   "укажите карту, деньги придут в течение часа.",
        ocr_text="ВЫПЛАТА 200000 ТГ ПОЛУЧИ СЕЙЧАС",
        visual_concepts=[],
    ))

    # --- 10. Казино + промокод (gambling) — instagram ---
    recs.append(_record(
        "demo-010", "instagram", "@pinup_bonus",
        "https://www.instagram.com/p/Cpinup010/",
        "Pin-Up casino дарит бонус! Промокод GOLD777, заноси на слоты и рулетку 🎰💰",
        "2026-06-24T09:37:00",
        transcript="Регистрируйся в Pin-Up по промокоду, получай бонус на депозит, "
                   "крути слоты и забирай выигрыш.",
        ocr_text="PIN-UP ПРОМОКОД GOLD777",
        visual_concepts=[
            {"label": "casino", "score": 0.9},
            {"label": "luxury_car", "score": 0.66},
        ],
    ))

    # --- 11. Пирамида: «инвест-клуб» (pyramid) — youtube ---
    recs.append(_record(
        "demo-011", "youtube", "@invest_club_almaty",
        "https://www.youtube.com/watch?v=club0011",
        "Инвест-клуб: вложи 100 000 тг и удвой депозит за 2 месяца. Доход "
        "гарантирован, без риска. Реф-ссылка в описании!",
        "2026-06-24T09:41:00",
        transcript="Закрытый инвест-клуб, удвоим твой депозит, доход гарантирован, "
                   "приглашай по реферальной ссылке и зарабатывай больше.",
        ocr_text="УДВОИМ ДЕПОЗИТ ДОХОД ГАРАНТИРОВАН",
        visual_concepts=[{"label": "cash_flaunt", "score": 0.72}],
    ))

    # --- 12. ЧИСТЫЙ: легальная новость (clean) — youtube ---
    recs.append(_record(
        "demo-012", "youtube", "@almaty_news",
        "https://www.youtube.com/watch?v=news0012",
        "Сегодня в Алматы открылась новая городская библиотека с детским читальным "
        "залом и коворкингом. Вход свободный.",
        "2026-06-24T09:45:00",
        transcript="В центре города торжественно открыли современную библиотеку, "
                   "горожане смогут бесплатно пользоваться книгами и залами.",
        ocr_text="ОТКРЫТИЕ БИБЛИОТЕКИ",
        visual_concepts=[],
    ))

    # --- 13. ЧИСТЫЙ: фин-грамотность от регулятора (clean) — instagram ---
    recs.append(_record(
        "demo-013", "instagram", "@fingramota_kz",
        "https://www.instagram.com/p/Cfin013/",
        "Как распознать финансовую пирамиду: 5 признаков. Не доверяйте обещаниям "
        "лёгких денег. Проверяйте лицензию на сайте регулятора.",
        "2026-06-24T09:49:00",
        transcript="Финансовая грамотность: если вам обещают доход без риска, это "
                   "повод насторожиться, всегда проверяйте лицензию компании.",
        ocr_text="ФИНАНСОВАЯ ГРАМОТНОСТЬ ПАМЯТКА",
        visual_concepts=[],
    ))

    # --- 14. ЧИСТЫЙ: бытовой рецепт (clean) — tiktok ---
    recs.append(_record(
        "demo-014", "tiktok", "@home_recipes",
        "https://www.tiktok.com/@home_recipes/video/7300000000000000014",
        "Готовим домашние баурсаки за 20 минут! Простой рецепт теста и пошаговая "
        "инструкция. Сохраняй, чтобы не потерять 🥟",
        "2026-06-24T09:53:00",
        transcript="Берём муку, дрожжи, молоко, замешиваем тесто, обжариваем "
                   "баурсаки до золотистой корочки, подаём к чаю.",
        ocr_text="РЕЦЕПТ БАУРСАКИ",
        visual_concepts=[],
    ))

    # --- 15. ЧИСТЫЙ: спорт-тренировка (clean) — youtube ---
    recs.append(_record(
        "demo-015", "youtube", "@fit_with_aigerim",
        "https://www.youtube.com/watch?v=fit0015",
        "Утренняя зарядка на 10 минут для бодрого дня. Без инвентаря, подходит "
        "новичкам. Повторяйте за мной!",
        "2026-06-24T09:57:00",
        transcript="Начинаем с разминки шеи, затем приседания и растяжка, "
                   "повторяем упражнения вместе, дышим ровно.",
        ocr_text="УТРЕННЯЯ ЗАРЯДКА 10 МИНУТ",
        visual_concepts=[],
    ))

    # --- Telegram-платформа (F8): дёшево покрываем «и другие платформы» ---
    # У telegram-постов нет audio/ocr/visual — текст и есть сигнал, поэтому
    # combined_text = caption. tg_001 и tg_002 ссылаются на ОДИН канал
    # Casino_Win_KZ -> общая сущность entity:telegram:casino_win_kz -> ребро
    # в графе F5 (координированная сеть). tg_003 — мягкая, неоднозначная
    # формулировка БЕЗ явного %/бренда/промокода -> риск падает в REVIEW-полосу
    # (40-69), чтобы демо-лента показала спред escalate + review + clean.

    # --- tg_001: казино/пирамида, явный escalate (gambling/pyramid) ---
    recs.append(_record(
        "tg_001", "telegram", "@Casino_Win_KZ",
        "https://t.me/Casino_Win_KZ",
        "🎰💰 Гарантированный доход 300% за неделю! Вывод сразу. Реальные заносы "
        "каждый день. Пиши в личку 👉 https://t.me/Casino_Win_KZ Промокод KOZ300",
        "2026-06-24T10:01:00",
        source="telegram",
    ))

    # --- tg_002: реф-схема, делит канал Casino_Win_KZ с tg_001 (escalate) ---
    recs.append(_record(
        "tg_002", "telegram", "@VipStavka",
        "https://t.me/VipStavka",
        "Финансовая свобода с нашим клубом 📈 Заносим по 500 000 ₸ в день. "
        "Реферальная программа: приведи друга — получи бонус. "
        "Канал: https://t.me/Casino_Win_KZ",
        "2026-06-24T10:05:00",
        source="telegram",
    ))

    # --- tg_003: мягкая «инвест-приманка» (НЕ escalate) — показывает, что модель
    # различает, а не штампует всё в красную зону. Намеренно неоднозначная мягкая
    # формулировка: НЕТ явного %, бренда казино, промокода, реквизитов перевода или
    # фишинг-цепочки. Поэтому ни один handcrafted-сигнал не срабатывает, и риск
    # садится ниже порога эскалации (review/clean). ---
    recs.append(_record(
        "tg_003", "telegram", "@InvestSovetnik",
        "https://t.me/InvestSovetnik",
        "Делюсь мыслями о вложениях и доходности, обсуждаем в канале https://t.me/InvestSovetnik",
        "2026-06-24T10:09:00",
        source="telegram",
    ))

    return recs


def main() -> None:
    """Записать demo-записи в config.DEMO_POSTS_PATH (JSONL, ensure_ascii=False)."""
    recs = build_demo_records()
    path = config.DEMO_POSTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # ASCII-санитизация: консоль Windows (cp1252) не печатает кириллицу в пути.
    msg = f"build_demo: wrote {len(recs)} records to {path}"
    sys.stdout.write(msg.encode("ascii", "replace").decode("ascii") + "\n")


if __name__ == "__main__":
    main()
