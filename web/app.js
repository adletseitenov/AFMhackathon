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
  link: "ph-link",
  upload: "ph-upload-simple",
};

// Происхождение поста (post.source): seed | live | discovered | telegram.
// «live» — выгрузка реального канала; «discovered» — авто-поиск по YouTube.
const SOURCE_LABELS = {
  seed: "демо",
  live: "загрузка",
  discovered: "YouTube-поиск",
  telegram: "Telegram",
};
const SOURCE_ICONS = {
  seed: "ph-flask",
  live: "ph-download-simple",
  discovered: "ph-youtube-logo",
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
    ],

    // --- top bar ---
    macroF1: null,

    // --- feed / queue ---
    feed: [],
    feedLoading: true,
    feedError: "",
    categoryFilter: "",
    platformFilter: "",
    realOnly: true,          // ЛЕНТА defaults to real posts only (real_only=1)
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
    ],

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

    // --- drill-down ---
    detail: null,
    detailLoading: false,
    selectedId: null,

    // --- graph ---
    graphMinRisk: 0,
    graphError: "",
    graphLoading: false,
    graphEmpty: false,
    _network: null,

    // --- trends ---
    trends: null,
    trendsError: "",
    _charts: {},
    _trendsLoaded: false,

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
      this.loadFeed();
      this._pollTimer = setInterval(() => {
        if (this.tab === "feed") this.loadFeed();
      }, 2000);
    },

    setTab(id) {
      this.tab = id;
      // Граф: ВСЕГДА перестраиваем из свежих данных при входе во вкладку
      // (чтобы отразить вновь найденные/просканированные посты).
      if (id === "graph") this.$nextTick(() => this.loadGraph());
      if (id === "trends") this.$nextTick(() => this.loadTrends());
      if (id === "watch") {
        this.loadWatchlist();
        this.loadWatchStats();
      }
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

    // ===================================================== feed
    async loadFeed() {
      let url = "/api/feed?limit=100";
      if (this.realOnly) url += "&real_only=1";   // по умолчанию — только реальные посты
      if (this.categoryFilter) url += "&category=" + encodeURIComponent(this.categoryFilter);
      if (this.platformFilter) url += "&platform=" + encodeURIComponent(this.platformFilter);
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
      try {
        const r = await fetch("/api/post/" + encodeURIComponent(id));
        if (!r.ok) throw new Error("HTTP " + r.status);
        this.detail = await r.json();
      } catch (e) {
        this.feedError = "Не удалось открыть материал: " + e.message;
      } finally {
        this.detailLoading = false;
        this.$nextTick(() => observeReveals());
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

        const nodes = g.nodes.map((n) => {
          const isPost = n.type === "post";
          const pal = RISK_PALETTE[riskTier(n.risk || 0)];
          return {
            id: n.id,
            label: n.label,
            shape: isPost ? "dot" : "box",
            size: isPost ? 12 + Math.round((n.risk || 0) / 6) : 10,
            color: isPost
              ? { background: pal.net, border: pal.tx, highlight: { background: pal.net, border: "#111111" } }
              : { background: "#FFFFFF", border: "#C9C9C7", highlight: { background: "#F1F1EF", border: "#787774" } },
            font: { color: isPost ? "#111111" : "#787774", size: 12, face: "Geist Sans, system-ui, sans-serif" },
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
          physics: { stabilization: true, barnesHut: { gravitationalConstant: -3500, springLength: 120 } },
          nodes: { borderWidth: 1.5 },
        };
        // экземпляр уже уничтожен в начале loadGraph — создаём свежий
        this._network = new vis.Network(el, data, options);

        this._network.on("click", (params) => {
          if (!params.nodes.length) return;
          const node = data.nodes.get(params.nodes[0]);
          if (node && node._post) {
            this.tab = "feed";
            this.$nextTick(() => this.openPost(node._post));
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
        await this.loadFeed();      // показать свежесканированные посты в ленте
        await this.loadWatchStats(); // и пересчитать статистику дашборда
      } catch (e) {
        this.watchError = "Сканирование списка не удалось: " + e.message;
      } finally {
        this.watchScanning = false;
      }
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
    categoryLabel(c) { return CATEGORY_LABELS[c] || c; },
    actionLabel(a) { return ACTION_LABELS[a] || a; },
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
