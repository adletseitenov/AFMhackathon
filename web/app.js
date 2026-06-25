/* КӨЗ — аналитическая консоль (F4). Alpine-состояние + 4 вью.
 *
 * Контракты бэкенда (read-only для фронта):
 *   GET  /api/feed?min_risk=&category=&limit= -> [{post, score, recommended_action}]
 *   GET  /api/post/{id}   -> {post, extracted, score, explanation, recommended_action}
 *   POST /api/tick        -> {revealed, total_revealed}
 *   POST /api/analyze     -> {post, extracted, score, explanation, recommended_action, note?}
 *   GET  /api/graph?post_id=&min_risk= -> {nodes:[{id,label,type,risk,high_risk}], edges:[...]}
 *   GET  /api/trends      -> {by_category, by_platform, by_recommended_action,
 *                             risk_histogram{"0-39","40-69","70-100"}, top_brands, total_posts, ...}
 *   GET  /api/metrics     -> {macro_f1, ...}  (404 пока модель не обучена)
 *
 * Пороги цвета совпадают с config: escalate>=70, review 40-69, иначе clean.
 * Пастельные риск-бейджи зафиксированы (LIGHT minimalist).
 */

// Пастельные риск-цвета (locked) — по 3-уровневой шкале.
const RISK_PALETTE = {
  escalate: { bg: "#FDEBEC", tx: "#9F2F2D", net: "#9F2F2D" },
  review:   { bg: "#FBF3DB", tx: "#956400", net: "#956400" },
  clean:    { bg: "#EDF3EC", tx: "#346538", net: "#346538" },
};

function riskTier(risk) {
  if (risk >= 70) return "escalate";
  if (risk >= 40) return "review";
  return "clean";
}

const CATEGORY_LABELS = {
  gambling: "гемблинг",
  pyramid: "финпирамида",
  fraud: "мошенничество",
  clean: "чисто",
};

const ACTION_LABELS = {
  auto_clear: "авто-очистка",
  review: "на проверку",
  escalate: "эскалация",
};

// RU-метки сигналов собственной модели (зеркалят app/decision/explain._FEATURE_LABELS).
const FEATURE_LABELS = {
  payout_promise: "Обещание гарантированного дохода",
  casino_betting_brand: "Упоминание казино/букмекера",
  dm_cta: "Призыв писать в личку",
  referral: "Реферальная схема",
  promo_code: "Промокод",
  crypto_iban: "Крипто-кошелёк/реквизиты",
  urgency: "Срочность/давление",
  money_emoji: "Демонстрация денег",
  visual_gambling: "Визуальные маркеры азартных игр",
};

const PLATFORM_ICONS = {
  tiktok: "ph-music-notes",
  instagram: "ph-instagram-logo",
  youtube: "ph-youtube-logo",
  telegram: "ph-telegram-logo",
  twitch: "ph-twitch-logo",
  kick: "ph-monitor-play",
  link: "ph-link",
  upload: "ph-upload-simple",
};

// Происхождение поста (post.source): seed | live | discovered | telegram.
// «live» — выгрузка реального канала; «discovered» — авто-поиск по YouTube.
const SOURCE_LABELS = {
  seed: "демо",
  live: "загрузка",
  discovered: "автопоиск",
  telegram: "Telegram",
};
const SOURCE_ICONS = {
  seed: "ph-flask",
  live: "ph-download-simple",
  discovered: "ph-radar",
  telegram: "ph-telegram-logo",
};

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// IntersectionObserver: мягкий fade-in для .reveal элементов.
const _revealObserver =
  "IntersectionObserver" in window
    ? new IntersectionObserver(
        (entries) => {
          entries.forEach((e) => {
            if (e.isIntersecting) {
              e.target.classList.add("in-view");
              _revealObserver.unobserve(e.target);
            }
          });
        },
        { threshold: 0.05 }
      )
    : null;

function observeReveals(root) {
  if (!_revealObserver) return;
  (root || document)
    .querySelectorAll(".reveal:not(.in-view)")
    .forEach((el) => _revealObserver.observe(el));
}

function kozApp() {
  return {
    // --- nav ---
    tab: "feed",
    tabs: [
      { id: "feed", label: "Лента", icon: "ph-queue" },
      { id: "graph", label: "Граф", icon: "ph-graph" },
      { id: "trends", label: "Тренды", icon: "ph-chart-bar" },
      { id: "watch", label: "Мониторинг", icon: "ph-binoculars" },
      { id: "live", label: "Живая проверка", icon: "ph-shield-check" },
      { id: "registry", label: "Реестр лицензий", icon: "ph-seal-check" },
    ],

    // --- top bar ---
    macroF1: null,

    // --- «Актуальные проблемы» (GET /api/hotspots) — самое опасное прямо сейчас ---
    // {top_problems:[{category,label,count,avg_risk,escalate_count}],
    //  top_operators:[{brand,count,avg_risk,licensed}],
    //  top_telegram:[{channel,count,avg_risk}], top_youtube:[{channel,count,avg_risk}],
    //  recommendations:[{title,rationale,action,priority,evidence,score}]}.
    hotspots: null,
    hotspotsLoading: false,
    hotspotsError: "",

    // --- feed / queue ---
    feed: [],
    feedLoading: true,
    feedError: "",
    categoryFilter: "",
    platformFilter: "",
    sortBy: "relevance",     // порядок очереди: relevance | novelty | newest | popularity
    realOnly: true,          // ЛЕНТА defaults to real posts only (real_only=1)
    actionFilter: "",        // уровень опасности: '' | escalate | review | clean (-> &action=)
    licensedFilter: "all",   // легальность оператора: all | licensed | unlicensed (-> &licensed=)
    sinceFilter: "",         // посты не старше этой даты публикации (ISO yyyy-mm-dd -> &since=)
    queueSaving: false,      // POST /api/post/{id}/queue в полёте — блокирует двойные клики
    ticking: false,
    _knownIds: new Set(),
    _newIds: new Set(),
    _pollTimer: null,

    // --- autonomous discovery (POST /api/discover -> {job_id}; poll /api/jobs/{id}) ---
    discovering: false,
    discoverStage: "",       // RU stage string from the running job
    discoverProgress: 0,     // 0..100
    discoverError: "",
    discoverResult: null,    // {added, youtube_added, telegram_added, flagged, queries, samples:[...]}
    discoverPlatform: "",    // ""=все | youtube | telegram | tiktok | instagram
    discoverDeep: false,     // «глубокий разбор видео» — audio→текст + OCR + визуальные маркеры
    _discoverDeepRun: false, // запомненный режим запущенной задачи (для текста результата)
    _discoverJobId: null,
    _discoverTimer: null,
    discoverPlatforms: [
      { id: "",          label: "Все" },
      { id: "youtube",   label: "YouTube" },
      { id: "telegram",  label: "Telegram" },
      { id: "tiktok",    label: "TikTok" },
      { id: "instagram", label: "Instagram" },
      { id: "twitch",    label: "Twitch" },
      { id: "kick",      label: "Kick" },
    ],

    // --- настраиваемые фильтры автопоиска (GET /api/discover/catalog) ---
    // Селекторы наполняются с бэка; пока каталог не загружен — безопасные дефолты.
    discoverCountry: "kz",          // одиночный выбор (страна)
    discoverCategories: [],          // мульти-выбор (категории); []/['all'] = все
    discoverContentType: "all",      // all | video | live
    discoverSort: "relevance",       // relevance | recent | popular
    discoverDanger: "any",           // any | high | medium | low — искать только этот уровень опасности
    catalogCountries: [{ id: "kz", label: "Казахстан" }, { id: "ru", label: "Россия" }, { id: "all", label: "Все страны" }],
    catalogCategories: [{ id: "all", label: "Все категории" }],
    catalogContentTypes: [{ id: "all", label: "Все" }, { id: "video", label: "VOD/посты" }, { id: "live", label: "Прямые эфиры" }],
    catalogSorts: [{ id: "relevance", label: "по релевантности" }, { id: "recent", label: "по новизне" }, { id: "popular", label: "по популярности" }],
    _catalogLoaded: false,

    // --- telegram scanner (Лента) ---
    scanInput: "",
    scanning: false,
    scanError: "",
    scanResult: null, // {added, flagged, channels:[{channel,fetched,added,flagged}]}

    // --- watchlist (Мониторинг) ---
    watchlist: [],
    watchLoading: false,
    watchError: "",
    watchInput: "",
    watchAdding: false,
    watchScanning: false,
    watchScanResult: null,
    // derived per-channel stats from the real feed: {'@channel': {collected, flagged}}
    watchStats: {},
    watchStatsLoading: false,

    // --- мультиплатформенный мониторинг (Ф6) ---
    // /api/watchlist/entries -> {entries:[{target,platform,last_scan,last_added,
    //   last_flagged,total_collected,licensed:bool|null}], watched:[{target,platform}]}
    watchEntries: [],
    watchWatched: [],
    watchEntriesLoading: false,
    watchEntriesError: "",
    // детальная статистика записи мониторинга (модалка по клику): все посts + агрегаты
    monitorDetail: null,
    monitorDetailLoading: false,
    monitorDetailError: "",
    monitorTarget: "",
    monitorRecs: [],
    watchPlatform: "telegram",  // выбранная площадка в форме добавления
    watchPlatforms: [
      { id: "telegram", label: "Telegram" },
      { id: "youtube", label: "YouTube" },
      { id: "tiktok", label: "TikTok" },
      { id: "twitch", label: "Twitch" },
      { id: "kick", label: "Kick" },
      { id: "instagram", label: "Instagram" },
      { id: "operator", label: "Контора (оператор)" },
    ],

    // --- «Следить за конторой» (Ф7) ---
    watchFollowNote: "",   // зелёный toast-подтверждение
    followSaving: false,   // POST в полёте — не дублируем
    _followTimer: null,

    // --- drill-down ---
    detail: null,
    detailLoading: false,
    selectedId: null,

    // --- analyst verdict (POST /api/feedback/verdict) ---
    verdictReason: "",          // optional free-text reason
    verdictCategory: "gambling",// category for reclassify (gambling|pyramid|fraud|clean)
    showReclassify: false,      // reveals the category select
    verdictSaving: false,       // request in flight
    verdictNote: "",            // small confirmation («учтено»)
    verdictError: "",

    // --- model training widget (GET /api/feedback/stats, POST /api/feedback/retrain) ---
    fbStats: null,              // {total_labels, labels_until_retrain, by_label}
    retraining: false,
    retrainStage: "",
    retrainProgress: 0,
    retrainError: "",
    retrainResult: null,        // {before, after, delta, total_labels}
    _retrainJobId: null,
    _retrainTimer: null,

    // --- uncertainty sub-view («На грани») ---
    feedMode: "queue",          // "queue" | "uncertain"
    uncertain: [],
    uncertainLoading: false,
    uncertainError: "",

    // --- graph ---
    graphMinRisk: 0,
    graphError: "",
    graphLoading: false,
    graphEmpty: false,
    _network: null,

    // --- мини-граф связей в карточке анализа (отдельный экземпляр сети) ---
    detailGraphNote: "",   // RU-текст заглушки, если граф не построить
    _detailNetwork: null,

    // --- trends ---
    trends: null,
    trendsError: "",
    _charts: {},
    _trendsLoaded: false,

    // --- рекомендации АФМ (GET /api/recommendations) ---
    // {stats:{total_posts, flagged, top_platform, top_brand},
    //  recommendations:[{title, rationale, action, priority:'high'|'medium'|'low', evidence}]}
    recommendations: [],
    recsStats: null,
    recsLoading: false,
    recsError: "",
    // селектор «проблемы и источники» рекомендаций (GET /api/recommendations/sources)
    // {categories:[{id,label,count}], brands:[{id,label,count,licensed}], platforms:[{id,label,count}]}
    recSources: null,
    recSourcesLoaded: false,
    recFocus: "",   // "" = глобально | "kind:value" (category:gambling | brand:1xbet | platform:tiktok)

    // --- реестр лицензий (GET/POST /api/licensed) — что легально в РК ---
    // {operators:[{name,note,source:'default'|'added',keywords:[]}], disclaimer, compliance_hint}
    registry: null,
    registryLoading: false,
    registryError: "",
    registryNote: "",            // тост после изменения («1xBet помечен лицензированным»)
    registrySaving: "",          // имя оператора, по которому идёт сохранение (для спиннера)
    newOpName: "",               // форма «добавить оператора»: имя
    newOpKeywords: "",           // ключевые слова (через запятую)

    // --- live check (async job: POST /api/analyze -> {job_id}; poll /api/jobs/{id}) ---
    liveUrl: "",
    liveFile: null,
    liveResult: null,
    liveLoading: false,
    liveError: "",
    liveStage: "",        // RU stage string from the running job
    liveProgress: 0,      // 0..100
    _jobId: null,
    _jobTimer: null,

    // ===================================================== lifecycle
    init() {
      this.loadMetrics();
      this.loadCatalog();
      this.loadHotspots();
      this.loadFeed();
      this.loadFeedbackStats();
      this._pollTimer = setInterval(() => {
        if (this.tab === "feed" && this.feedMode === "queue") this.loadFeed();
      }, 2000);
    },

    // Каталог настраиваемых фильтров автопоиска. GET /api/discover/catalog ->
    // {countries:[{id,label}], categories:[...], content_types:[...], sorts:[...]}.
    // Если эндпоинт недоступен (старый бэк) — молча оставляем встроенные дефолты,
    // селекторы всё равно рабочие. Применяем только непустые списки.
    async loadCatalog() {
      if (this._catalogLoaded) return;
      try {
        const r = await fetch("/api/discover/catalog");
        if (!r.ok) return;
        const c = await r.json();
        if (c && Array.isArray(c.countries) && c.countries.length) {
          this.catalogCountries = c.countries;
          if (!c.countries.some((x) => x.id === this.discoverCountry)) {
            this.discoverCountry = c.countries[0].id;
          }
        }
        if (c && Array.isArray(c.categories) && c.categories.length) {
          this.catalogCategories = c.categories;
        }
        if (c && Array.isArray(c.content_types) && c.content_types.length) {
          this.catalogContentTypes = c.content_types;
          if (!c.content_types.some((x) => x.id === this.discoverContentType)) {
            this.discoverContentType = c.content_types[0].id;
          }
        }
        if (c && Array.isArray(c.sorts) && c.sorts.length) {
          this.catalogSorts = c.sorts;
          if (!c.sorts.some((x) => x.id === this.discoverSort)) {
            this.discoverSort = c.sorts[0].id;
          }
        }
        this._catalogLoaded = true;
      } catch (_) {
        /* offline / старый бэк — оставляем встроенные дефолты */
      }
    },

    // Переключатель категории в мульти-выборе автопоиска.
    toggleDiscoverCategory(id) {
      const i = this.discoverCategories.indexOf(id);
      if (i >= 0) this.discoverCategories.splice(i, 1);
      else this.discoverCategories.push(id);
    },
    isDiscoverCategory(id) {
      return this.discoverCategories.indexOf(id) >= 0;
    },

    setTab(id) {
      this.tab = id;
      // Граф: ВСЕГДА перестраиваем из свежих данных при входе во вкладку
      // (чтобы отразить вновь найденные/просканированные посты).
      if (id === "graph") this.$nextTick(() => this.loadGraph());
      if (id === "trends") this.$nextTick(() => this.loadTrends());
      if (id === "watch") {
        this.loadWatchEntries();   // основной мультиплатформенный дашборд (Ф6)
        this.loadWatchlist();      // telegram-список как fallback/совместимость
        this.loadWatchStats();
      }
      if (id === "registry") this.loadRegistry();
    },

    // ===================================================== top bar
    async loadMetrics() {
      try {
        const r = await fetch("/api/metrics");
        if (!r.ok) return; // 404 пока модель не обучена — просто прочерк
        const m = await r.json();
        if (typeof m.macro_f1 === "number") this.macroF1 = m.macro_f1;
      } catch (_) {
        /* offline — оставляем прочерк */
      }
    },

    // ===================================================== «Актуальные проблемы»
    //
    // GET /api/hotspots -> ВСЕГДА 5 ключей (списки могут быть пустыми):
    //   {top_problems, top_operators, top_telegram, top_youtube, recommendations}.
    // Авто-загрузка при открытии консоли (init) + кнопка «Обновить». Ошибку
    // показываем в плашке (как другие fetch-методы), консоль не падает.
    async loadHotspots() {
      if (this.hotspotsLoading) return;
      this.hotspotsLoading = true;
      this.hotspotsError = "";
      try {
        const r = await fetch("/api/hotspots");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.hotspots = await r.json();
        this.$nextTick(() => observeReveals());
      } catch (e) {
        this.hotspotsError = "Не удалось загрузить актуальные проблемы: " + e.message;
      } finally {
        this.hotspotsLoading = false;
      }
    },

    // Суммарно эскалаций по всем актуальным проблемам (Ф8: сводка-счётчик).
    get hotspotsEscalateTotal() {
      const probs = (this.hotspots && this.hotspots.top_problems) || [];
      return probs.reduce((s, p) => s + (Number(p.escalate_count) || 0), 0);
    },

    // Бейдж/число avg_risk: округляем float и красим по зафиксированной палитре.
    avgRiskBadge(risk) { return this.riskBadge(Math.round(Number(risk) || 0)); },

    // ===================================================== feed
    async loadFeed() {
      let url = "/api/feed?limit=100";
      if (this.realOnly) url += "&real_only=1";   // по умолчанию — только реальные посты
      if (this.categoryFilter) url += "&category=" + encodeURIComponent(this.categoryFilter);
      if (this.platformFilter) url += "&platform=" + encodeURIComponent(this.platformFilter);
      // уровень опасности (рекомендованное действие) — отдельный фильтр
      if (this.actionFilter) url += "&action=" + encodeURIComponent(this.actionFilter);
      // легальность оператора — all не прокидываем (это дефолт бэка)
      if (this.licensedFilter && this.licensedFilter !== "all") {
        url += "&licensed=" + encodeURIComponent(this.licensedFilter);
      }
      // посты не старше выбранной даты публикации
      if (this.sinceFilter) url += "&since=" + encodeURIComponent(this.sinceFilter);
      url += "&sort=" + encodeURIComponent(this.sortBy);
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();

        // отметить новые элементы для мягкого пульса
        const fresh = [];
        for (const row of data) {
          if (!this._knownIds.has(row.post.id)) {
            this._knownIds.add(row.post.id);
            if (!this.feedLoading) fresh.push(row.post.id);
          }
        }
        if (fresh.length) {
          fresh.forEach((id) => this._newIds.add(id));
          setTimeout(() => {
            fresh.forEach((id) => this._newIds.delete(id));
          }, 6000);
        }

        this.feed = data;
        this.feedError = "";
        this.feedLoading = false;
        this.$nextTick(() => observeReveals());
      } catch (e) {
        this.feedError = "Не удалось загрузить очередь: " + e.message;
        this.feedLoading = false;
      }
    },

    isNew(id) {
      return this._newIds.has(id);
    },

    // «Обновить»: всегда перезагружает ленту (видимый отклик). В демо-режиме
    // (real_only выкл) дополнительно раскрывает следующую порцию seed-постов.
    async refresh() {
      if (this.ticking) return;
      this.ticking = true;
      try {
        if (!this.realOnly) {
          await fetch("/api/tick", { method: "POST" });
        }
        await this.loadFeed();
      } catch (e) {
        this.feedError = "Не удалось обновить: " + e.message;
      } finally {
        this.ticking = false;
      }
    },

    // Переключатель «Только реальные»: меняет real_only и перезагружает ленту.
    toggleRealOnly() {
      this.realOnly = !this.realOnly;
      this.feedLoading = true;
      this.loadFeed();
    },

    // Поставить/снять пост в очередь на разбор.
    // POST /api/post/{id}/queue {queued} -> {ok, post_id, queued}. Мутируем post.queued
    // (Alpine реактивен), затем перезагружаем ленту, чтобы она отразила изменение
    // (снятый из очереди пост пропадёт из ленты — это ожидаемо). queueSaving не даёт
    // кликнуть дважды. В drill-down карточку НЕ сбрасываем — просто обновляем флаг.
    async toggleQueue(post) {
      if (this.queueSaving || !post || !post.id) return;
      this.queueSaving = true;
      const next = !post.queued;
      try {
        const r = await fetch("/api/post/" + encodeURIComponent(post.id) + "/queue", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ queued: next }),
        });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        const data = await r.json();
        // мутируем объект — Alpine увидит изменение и в ленте, и в drill-down
        post.queued = typeof data.queued === "boolean" ? data.queued : next;
        this.feedError = "";
        await this.loadFeed(); // лента отражает изменение (снятый пост исчезнет)
      } catch (e) {
        this.feedError = "Не удалось изменить очередь: " + e.message;
      } finally {
        this.queueSaving = false;
      }
    },

    // ===================================================== autonomous discovery
    //
    // THE headline feature. POST /api/discover {per_query, with_telegram} -> {job_id}.
    // We poll GET /api/jobs/{job_id} every ~800ms for {status, stage, progress, result, error}
    // until status is 'done' or 'error', surfacing the RU stage + percent meanwhile.
    // On 'done' the result carries {added, youtube_added, telegram_added, flagged, queries, samples}.
    async discover() {
      if (this.discovering) return;
      this._stopDiscoverPoll();
      this.discovering = true;
      this.discoverError = "";
      this.discoverResult = null;
      this.discoverStage = "постановка в очередь";
      this.discoverProgress = 0;
      // Запоминаем режим запуска, чтобы корректно описать результат, даже
      // если оператор переключит тумблеры пока задача выполняется.
      this._discoverDeepRun = this.discoverDeep;
      // Платформенный охват: пустая строка означает «все площадки».
      // Telegram включаем в обход только когда выбраны «Все» или «Telegram».
      const platform = this.discoverPlatform || "";
      const withTelegram = platform === "" || platform === "telegram";
      try {
        const r = await fetch("/api/discover", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            per_query: 4,
            platform: platform,
            deep: this.discoverDeep,
            with_telegram: withTelegram,
            // настраиваемые фильтры автопоиска (бэк дополняет existing-поля)
            country: this.discoverCountry,
            categories: this.discoverCategories,
            content_type: this.discoverContentType,
            sort: this.discoverSort,
            danger: this.discoverDanger,
          }),
        });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        const start = await r.json();
        if (!start.job_id) {
          // На случай синхронного ответа с готовым результатом.
          if (start.added !== undefined || start.samples) {
            this._applyDiscoverResult(start);
            this.discovering = false;
            await this.loadFeed();
            return;
          }
          throw new Error("сервер не вернул идентификатор задачи");
        }
        this._discoverJobId = start.job_id;
        this._pollDiscover();
      } catch (e) {
        this.discoverError = "Поиск не удался: " + e.message;
        this.discovering = false;
      }
    },

    _pollDiscover() {
      this._discoverTimer = setInterval(async () => {
        if (!this._discoverJobId) return;
        try {
          const r = await fetch("/api/jobs/" + encodeURIComponent(this._discoverJobId));
          if (r.status === 404) {
            // Задача неизвестна серверу: чаще всего сервер перезапустили (реестр задач
            // живёт в памяти). НЕ зависаем на красной ошибке — уже найденные посты
            // сохранены в БД, обновляем ленту и мягко сообщаем.
            this._stopDiscoverPoll();
            this.discovering = false;
            this.discoverError = "";
            this.discoverResult = { note: "Поиск прерван (сервер перезапущён) — уже найденные посты в ленте ниже." };
            await this.loadFeed();
            return;
          }
          if (!r.ok) throw new Error("HTTP " + r.status);
          const job = await r.json();
          this.discoverStage = job.stage || this.discoverStage || "поиск в интернете";
          if (typeof job.progress === "number") this.discoverProgress = job.progress;

          if (job.status === "done") {
            this._stopDiscoverPoll();
            this._applyDiscoverResult(job.result || {});
            this.discovering = false;
            await this.loadFeed(); // найденные РЕАЛЬНЫЕ посты появляются в ленте
          } else if (job.status === "error") {
            this._stopDiscoverPoll();
            this.discoverError = "Поиск не удался: " + (job.error || "неизвестная ошибка");
            this.discovering = false;
          }
        } catch (e) {
          // временный сбой сети — показываем, но не убиваем задачу
          this.discoverError = "Потеряна связь с задачей поиска: " + e.message;
        }
      }, 800);
    },

    _applyDiscoverResult(res) {
      this.discoverResult = {
        added: res.added || 0,
        youtube_added: res.youtube_added || 0,
        telegram_added: res.telegram_added || 0,
        flagged: res.flagged || 0,
        queries: res.queries || [],
        samples: res.samples || [],
        note: res.note || "",
        telegram_chats: res.telegram_chats || 0,
        telegram_chat_links: res.telegram_chat_links || [],
      };
    },

    _stopDiscoverPoll() {
      if (this._discoverTimer) { clearInterval(this._discoverTimer); this._discoverTimer = null; }
      this._discoverJobId = null;
    },

    // ===================================================== drill-down
    async openPost(id) {
      this.selectedId = id;
      this.detailLoading = true;
      this.detail = null;
      this.detailGraphNote = "";
      // прошлый мини-граф уничтожаем сразу — карточка перерисовывается
      if (this._detailNetwork) { this._detailNetwork.destroy(); this._detailNetwork = null; }
      try {
        const r = await fetch("/api/post/" + encodeURIComponent(id));
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.detail = await r.json();
      } catch (e) {
        this.feedError = "Не удалось открыть материал: " + e.message;
      } finally {
        this.detailLoading = false;
        // открытие нового материала сбрасывает черновик вердикта
        this.showReclassify = false;
        this.verdictReason = "";
        this.verdictCategory = "gambling";
        this.verdictNote = "";
        this.verdictError = "";
        this.$nextTick(() => {
          observeReveals();
          // мини-граф связей строим после рендера карточки (когда detail установлен)
          if (this.detail) this.renderDetailGraph();
        });
      }
    },

    // Мини-граф связей материала (эго-сеть из detail.graph). ОТДЕЛЬНЫЙ экземпляр
    // сети (_detailNetwork), чтобы не конфликтовать со вкладкой «Граф» (_network).
    // По образцу loadGraph: та же риск-палитра для постов, белые box-узлы для
    // сущностей, зелёная обводка + ✓ для лицензированных операторов. Клик по
    // узлу-посту открывает его разбор. Если связей нет (<=1 узла) — тихо
    // показываем заглушку и сеть не строим.
    renderDetailGraph() {
      // всегда уничтожаем прошлый мини-граф перед свежим построением
      if (this._detailNetwork) { this._detailNetwork.destroy(); this._detailNetwork = null; }
      // карточка закрыта — строить нечего
      if (!this.detail) return;

      const g = this.detail.graph;
      // нет связей: заглушку показывает разметка (x-show), сеть не строим
      if (!g || !g.nodes || g.nodes.length <= 1) return;

      if (typeof vis === "undefined" || !vis.Network) {
        this.detailGraphNote = "Библиотека графа недоступна (CDN заблокирован).";
        return;
      }
      const el = document.getElementById("detailGraph");
      if (!el) return;
      el.innerHTML = "";

      const nodes = g.nodes.map((n) => {
        const isPost = n.type === "post";
        const pal = RISK_PALETTE[riskTier(n.risk || 0)];
        const lic = !!n.licensed;  // лицензированный в РК оператор — зелёная обводка + ✓
        const base = isPost
          ? { background: pal.net, border: pal.tx, highlight: { background: pal.net, border: "#111111" } }
          : { background: "#FFFFFF", border: "#C9C9C7", highlight: { background: "#F1F1EF", border: "#787774" } };
        if (lic) {
          base.border = "#346538";
          base.highlight = { background: base.background, border: "#346538" };
        }
        const ops = (n.licensed_operators || []).join(", ");
        return {
          id: n.id,
          label: (lic ? "✓ " : "") + n.label,
          shape: isPost ? "dot" : "box",
          size: isPost ? 12 + Math.round((n.risk || 0) / 6) : 10,
          color: base,
          borderWidth: lic ? 3 : 1.5,
          font: { color: isPost ? "#111111" : "#787774", size: 12, face: "Geist Sans, system-ui, sans-serif" },
          title: lic
            ? "Разрешён в РК" + (ops ? ": " + ops : "") + " — блокировка не требуется, проверить рекламные нормы"
            : undefined,
          _post: isPost ? n.id.replace(/^post:/, "") : null,
        };
      });
      const edges = (g.edges || []).map((e) => ({
        from: e.source,
        to: e.target,
        color: { color: "#EAEAEA", highlight: "#787774" },
        width: 1,
        smooth: { type: "continuous" },
      }));

      const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
      const options = {
        interaction: { hover: true, tooltipDelay: 120 },
        // ограничиваем итерации стабилизации (по умолчанию 1000) — эго-граф мал, но
        // нет смысла крутить полную физику; замораживаем сразу после раскладки.
        physics: {
          stabilization: { enabled: true, iterations: 200, updateInterval: 25, fit: true },
          barnesHut: { gravitationalConstant: -3500, springLength: 120 },
        },
        nodes: { borderWidth: 1.5 },
      };
      this._detailNetwork = new vis.Network(el, data, options);
      this._detailNetwork.once("stabilizationIterationsDone", () => {
        try { this._detailNetwork.setOptions({ physics: false }); } catch (_) {}
      });

      this._detailNetwork.on("click", (params) => {
        if (!params.nodes.length) return;
        const node = data.nodes.get(params.nodes[0]);
        if (node && node._post) this.openPost(node._post);
      });
    },

    // ===================================================== analyst verdict
    //
    // POST /api/feedback/verdict {post_id, verdict:'confirm'|'reject'|'reclassify',
    //                             category?, reason?}. Подтверждаем «учтено» и
    //                             освежаем статистику обучения.
    async submitVerdict(verdict) {
      if (this.verdictSaving) return;
      if (!this.detail || !this.detail.post) return;
      this.verdictSaving = true;
      this.verdictNote = "";
      this.verdictError = "";
      const body = {
        post_id: this.detail.post.id,
        verdict: verdict,
      };
      if (verdict === "reclassify") body.category = this.verdictCategory;
      const reason = (this.verdictReason || "").trim();
      if (reason) body.reason = reason;
      try {
        const r = await fetch("/api/feedback/verdict", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.verdictNote = "учтено";
        this.showReclassify = false;
        this.verdictReason = "";
        // вердикт — новая метка для дообучения: освежаем счётчик
        this.loadFeedbackStats();
        // спрятать «учтено» через несколько секунд
        setTimeout(() => { this.verdictNote = ""; }, 4000);
      } catch (e) {
        this.verdictError = "Не удалось сохранить вердикт: " + e.message;
      } finally {
        this.verdictSaving = false;
      }
    },

    // ===================================================== model training
    //
    // GET /api/feedback/stats -> {total_labels, labels_until_retrain, by_label}.
    async loadFeedbackStats() {
      try {
        const r = await fetch("/api/feedback/stats");
        if (!r.ok) return; // эндпоинт ещё не поднят — просто прячем виджет
        this.fbStats = await r.json();
      } catch (_) {
        /* offline — оставляем прежнее значение */
      }
    },

    // POST /api/feedback/retrain -> {job_id}; затем poll GET /api/jobs/{id}
    // до {before, after, delta, total_labels}. Тот же паттерн, что discover()/analyze().
    async retrain() {
      if (this.retraining) return;
      this._stopRetrainPoll();
      this.retraining = true;
      this.retrainError = "";
      this.retrainResult = null;
      this.retrainStage = "постановка в очередь";
      this.retrainProgress = 0;
      try {
        const r = await fetch("/api/feedback/retrain", { method: "POST" });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        const start = await r.json();
        if (!start.job_id) {
          // На случай синхронного ответа с готовым результатом.
          if (start.after !== undefined || start.delta !== undefined) {
            this._applyRetrainResult(start);
            this.retraining = false;
            this.loadFeedbackStats();
            this.loadMetrics();
            return;
          }
          throw new Error("сервер не вернул идентификатор задачи");
        }
        this._retrainJobId = start.job_id;
        this._pollRetrain();
      } catch (e) {
        this.retrainError = "Переобучение не удалось: " + e.message;
        this.retraining = false;
      }
    },

    _pollRetrain() {
      this._retrainTimer = setInterval(async () => {
        if (!this._retrainJobId) return;
        try {
          const r = await fetch("/api/jobs/" + encodeURIComponent(this._retrainJobId));
          if (r.status === 404) {
            // задача неизвестна (сервер перезапущен) — не зависаем на ошибке поллинга
            this._stopRetrainPoll();
            this.retraining = false;
            this.retrainError = "Переобучение прервано (сервер перезапущён). Запустите его заново.";
            return;
          }
          if (!r.ok) throw new Error("HTTP " + r.status);
          const job = await r.json();
          this.retrainStage = job.stage || this.retrainStage || "переобучение";
          if (typeof job.progress === "number") this.retrainProgress = job.progress;

          if (job.status === "done") {
            this._stopRetrainPoll();
            this._applyRetrainResult(job.result || {});
            this.retraining = false;
            this.loadFeedbackStats(); // счётчик до переобучения обнулился
            this.loadMetrics();       // macro-F1 в шапке мог измениться
          } else if (job.status === "error") {
            this._stopRetrainPoll();
            this.retrainError = "Переобучение не удалось: " + (job.error || "неизвестная ошибка");
            this.retraining = false;
          }
        } catch (e) {
          this.retrainError = "Потеряна связь с задачей переобучения: " + e.message;
        }
      }, 800);
    },

    _applyRetrainResult(res) {
      this.retrainResult = {
        before: typeof res.before === "number" ? res.before : null,
        after: typeof res.after === "number" ? res.after : null,
        delta: typeof res.delta === "number" ? res.delta : null,
        total_labels: res.total_labels || 0,
      };
    },

    _stopRetrainPoll() {
      if (this._retrainTimer) { clearInterval(this._retrainTimer); this._retrainTimer = null; }
      this._retrainJobId = null;
    },

    // знак дельты F1 для подписи (+0.03 / −0.01)
    deltaLabel(d) {
      if (typeof d !== "number") return "";
      const sign = d > 0 ? "+" : (d < 0 ? "−" : "±");
      return sign + Math.abs(d).toFixed(2);
    },

    // ===================================================== «На грани» (uncertainty)
    //
    // GET /api/feedback/uncertain?limit= -> [{post, score, recommended_action}] —
    // та же форма, что и лента; рендерим тем же card-разметкой.
    setFeedMode(mode) {
      this.feedMode = mode;
      if (mode === "uncertain") this.loadUncertain();
    },

    // активный список карточек: основная очередь либо пограничные материалы.
    // позволяет переиспользовать ту же card-разметку для обоих режимов.
    get cards() {
      return this.feedMode === "uncertain" ? this.uncertain : this.feed;
    },

    async loadUncertain() {
      this.uncertainLoading = true;
      this.uncertainError = "";
      try {
        const r = await fetch("/api/feedback/uncertain?limit=40");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.uncertain = await r.json();
        this.$nextTick(() => observeReveals());
      } catch (e) {
        this.uncertainError = "Не удалось загрузить пограничные материалы: " + e.message;
      } finally {
        this.uncertainLoading = false;
      }
    },

    // ===================================================== graph
    //
    // ВСЕГДА тянет свежие /api/graph при входе во вкладку и по кнопке «Обновить».
    // Прежний экземпляр vis-network уничтожается перед созданием нового, иначе
    // остаётся «мёртвый» канвас и граф не отражает новые посты.
    async loadGraph() {
      this.graphError = "";
      this.graphEmpty = false;
      const el = document.getElementById("graph");
      if (!el) return;

      if (typeof vis === "undefined" || !vis.Network) {
        this.graphError = "Библиотека графа недоступна (CDN заблокирован).";
        return;
      }

      // всегда уничтожаем прошлый граф перед свежим построением
      if (this._network) { this._network.destroy(); this._network = null; }
      el.innerHTML = "";
      this.graphLoading = true;
      try {
        const r = await fetch("/api/graph?min_risk=" + this.graphMinRisk);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const g = await r.json();
        this.graphLoading = false;

        if (!g.nodes || !g.nodes.length) {
          this.graphEmpty = true;
          el.innerHTML =
            '<div class="h-full grid place-items-center text-muted text-[13px]">Нет связей при заданном пороге риска. Понизьте порог или найдите новые посты.</div>';
          return;
        }

        // ПРОИЗВОДИТЕЛЬНОСТЬ: при большом графе (>400 узлов) полная силовая
        // стабилизация vis-network + improvedLayout (Kamada-Kawai, ~O(n^2)) фризят
        // главный поток на секунды. Для больших графов отключаем дорогой пре-лейаут,
        // ограничиваем итерации физики и замораживаем её после первичной раскладки.
        const big = g.nodes.length > 400;

        const nodes = g.nodes.map((n) => {
          const isPost = n.type === "post";
          const pal = RISK_PALETTE[riskTier(n.risk || 0)];
          const lic = !!n.licensed;  // лицензированный в РК оператор — зелёная обводка + ✓
          const base = isPost
            ? { background: pal.net, border: pal.tx, highlight: { background: pal.net, border: "#111111" } }
            : { background: "#FFFFFF", border: "#C9C9C7", highlight: { background: "#F1F1EF", border: "#787774" } };
          if (lic) {
            base.border = "#346538";
            base.highlight = { background: base.background, border: "#346538" };
          }
          const ops = (n.licensed_operators || []).join(", ");
          return {
            id: n.id,
            label: (lic ? "✓ " : "") + n.label,
            shape: isPost ? "dot" : "box",
            size: isPost ? 12 + Math.round((n.risk || 0) / 6) : 10,
            color: base,
            borderWidth: lic ? 3 : 1.5,
            font: { color: isPost ? "#111111" : "#787774", size: 12, face: "Geist Sans, system-ui, sans-serif" },
            title: lic
              ? "Разрешён в РК" + (ops ? ": " + ops : "") + " — блокировка не требуется, проверить рекламные нормы. Клик — следить за конторой."
              : (this.isOperatorNode(n) ? ("Контора «" + n.label + "» — клик: следить за конторой (мониторинг)") : undefined),
            _post: isPost ? n.id.replace(/^post:/, "") : null,
            // узел-контора: бренд казино/букмекера (casino_brand/betting_brand) ИЛИ
            // licensed-оператор — клик добавляет контору в мониторинг (Ф7). Включает
            // НЕлицензированные бренды (1xbet/mostbet) — их и нужно мониторить.
            _brand: this.isOperatorNode(n) ? n.label : null,
          };
        });
        const edges = (g.edges || []).map((e) => ({
          from: e.source,
          to: e.target,
          color: { color: "#EAEAEA", highlight: "#787774" },
          width: 1,
          // прямые рёбра для больших графов — без дорогих безье на каждое ребро
          smooth: big ? false : { type: "continuous" },
        }));

        const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
        const options = {
          interaction: { hover: true, tooltipDelay: 120 },
          // improvedLayout (Kamada-Kawai пре-лейаут) ~O(n^2) — для больших графов выкл.
          layout: { improvedLayout: !big },
          physics: {
            // ограничиваем итерации стабилизации (по умолчанию 1000) — главный фриз
            stabilization: { enabled: true, iterations: big ? 120 : 250, updateInterval: 25, fit: true },
            barnesHut: { gravitationalConstant: -3500, springLength: 120 },
          },
          nodes: { borderWidth: 1.5 },
        };
        // экземпляр уже уничтожен в начале loadGraph — создаём свежий
        this._network = new vis.Network(el, data, options);
        // после первичной стабилизации замораживаем физику — снимаем постоянную
        // нагрузку CPU (перетаскивание узлов остаётся доступным).
        this._network.once("stabilizationIterationsDone", () => {
          try { this._network.setOptions({ physics: false }); } catch (_) {}
        });

        this._network.on("click", (params) => {
          if (!params.nodes.length) return;
          const node = data.nodes.get(params.nodes[0]);
          if (node && node._post) {
            // узел-пост: открыть разбор (прежнее поведение)
            this.tab = "feed";
            this.$nextTick(() => this.openPost(node._post));
          } else if (node && node._brand) {
            // узел-контора (licensed/brand/operator): следить за конторой (Ф7)
            this.followOperator(node._brand);
          }
        });
      } catch (e) {
        this.graphLoading = false;
        this.graphError = "Не удалось построить граф: " + e.message;
      }
    },

    // ===================================================== trends
    async loadTrends() {
      this.trendsError = "";
      // превентивные рекомендации тянем параллельно — независимая панель
      this.loadRecSources();      // доступные «проблемы и источники» для селектора (один раз)
      this.loadRecommendations();
      try {
        const r = await fetch("/api/trends");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.trends = await r.json();
      } catch (e) {
        this.trendsError = "Не удалось загрузить тренды: " + e.message;
        return;
      }
      if (typeof Chart === "undefined") {
        this.trendsError = "Библиотека графиков недоступна (CDN заблокирован). Данные показаны списком брендов ниже.";
        return;
      }
      this.$nextTick(() => this.renderCharts());
    },

    // ===================================================== рекомендации АФМ
    //
    // GET /api/recommendations -> {stats:{total_posts, flagged, top_platform, top_brand},
    //   recommendations:[{title, rationale, action, priority, evidence}]}.
    // Превентивные действия по выявленным трендам; рендерятся карточками внизу
    // вкладки «Тренды». Приоритет красит бейдж (high/medium/low — палитра риска).
    // Доступные «проблемы и источники» для селектора фокуса. GET /api/recommendations/sources
    // -> {categories, brands, platforms}. Тихо обрабатываем ошибку (как loadCatalog):
    // селектор просто покажет только «Все проблемы». НЕ кидаем наверх.
    async loadRecSources() {
      if (this.recSourcesLoaded) return;
      try {
        const r = await fetch("/api/recommendations/sources");
        if (!r.ok) return; // старый бэк/404 — оставляем пустой селектор
        this.recSources = await r.json();
        this.recSourcesLoaded = true;
      } catch (_) {
        /* offline / старый бэк — селектор показывает только «Все проблемы» */
      }
    },

    // Сменить фокус рекомендаций и перезагрузить под выбор.
    setRecFocus(value) {
      this.recFocus = value;
      this.loadRecommendations();
    },

    async loadRecommendations() {
      if (this.recsLoading) return;
      this.recsLoading = true;
      this.recsError = "";
      try {
        // непустой recFocus -> ?focus=kind:value; пусто -> глобально (прежнее поведение)
        let url = "/api/recommendations";
        if (this.recFocus) url += "?focus=" + encodeURIComponent(this.recFocus);
        const r = await fetch(url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        this.recommendations = (data && data.recommendations) || [];
        this.recsStats = (data && data.stats) || null;
        this.$nextTick(() => observeReveals());
      } catch (e) {
        this.recommendations = [];
        this.recsStats = null;
        this.recsError = "Не удалось загрузить рекомендации: " + e.message;
      } finally {
        this.recsLoading = false;
      }
    },

    // ===================================================== реестр лицензий
    // Текущий эффективный реестр (что легально в РК) — GET /api/licensed.
    async loadRegistry() {
      if (this.registryLoading) return;
      this.registryLoading = true;
      this.registryError = "";
      try {
        const r = await fetch("/api/licensed");
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.registry = await r.json();
        this.$nextTick(() => observeReveals());
      } catch (e) {
        this.registry = null;
        this.registryError = "Не удалось загрузить реестр: " + e.message;
      } finally {
        this.registryLoading = false;
      }
    },

    // Пометить оператора лицензированным/нелицензированным в РК (POST /api/licensed).
    // licensed=true: «легально в РК»; false: «не легально» (отключить из реестра).
    async setOperator(name, licensed, keywords, note) {
      name = String(name || "").trim();
      if (!name || this.registrySaving) return;
      this.registrySaving = name;
      this.registryError = "";
      this.registryNote = "";
      try {
        const body = { name, licensed: !!licensed };
        if (Array.isArray(keywords) && keywords.length) body.keywords = keywords;
        if (note) body.note = note;
        const r = await fetch("/api/licensed", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j && j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.registry = await r.json();
        this.registryNote = licensed
          ? `«${name}» помечен лицензированным в РК`
          : `«${name}» помечен НЕлицензированным`;
        this.$nextTick(() => observeReveals());
        setTimeout(() => { this.registryNote = ""; }, 4000);
      } catch (e) {
        this.registryError = "Не удалось сохранить: " + e.message;
      } finally {
        this.registrySaving = "";
      }
    },

    // Добавить нового оператора из формы (имя + ключевые слова через запятую).
    async addOperator() {
      const name = String(this.newOpName || "").trim();
      if (!name) { this.registryError = "Укажите имя оператора"; return; }
      const kws = String(this.newOpKeywords || "")
        .split(",").map((s) => s.trim()).filter(Boolean);
      await this.setOperator(name, true, kws.length ? kws : [name],
        "Добавлен аналитиком как лицензированный в РК (сверять с реестром АФМ).");
      if (!this.registryError) { this.newOpName = ""; this.newOpKeywords = ""; }
    },

    // Человекочитаемая метка текущего фокуса для бейджа («фокус: …»).
    recFocusLabel() {
      if (!this.recFocus || !this.recSources) return "";
      const [kind, ...rest] = this.recFocus.split(":");
      const id = rest.join(":");
      const lists = { category: "categories", brand: "brands", platform: "platforms" };
      const arr = (this.recSources[lists[kind]] || []);
      const hit = arr.find((x) => String(x.id) === id);
      return hit ? hit.label : id;
    },

    // Палитра бейджа приоритета (зафиксированная риск-палитра).
    priorityBadge(priority) {
      const p = String(priority || "").toLowerCase();
      if (p === "high")   return "background:#FDEBEC;color:#9F2F2D";
      if (p === "medium") return "background:#FBF3DB;color:#956400";
      return "background:#EDF3EC;color:#346538"; // low / прочее
    },
    priorityLabel(priority) {
      const p = String(priority || "").toLowerCase();
      if (p === "high")   return "высокий";
      if (p === "medium") return "средний";
      return "низкий";
    },

    // Точечные рекомендации по открытому кейсу (GET /api/post -> case_recommendations).
    // Массив {title, rationale, action, priority}; пустой/отсутствует -> [].
    get caseRecs() {
      return (this.detail && Array.isArray(this.detail.case_recommendations))
        ? this.detail.case_recommendations
        : [];
    },

    // Сводка ИСТОЧНИКОВ улик по модальностям для открытого кейса: какая модальность
    // что дала (включая пустые — чтобы было видно «речи нет» vs «разбор не запускался»).
    // Подпись (текст) / Речь→текст (Whisper) / Текст с экрана (OCR) / Визуал (CLIP).
    get modalitySources() {
      const d = this.detail;
      if (!d) return [];
      const e = d.extracted || {};
      const cap = ((d.post && d.post.caption) || e.caption || "").trim();
      const vc = Array.isArray(e.visual_concepts) ? e.visual_concepts : [];
      const tr = (e.transcript || "").trim();
      const ocr = (e.ocr_text || "").trim();
      return [
        { key: "caption", label: "Подпись", icon: "ph-textbox", color: "#787774",
          present: !!cap, note: "текст поста" },
        { key: "speech", label: "Речь → текст", icon: "ph-waveform", color: "#1F6C9F",
          present: !!tr, note: "Whisper (речь из видео)" },
        { key: "screen", label: "Текст с экрана", icon: "ph-text-aa", color: "#956400",
          present: !!ocr, note: "OCR (текст на кадрах)" },
        { key: "visual", label: "Визуал", icon: "ph-eye", color: "#346538",
          present: vc.length > 0, note: vc.length ? (vc.length + " маркер(ов), CLIP") : "CLIP (образы в кадре)" },
      ];
    },

    // Правовое основание кейса (GET /api/post -> legal_basis): статьи закона РК
    // + наказание + законный путь мер. {category_ru, articles:[{code,article,
    // title,summary,punishment}], enforcement:[str], disclaimer}. Пусто -> null.
    get legalBasis() {
      const lb = this.detail && this.detail.legal_basis;
      if (!lb || !Array.isArray(lb.articles)) return null;
      // Показываем блок, только если есть статьи ИЛИ конкретные шаги мер.
      const hasArticles = lb.articles.length > 0;
      const hasSteps = Array.isArray(lb.enforcement) && lb.enforcement.length > 0;
      return (hasArticles || hasSteps) ? lb : null;
    },

    _chart(id, config) {
      const el = document.getElementById(id);
      if (!el) return;
      if (this._charts[id]) this._charts[id].destroy();
      this._charts[id] = new Chart(el, config);
    },

    renderCharts() {
      const t = this.trends;
      if (!t) return;
      const gridColor = "#EAEAEA";
      const tick = { color: "#787774", font: { family: "Geist Sans, system-ui", size: 11 } };
      const noLegend = { legend: { display: false } };

      // 1) Категории (bar)
      const cats = ["gambling", "pyramid", "fraud", "clean"];
      this._chart("chartCategory", {
        type: "bar",
        data: {
          labels: cats.map((c) => CATEGORY_LABELS[c]),
          datasets: [{
            data: cats.map((c) => (t.by_category && t.by_category[c]) || 0),
            backgroundColor: ["#9F2F2D", "#956400", "#B45309", "#346538"].map((c) => c + "DD"),
            borderRadius: 6,
            barThickness: 38,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: noLegend,
          scales: {
            x: { grid: { display: false }, ticks: tick },
            y: { beginAtZero: true, grid: { color: gridColor }, ticks: { ...tick, precision: 0 } },
          },
        },
      });

      // 2) Рекомендованные действия (3 уровня)
      const actKeys = ["auto_clear", "review", "escalate"];
      this._chart("chartAction", {
        type: "doughnut",
        data: {
          labels: actKeys.map((a) => ACTION_LABELS[a]),
          datasets: [{
            data: actKeys.map((a) => (t.by_recommended_action && t.by_recommended_action[a]) || 0),
            backgroundColor: ["#EDF3EC", "#FBF3DB", "#FDEBEC"],
            borderColor: ["#346538", "#956400", "#9F2F2D"],
            borderWidth: 1.5,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, cutout: "62%",
          plugins: { legend: { position: "bottom", labels: { ...tick, boxWidth: 12, padding: 14 } } },
        },
      });

      // 3) Платформы (donut, динамические ключи)
      const platLabels = Object.keys(t.by_platform || {});
      this._chart("chartPlatform", {
        type: "doughnut",
        data: {
          labels: platLabels,
          datasets: [{
            data: platLabels.map((p) => t.by_platform[p]),
            backgroundColor: ["#111111", "#787774", "#C9C9C7", "#9F2F2D", "#956400", "#346538"],
            borderColor: "#FFFFFF", borderWidth: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, cutout: "62%",
          plugins: { legend: { position: "bottom", labels: { ...tick, boxWidth: 12, padding: 14 } } },
        },
      });

      // 4) Гистограмма риска — ТОЧНЫЕ ключи "0-39","40-69","70-100"
      const riskKeys = ["0-39", "40-69", "70-100"];
      const rh = t.risk_histogram || {};
      this._chart("chartRisk", {
        type: "bar",
        data: {
          labels: ["низкий (0–39)", "средний (40–69)", "высокий (70–100)"],
          datasets: [{
            data: riskKeys.map((k) => rh[k] || 0),
            backgroundColor: ["#EDF3EC", "#FBF3DB", "#FDEBEC"],
            borderColor: ["#346538", "#956400", "#9F2F2D"],
            borderWidth: 1.5, borderRadius: 6, barThickness: 56,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: noLegend,
          scales: {
            x: { grid: { display: false }, ticks: tick },
            y: { beginAtZero: true, grid: { color: gridColor }, ticks: { ...tick, precision: 0 } },
          },
        },
      });
    },

    brandPct(count) {
      const t = this.trends;
      if (!t || !t.top_brands || !t.top_brands.length) return 0;
      const max = Math.max(...t.top_brands.map((b) => b.count), 1);
      return Math.round((count / max) * 100);
    },

    // ===================================================== live check (async)
    //
    // /api/analyze is asynchronous: POST returns {job_id, status:"queued"}.
    // We then poll GET /api/jobs/{job_id} every ~700ms reading
    // {status, stage, progress, result, error} until status is 'done' or 'error'.
    // While running we surface the RU `stage` text + `progress` percent in a bar.
    async analyze() {
      if (this.liveLoading) return;
      if (!this.liveUrl && !this.liveFile) {
        this.liveError = "Вставьте ссылку или выберите файл.";
        return;
      }
      this._stopJobPoll();
      this.liveLoading = true;
      this.liveError = "";
      this.liveResult = null;
      this.liveStage = "постановка в очередь";
      this.liveProgress = 0;
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
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        const start = await r.json();
        if (!start.job_id) {
          // Backend may still answer synchronously with a verdict — accept it.
          if (start.score) {
            this.liveResult = start;
            this.liveLoading = false;
            this.$nextTick(() => observeReveals());
            return;
          }
          throw new Error("сервер не вернул идентификатор задачи");
        }
        this._jobId = start.job_id;
        this._pollJob();
      } catch (e) {
        this.liveError = "Проверка не удалась: " + e.message;
        this.liveLoading = false;
      }
    },

    // Poll the job until terminal; ~700ms cadence.
    _pollJob() {
      this._jobTimer = setInterval(async () => {
        if (!this._jobId) return;
        try {
          const r = await fetch("/api/jobs/" + encodeURIComponent(this._jobId));
          if (r.status === 404) {
            // задача неизвестна (сервер перезапущен) — не зависаем на ошибке поллинга
            this._stopJobPoll();
            this.liveLoading = false;
            this.liveError = "Проверка прервана (сервер перезапущён). Запустите анализ ссылки заново.";
            return;
          }
          if (!r.ok) throw new Error("HTTP " + r.status);
          const job = await r.json();
          this.liveStage = job.stage || this.liveStage || "обработка";
          if (typeof job.progress === "number") this.liveProgress = job.progress;

          if (job.status === "done") {
            this._stopJobPoll();
            if (job.result && job.result.score) {
              this.liveResult = job.result;
            } else {
              this.liveError = "Анализ завершён, но результат пуст.";
            }
            this.liveLoading = false;
            this.$nextTick(() => observeReveals());
          } else if (job.status === "error") {
            this._stopJobPoll();
            this.liveError = "Анализ не удался: " + (job.error || "неизвестная ошибка");
            this.liveLoading = false;
          }
        } catch (e) {
          // transient fetch failure: surface but keep the run alive for a retry tick
          this.liveError = "Потеряна связь с задачей: " + e.message;
        }
      }, 700);
    },

    _stopJobPoll() {
      if (this._jobTimer) { clearInterval(this._jobTimer); this._jobTimer = null; }
      this._jobId = null;
    },

    // ===================================================== telegram scanner
    // POST /api/scan/telegram {channels:[...]} (comma-separated input -> array).
    // Response {added, flagged, channels:[{channel,fetched,added,flagged}]}.
    async scanTelegram() {
      if (this.scanning) return;
      const channels = this.scanInput
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (!channels.length) {
        this.scanError = "Укажите канал, например @durov или t.me/durov.";
        return;
      }
      this.scanning = true;
      this.scanError = "";
      this.scanResult = null;
      try {
        const r = await fetch("/api/scan/telegram", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channels }),
        });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.scanResult = await r.json();
        await this.loadFeed(); // feed now contains the real scanned posts
      } catch (e) {
        this.scanError = "Сканирование не удалось: " + e.message;
      } finally {
        this.scanning = false;
      }
    },

    // ===================================================== watchlist
    async loadWatchlist() {
      this.watchLoading = true;
      this.watchError = "";
      try {
        const r = await fetch("/api/watchlist");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        this.watchlist = (data && data.channels) || [];
      } catch (e) {
        this.watchError = "Не удалось загрузить список наблюдения: " + e.message;
      } finally {
        this.watchLoading = false;
      }
    },

    async addWatch() {
      const channel = (this.watchInput || "").trim();
      if (!channel) {
        this.watchError = "Укажите канал для добавления.";
        return;
      }
      if (this.watchAdding) return;
      this.watchAdding = true;
      this.watchError = "";
      try {
        const r = await fetch("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ channel }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        this.watchlist = (data && data.channels) || [];
        this.watchInput = "";
        this.loadWatchStats(); // обновить дашборд после добавления
      } catch (e) {
        this.watchError = "Не удалось добавить канал: " + e.message;
      } finally {
        this.watchAdding = false;
      }
    },

    async removeWatch(channel) {
      this.watchError = "";
      try {
        const r = await fetch("/api/watchlist/" + encodeURIComponent(channel), {
          method: "DELETE",
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        this.watchlist = (data && data.channels) || [];
        this.loadWatchStats(); // обновить дашборд после удаления
      } catch (e) {
        this.watchError = "Не удалось удалить канал: " + e.message;
      }
    },

    async scanWatchlist() {
      if (this.watchScanning) return;
      this.watchScanning = true;
      this.watchError = "";
      this.watchScanResult = null;
      try {
        const r = await fetch("/api/watchlist/scan", { method: "POST" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.watchScanResult = await r.json();
        await this.loadFeed();        // показать свежесканированные посты в ленте
        await this.loadWatchStats();  // пересчитать статистику telegram-дашборда
        await this.loadWatchEntries(); // и обновить мультиплатформенную таблицу (Ф6)
      } catch (e) {
        this.watchError = "Сканирование списка не удалось: " + e.message;
      } finally {
        this.watchScanning = false;
      }
    },

    // ===================================================== мультиплатформенный мониторинг (Ф6)
    //
    // GET /api/watchlist/entries -> {entries:[...], watched:[...]}. entries несут
    // статистику по каждой площадке/конторе (telegram|youtube|tiktok|twitch|kick|
    // instagram|operator). Это ОСНОВНОЙ дашборд вкладки «Мониторинг».
    // Открыть детальную страницу (модалку) записи мониторинга: все посты этой
    // конторы/канала + статистика (GET /api/monitor/entry) + решения по ней
    // (GET /api/recommendations?focus=brand:<target>). Клик по посту -> разбор.
    async openMonitorEntry(entry) {
      if (!entry || !entry.target) return;
      this.monitorTarget = entry.target;
      this.monitorDetail = null;
      this.monitorRecs = [];
      this.monitorDetailError = "";
      this.monitorDetailLoading = true;
      try {
        const qs = new URLSearchParams({ target: entry.target, platform: entry.platform || "all" });
        const r = await fetch("/api/monitor/entry?" + qs.toString());
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.monitorDetail = await r.json();
      } catch (e) {
        this.monitorDetailError = "Не удалось загрузить статистику: " + e.message;
        this.monitorDetail = {
          target: entry.target, platform: entry.platform, licensed: false,
          stats: { total: 0, flagged: 0, avg_risk: 0, by_category: {}, by_action: {} },
          posts: [],
        };
      } finally {
        this.monitorDetailLoading = false;
      }
      // решения по этой конторе/бренду (best-effort; «нет данных»-заглушки прячем)
      try {
        const rr = await fetch("/api/recommendations?focus=" + encodeURIComponent("brand:" + entry.target));
        if (rr.ok) {
          const data = await rr.json();
          this.monitorRecs = (data.recommendations || [])
            .filter((x) => !/нет данных/i.test(x.title || ""))
            .slice(0, 4);
        }
      } catch (e) { /* решения — необязательны */ }
    },

    closeMonitorEntry() {
      this.monitorDetail = null;
      this.monitorDetailLoading = false;
      this.monitorRecs = [];
    },

    // Открыть разбор поста из списка модалки: панель разбора живёт на вкладке
    // «Лента», поэтому закрываем модалку, переключаем вкладку и только потом
    // зовём openPost (иначе detail ставится, но не виден на вкладке «Мониторинг»).
    openPostFromList(id) {
      this.closeMonitorEntry();
      this.tab = "feed";
      this.$nextTick(() => this.openPost(id));
    },

    async loadWatchEntries() {
      this.watchEntriesLoading = true;
      this.watchEntriesError = "";
      try {
        const r = await fetch("/api/watchlist/entries");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        // Список = ВСЁ под наблюдением (data.watched), а статистика (data.entries)
        // накладывается сверху по ключу platform:target. Иначе только что добавленная
        // контора/канал (ещё не сканированы -> нет в entries) не показалась бы в списке.
        const entries = (data && data.entries) || [];
        const watched = (data && data.watched) || [];
        const byKey = {};
        for (const e of entries) {
          byKey[e.platform + ":" + String(e.target).toLowerCase()] = e;
        }
        const merged = watched.map((w) => byKey[w.platform + ":" + String(w.target).toLowerCase()] || {
          target: w.target, platform: w.platform,
          last_scan: null, last_added: 0, last_flagged: 0, total_collected: 0, licensed: null,
        });
        this.watchEntries = merged.length ? merged : entries;
        this.watchWatched = watched;
        this.$nextTick(() => observeReveals());
      } catch (e) {
        this.watchEntriesError = "Не удалось загрузить мониторинг: " + e.message;
      } finally {
        this.watchEntriesLoading = false;
      }
    },

    // Добавить площадку/контору: target из watchInput + выбранная watchPlatform.
    // POST /api/watchlist {target, platform}. Переиспользуем флаг watchAdding,
    // чтобы кнопка не срабатывала дважды.
    async addWatchEntry() {
      const target = (this.watchInput || "").trim();
      if (!target) {
        this.watchEntriesError = "Укажите аккаунт, ссылку или название конторы.";
        return;
      }
      if (this.watchAdding) return;
      this.watchAdding = true;
      this.watchEntriesError = "";
      const platform = this.watchPlatform || "telegram";
      try {
        const r = await fetch("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, platform }),
        });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.watchInput = "";
        await this.loadWatchEntries();
        if (platform === "telegram") this.loadWatchlist(); // освежить старый telegram-список
      } catch (e) {
        this.watchEntriesError = "Не удалось добавить в мониторинг: " + e.message;
      } finally {
        this.watchAdding = false;
      }
    },

    // Убрать запись из мониторинга. DELETE /api/watchlist/{target}?platform=<p>.
    async removeWatchEntry(target, platform) {
      this.watchEntriesError = "";
      try {
        const r = await fetch(
          "/api/watchlist/" + encodeURIComponent(target) +
            "?platform=" + encodeURIComponent(platform || "telegram"),
          { method: "DELETE" }
        );
        if (!r.ok) throw new Error("HTTP " + r.status);
        await this.loadWatchEntries();
        if ((platform || "telegram") === "telegram") this.loadWatchlist();
      } catch (e) {
        this.watchEntriesError = "Не удалось убрать из мониторинга: " + e.message;
      }
    },

    // ===================================================== «Следить за конторой» (Ф7)
    //
    // Добавляет контору/бренд в мониторинг как оператора. POST /api/watchlist
    // {target: brand, platform:"operator"}. Подтверждение — ненавязчивый зелёный
    // toast (watchFollowNote), который гаснет через ~4 сек.
    async followOperator(brand) {
      const target = String(brand || "").trim();
      if (!target) return;
      if (this.followSaving) return;
      this.followSaving = true;
      this.watchFollowNote = "";
      if (this._followTimer) { clearTimeout(this._followTimer); this._followTimer = null; }
      try {
        const r = await fetch("/api/watchlist", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target, platform: "operator" }),
        });
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.watchFollowNote = "Контора «" + target + "» добавлена в мониторинг";
        // если открыта вкладка мониторинга — освежим таблицу
        if (this.tab === "watch") this.loadWatchEntries();
      } catch (e) {
        this.watchFollowNote = "Не удалось добавить контору: " + e.message;
      } finally {
        this.followSaving = false;
        this._followTimer = setTimeout(() => { this.watchFollowNote = ""; }, 4000);
      }
    },

    // Узел-сущность относится к КОНТОРЕ (оператору/бренду), за которым можно следить.
    // Бренды извлекаются как сущности типов casino_brand/betting_brand (см.
    // BRAND_ENTITY_TYPES в бэкенде) — ВНЕ зависимости от лицензии: аналитику нужно
    // мониторить в первую очередь НЕлегальные конторы (1xbet/mostbet). licensed-узлы
    // тоже считаем конторой (зелёный сигнал из build.py).
    isOperatorNode(n) {
      if (!n || n.type === "post") return false;
      const t = String(n.type || "").toLowerCase();
      return !!n.licensed || t.includes("brand") || t.includes("operator")
        || t === "casino_brand" || t === "betting_brand";
    },

    // Узлы графа/карточки, относящиеся к конторе. Возвращает массив подписей брендов.
    operatorNodesFromGraph(graph) {
      const out = [];
      const nodes = (graph && graph.nodes) || [];
      for (const n of nodes) {
        if (this.isOperatorNode(n) && n.label) out.push(String(n.label));
      }
      return out;
    },

    // Список «контор» открытого материала для кнопок «Следить»: licensed_operators
    // поста + бренд/оператор-узлы из эго-графа карточки. Дедуп по нижнему регистру.
    get followOperatorTargets() {
      if (!this.detail) return [];
      const seen = new Set();
      const out = [];
      const push = (v) => {
        const s = String(v || "").trim();
        if (!s) return;
        const k = s.toLowerCase();
        if (seen.has(k)) return;
        seen.add(k);
        out.push(s);
      };
      const ops = (this.detail.post && this.detail.post.licensed_operators) || [];
      ops.forEach(push);
      this.operatorNodesFromGraph(this.detail.graph).forEach(push);
      return out;
    },

    // Дашборд мониторинга: первичный источник — GET /api/watchlist/stats,
    // который отдаёт per-channel {channel,last_scan,last_added,last_flagged,total_collected}.
    // Если статистики ещё нет (канал ни разу не сканировался), дополняем агрегатом
    // по реальной ленте, чтобы «собрано/флагнуто» не висели на нуле без причины.
    async loadWatchStats() {
      this.watchStatsLoading = true;
      try {
        // нормализованный индекс @channel(lower) -> исходное имя из watchlist
        const byLower = {};
        for (const ch of this.watchlist) {
          byLower[("@" + String(ch).replace(/^@/, "")).toLowerCase()] = ch;
        }
        const stats = {};
        for (const ch of this.watchlist) {
          stats[ch] = { collected: 0, flagged: 0, lastScan: null, lastFlagged: 0 };
        }

        // 1) официальная статистика бэкенда
        let haveStats = false;
        try {
          const sr = await fetch("/api/watchlist/stats");
          if (sr.ok) {
            const sj = await sr.json();
            const chans = (sj && sj.channels) || [];
            for (const c of chans) {
              const key = ("@" + String(c.channel || "").replace(/^@/, "")).toLowerCase();
              const ch = byLower[key] || c.channel;
              if (!ch) continue;
              if (!stats[ch]) stats[ch] = { collected: 0, flagged: 0, lastScan: null, lastFlagged: 0 };
              stats[ch].collected = Number(c.total_collected) || 0;
              stats[ch].flagged = Number(c.last_flagged) || 0;
              stats[ch].lastFlagged = Number(c.last_flagged) || 0;
              stats[ch].lastScan = c.last_scan || null;
              haveStats = true;
            }
          }
        } catch (_) { /* падаем на агрегат ленты ниже */ }

        // 2) дополнение/резерв: агрегат по реальной ленте для каналов без статистики
        try {
          const fr = await fetch("/api/feed?real_only=1&limit=500");
          if (fr.ok) {
            const rows = await fr.json();
            const agg = {};
            for (const ch of this.watchlist) agg[ch] = { collected: 0, flagged: 0 };
            for (const row of rows) {
              const handle = String((row.post && row.post.author_handle) || "").toLowerCase();
              const ch = byLower[handle];
              if (!ch) continue;
              agg[ch].collected += 1;
              if ((row.score && row.score.risk) >= 70) agg[ch].flagged += 1;
            }
            for (const ch of this.watchlist) {
              // официальный «собрано» в приоритете; иначе — счёт из ленты
              if (!haveStats || !stats[ch] || stats[ch].collected === 0) {
                stats[ch].collected = agg[ch].collected;
              }
              if (!stats[ch].flagged) stats[ch].flagged = agg[ch].flagged;
            }
          }
        } catch (_) { /* лента недоступна — оставляем то, что есть */ }

        this.watchStats = stats;
      } catch (e) {
        // дашборд не критичен — молча оставляем нули, ошибку показывает основной блок
        this.watchStats = {};
      } finally {
        this.watchStatsLoading = false;
      }
    },

    watchStat(channel) {
      return this.watchStats[channel] || { collected: 0, flagged: 0, lastScan: null, lastFlagged: 0 };
    },

    // ===================================================== view helpers
    riskBadge(risk) {
      const p = RISK_PALETTE[riskTier(risk || 0)];
      return `background:${p.bg};color:${p.tx}`;
    },
    // Тонкий риск-метр рядом с моно-числом: ширина = риск, цвет = тон тира.
    riskMeterStyle(risk) {
      const r = Math.max(0, Math.min(100, Number(risk) || 0));
      const p = RISK_PALETTE[riskTier(r)];
      return `width:${r}%;background:${p.tx}`;
    },
    // Тир риска как метод объекта (для шаблонов): escalate/review/clean.
    riskTier(risk) { return riskTier(risk); },
    categoryLabel(c) { return CATEGORY_LABELS[c] || c; },
    actionLabel(a) { return ACTION_LABELS[a] || a; },
    // Компактное число просмотров: 1 234 → «1.2K», 3 400 000 → «3.4M».
    formatViews(n) {
      const v = Number(n) || 0;
      if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
      if (v >= 1e3) return (v / 1e3).toFixed(v >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "K";
      return String(v);
    },
    featureLabel(f) { return FEATURE_LABELS[f] || f; },
    platformIcon(p) { return PLATFORM_ICONS[(p || "").toLowerCase()] || "ph-globe"; },
    platformLabel(id) {
      const m = (this.discoverPlatforms || []).find((x) => x.id === id);
      return m ? m.label : "Все";
    },

    // Короткое относительное время от ISO/epoch до «сейчас» (для «последний скан»).
    relTime(ts) {
      if (!ts && ts !== 0) return "—";
      let t;
      if (typeof ts === "number") t = ts < 1e12 ? ts * 1000 : ts;
      else { t = Date.parse(ts); if (Number.isNaN(t)) return "—"; }
      const diff = Math.max(0, Date.now() - t);
      const s = Math.round(diff / 1000);
      if (s < 45) return "только что";
      const m = Math.round(s / 60);
      if (m < 60) return m + " мин назад";
      const h = Math.round(m / 60);
      if (h < 24) return h + " ч назад";
      const d = Math.round(h / 24);
      if (d < 7) return d + " дн назад";
      const w = Math.round(d / 7);
      if (w < 5) return w + " нед назад";
      return Math.round(d / 30) + " мес назад";
    },

    // Происхождение поста (источник) -> человекочитаемая метка для бейджа.
    sourceLabel(s) { return SOURCE_LABELS[(s || "").toLowerCase()] || "источник"; },
    sourceIcon(s) { return SOURCE_ICONS[(s || "").toLowerCase()] || "ph-circle"; },

    // Пост идёт прямым эфиром (флаг post.live от бэка) — рисуем бейдж «прямой эфир».
    isLive(post) { return !!(post && post.live); },

    // Есть ли у поста реальная ссылка http(s) для кликабельного перехода.
    hasRealUrl(post) {
      return !!(post && post.url && /^https?:\/\//i.test(post.url));
    },
    // Есть ли что показать в медиа-области (видео-файл, превью-картинка или ссылка).
    hasAnyMedia(post) {
      return !!(post && (post.media_path || post.thumb_url || this.hasRealUrl(post)));
    },

    mediaSrc(path) {
      if (!path) return "";
      if (/^https?:\/\//i.test(path)) return path;
      // локальный файл media — отдаётся статикой; нормализуем слеши
      const norm = String(path).replace(/\\/g, "/");
      const idx = norm.lastIndexOf("/data/media/");
      return idx >= 0 ? norm.slice(idx) : norm;
    },

    // Подсветка триггер-улик (evidence) в тексте; безопасный HTML.
    highlight(text, features) {
      let out = escapeHtml(text);
      const evid = (features || [])
        .map((f) => (f.evidence || "").trim())
        .filter((e) => e.length >= 2);
      // длинные сначала, чтобы вложенные не ломали разметку
      evid.sort((a, b) => b.length - a.length);
      const done = new Set();
      for (const ev of evid) {
        const key = ev.toLowerCase();
        if (done.has(key)) continue;
        done.add(key);
        const safe = escapeHtml(ev).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        out = out.replace(new RegExp(safe, "gi"), (m) => `<mark>${m}</mark>`);
      }
      return out;
    },
  };
}

// экспонируем для Alpine (defer-загрузка)
window.kozApp = kozApp;
