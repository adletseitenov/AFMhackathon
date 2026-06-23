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
      { id: "live", label: "Живая проверка", icon: "ph-shield-check" },
    ],

    // --- top bar ---
    macroF1: null,

    // --- feed / queue ---
    feed: [],
    feedLoading: true,
    feedError: "",
    categoryFilter: "",
    ticking: false,
    _knownIds: new Set(),
    _newIds: new Set(),
    _pollTimer: null,

    // --- drill-down ---
    detail: null,
    detailLoading: false,
    selectedId: null,

    // --- graph ---
    graphMinRisk: 0,
    graphError: "",
    _network: null,
    _graphLoaded: false,

    // --- trends ---
    trends: null,
    trendsError: "",
    _charts: {},
    _trendsLoaded: false,

    // --- live check ---
    liveUrl: "",
    liveFile: null,
    liveResult: null,
    liveLoading: false,
    liveError: "",

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
      if (id === "graph") this.$nextTick(() => this.loadGraph(true));
      if (id === "trends") this.$nextTick(() => this.loadTrends());
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
      if (this.categoryFilter) url += "&category=" + encodeURIComponent(this.categoryFilter);
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

    async tick() {
      if (this.ticking) return;
      this.ticking = true;
      try {
        await fetch("/api/tick", { method: "POST" });
        await this.loadFeed();
      } catch (e) {
        this.feedError = "Тик не выполнен: " + e.message;
      } finally {
        this.ticking = false;
      }
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
    async loadGraph(force) {
      if (this._graphLoaded && !force) return;
      this.graphError = "";
      const el = document.getElementById("graph");
      if (!el) return;

      if (typeof vis === "undefined" || !vis.Network) {
        this.graphError = "Библиотека графа недоступна (CDN заблокирован).";
        return;
      }
      try {
        const r = await fetch("/api/graph?min_risk=" + this.graphMinRisk);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const g = await r.json();
        this._graphLoaded = true;

        if (!g.nodes || !g.nodes.length) {
          if (this._network) { this._network.destroy(); this._network = null; }
          el.innerHTML =
            '<div class="h-full grid place-items-center text-muted text-[13px]">Нет связей для отображения при заданном пороге риска.</div>';
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
        if (this._network) this._network.destroy();
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

    // ===================================================== live check
    async analyze() {
      if (this.liveLoading) return;
      if (!this.liveUrl && !this.liveFile) {
        this.liveError = "Вставьте ссылку или выберите файл.";
        return;
      }
      this.liveLoading = true;
      this.liveError = "";
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
        if (!r.ok) {
          let msg = "HTTP " + r.status;
          try { const j = await r.json(); if (j.detail) msg = j.detail; } catch (_) {}
          throw new Error(msg);
        }
        this.liveResult = await r.json();
        this.$nextTick(() => observeReveals());
      } catch (e) {
        this.liveError = "Проверка не удалась: " + e.message;
      } finally {
        this.liveLoading = false;
      }
    },

    // ===================================================== view helpers
    riskBadge(risk) {
      const p = RISK_PALETTE[riskTier(risk || 0)];
      return `background:${p.bg};color:${p.tx}`;
    },
    categoryLabel(c) { return CATEGORY_LABELS[c] || c; },
    actionLabel(a) { return ACTION_LABELS[a] || a; },
    featureLabel(f) { return FEATURE_LABELS[f] || f; },
    platformIcon(p) { return PLATFORM_ICONS[(p || "").toLowerCase()] || "ph-globe"; },

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
