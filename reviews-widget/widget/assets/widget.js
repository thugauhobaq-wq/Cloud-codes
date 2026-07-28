/*
 * Виджет отзывов — то, что клиент вставляет на сайт одной строчкой.
 *
 * Правила, из которых он собран:
 *   1. Ничего не ломать на чужой странице. Всё живёт в Shadow DOM,
 *      глобальных переменных не оставляем, при любой ошибке молча гаснем.
 *   2. Никаких зависимостей и сборки — файл отдаётся как есть.
 *   3. Никакого innerHTML для данных: текст отзыва вставляется только
 *      через textContent, иначе первый же отзыв со скриптом станет XSS.
 */
(function () {
  "use strict";

  var DEFAULTS = {
    title: "Отзывы клиентов",
    theme: "auto",
    accent: "#2f6df6",
    layout: "carousel",
    limit: 20,
    min_rating: 1,
    autoplay: true,
    interval: 6000,
    show_summary: true,
    show_form_button: true,
    form_button_text: "Оставить отзыв",
    card_min_width: 300,
    radius: 16,
    seo_schema: false
  };

  var BOOLS = ["autoplay", "show_summary", "show_form_button", "seo_schema"];
  var NUMBERS = ["limit", "min_rating", "interval", "card_min_width", "radius"];
  var MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
                "июля", "августа", "сентября", "октября", "ноября", "декабря"];
  var SOURCE_TITLES = { form: "с сайта", telegram: "из Telegram", import: "", demo: "" };
  var MAX_PER_VIEW = 3;

  /* Конфиг подставляет сервер в /w/<ключ>.js. Для generic-варианта
     (/embed.js) он пустой, и ключ берётся из data-site у тега script. */
  var BAKED = window.__REVIEWS_WIDGET__ || {};
  try { delete window.__REVIEWS_WIDGET__; } catch (e) { window.__REVIEWS_WIDGET__ = null; }

  var CURRENT = document.currentScript || findSelf();
  var reducedMotion = false;
  try {
    reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (e) { /* старые браузеры */ }

  /* ------------------------------------------------------------- утилиты */

  function findSelf() {
    // Запасной вариант для браузеров без document.currentScript.
    var scripts = document.getElementsByTagName("script");
    for (var i = scripts.length - 1; i >= 0; i--) {
      if (/\/(embed|w\/[^/]+)\.js/.test(scripts[i].src || "")) return scripts[i];
    }
    return null;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function merge() {
    var out = {};
    for (var i = 0; i < arguments.length; i++) {
      var source = arguments[i] || {};
      for (var key in source) {
        if (Object.prototype.hasOwnProperty.call(source, key) && source[key] != null) {
          out[key] = source[key];
        }
      }
    }
    return out;
  }

  function coerce(settings) {
    var out = merge(DEFAULTS, settings);
    BOOLS.forEach(function (key) {
      var value = out[key];
      out[key] = !(value === false || value === "false" || value === "0" || value === 0);
    });
    NUMBERS.forEach(function (key) {
      var value = parseInt(out[key], 10);
      out[key] = isNaN(value) ? DEFAULTS[key] : value;
    });
    if (["auto", "light", "dark"].indexOf(out.theme) < 0) out.theme = "auto";
    if (["carousel", "grid"].indexOf(out.layout) < 0) out.layout = "carousel";
    if (!/^#[0-9a-f]{6}$/i.test(out.accent)) out.accent = DEFAULTS.accent;
    return out;
  }

  function fromDataset(node) {
    // data-theme="dark" → { theme: "dark" }, data-card-min-width → card_min_width
    var out = {};
    if (!node || !node.attributes) return out;
    for (var i = 0; i < node.attributes.length; i++) {
      var attr = node.attributes[i];
      if (attr.name.indexOf("data-") !== 0) continue;
      var key = attr.name.slice(5).replace(/-/g, "_");
      if (key === "site" || key === "reviews_widget" || key === "api") continue;
      if (Object.prototype.hasOwnProperty.call(DEFAULTS, key)) out[key] = attr.value;
    }
    return out;
  }

  function apiBase() {
    if (BAKED.api) return BAKED.api;
    var src = (CURRENT && CURRENT.src) || "";
    if (!src) return "";
    var url = src.replace(/\/(embed\.js|w\/[^/]+\.js)(\?.*)?$/, "");
    return url === src ? "" : url;
  }

  function request(url, done) {
    try {
      var xhr = new XMLHttpRequest();
      xhr.open("GET", url, true);
      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (xhr.status >= 200 && xhr.status < 300) {
          try { done(null, JSON.parse(xhr.responseText)); }
          catch (e) { done(e); }
        } else {
          done(new Error("HTTP " + xhr.status));
        }
      };
      xhr.onerror = function () { done(new Error("network")); };
      xhr.send();
    } catch (e) {
      done(e);
    }
  }

  function formatDate(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return "";
    var days = Math.floor((Date.now() - date.getTime()) / 86400000);
    if (days <= 0) return "сегодня";
    if (days === 1) return "вчера";
    if (days < 7) return days + " дн. назад";
    if (days < 31) {
      var weeks = Math.floor(days / 7);
      return weeks + (weeks === 1 ? " неделю назад" : weeks < 5 ? " недели назад" : " недель назад");
    }
    var text = date.getDate() + " " + MONTHS[date.getMonth()];
    if (date.getFullYear() !== new Date().getFullYear()) text += " " + date.getFullYear();
    return text;
  }

  function avatarHue(name) {
    var hash = 0;
    for (var i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) % 360;
    return hash;
  }

  function stars(value, size) {
    var wrap = el("span", "rw-stars");
    wrap.setAttribute("aria-label", "Оценка " + value + " из 5");
    for (var i = 1; i <= 5; i++) {
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 20 20");
      svg.setAttribute("width", size || 16);
      svg.setAttribute("height", size || 16);
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("class", i <= Math.round(value) ? "rw-star rw-star-on" : "rw-star");
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", "M10 1.6l2.5 5.1 5.6.8-4 3.9 1 5.6-5.1-2.7-5 2.7 1-5.6-4.1-3.9 5.6-.8z");
      svg.appendChild(path);
      wrap.appendChild(svg);
    }
    return wrap;
  }

  /* --------------------------------------------------------------- стили */

  function css(settings) {
    return [
      // !important здесь не перестраховка: по спецификации важные объявления
      // из shadow-дерева перебивают важные объявления страницы, и только так
      // виджет защищён от сайтов с глобальным `* { ... !important }`.
      // contain:layout тут нельзя — он сделал бы :host containing block
      // для position:fixed, и модалка с формой перестала бы быть модалкой.
      ":host{all:initial!important;display:block!important}",
      ".rw{",
      "--rw-accent:", settings.accent, ";",
      "--rw-radius:", settings.radius, "px;",
      "--rw-gap:16px;--rw-card-min:", settings.card_min_width, "px;",
      "--rw-bg:transparent;--rw-fg:#151922;--rw-muted:#6b7280;--rw-card:#fff;",
      "--rw-border:#e6e8ee;--rw-shadow:0 1px 2px rgba(16,24,40,.05),0 8px 24px rgba(16,24,40,.06);",
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;",
      "font-size:15px;line-height:1.55;color:var(--rw-fg);background:var(--rw-bg);",
      "-webkit-font-smoothing:antialiased;box-sizing:border-box;position:relative;display:block}",
      ".rw *,.rw *:before,.rw *:after{box-sizing:border-box}",
      ".rw-dark{--rw-fg:#e8eaf0;--rw-muted:#98a1b3;--rw-card:#171a21;--rw-border:#272b36;",
      "--rw-shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35)}",
      "@media (prefers-color-scheme:dark){.rw-auto{--rw-fg:#e8eaf0;--rw-muted:#98a1b3;",
      "--rw-card:#171a21;--rw-border:#272b36;",
      "--rw-shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35)}}",

      ".rw-head{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:0 0 14px}",
      ".rw-title{font-size:20px;font-weight:650;margin:0;letter-spacing:-.01em}",
      ".rw-summary{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--rw-muted)}",
      ".rw-avg{font-weight:700;font-size:16px;color:var(--rw-fg)}",
      ".rw-spacer{flex:1 1 auto}",
      ".rw-stars{display:inline-flex;gap:2px;line-height:0;vertical-align:middle}",
      ".rw-star{fill:none;stroke:currentColor;stroke-width:1.5;color:#c9cdd8}",
      ".rw-star-on{fill:#f5b544;stroke:none;color:#f5b544}",

      ".rw-btn{appearance:none;border:1px solid var(--rw-border);background:var(--rw-card);",
      "color:var(--rw-fg);font:inherit;font-size:14px;font-weight:550;padding:9px 16px;",
      "border-radius:999px;cursor:pointer;transition:.15s ease;white-space:nowrap}",
      ".rw-btn:hover{border-color:var(--rw-accent);color:var(--rw-accent)}",
      ".rw-btn-primary{background:var(--rw-accent);border-color:var(--rw-accent);color:#fff}",
      ".rw-btn-primary:hover{color:#fff;filter:brightness(1.08)}",
      ".rw :focus-visible{outline:2px solid var(--rw-accent);outline-offset:2px}",

      ".rw-viewport{position:relative}",
      ".rw-track{display:flex;gap:var(--rw-gap);overflow-x:auto;overflow-y:hidden;",
      "scroll-snap-type:x mandatory;scrollbar-width:none;-ms-overflow-style:none;",
      "padding:2px 2px 6px;margin:-2px -2px -6px;align-items:stretch}",
      ".rw-track::-webkit-scrollbar{display:none}",
      ".rw-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--rw-card-min),1fr));",
      "gap:var(--rw-gap);overflow:visible}",

      ".rw-card{flex:0 0 var(--rw-card-w,var(--rw-card-min));scroll-snap-align:start;",
      "background:var(--rw-card);border:1px solid var(--rw-border);border-radius:var(--rw-radius);",
      "box-shadow:var(--rw-shadow);padding:18px;display:flex;flex-direction:column;gap:12px}",
      ".rw-grid .rw-card{flex:1 1 auto}",
      ".rw-card-top{display:flex;align-items:center;gap:8px}",
      ".rw-pin{font-size:11px;font-weight:600;color:var(--rw-accent);border:1px solid currentColor;",
      "border-radius:999px;padding:1px 8px}",
      ".rw-text{margin:0;white-space:pre-line;overflow-wrap:anywhere;flex:1 1 auto}",
      ".rw-clamp{display:-webkit-box;-webkit-line-clamp:7;-webkit-box-orient:vertical;overflow:hidden}",
      ".rw-more{align-self:flex-start;background:none;border:0;padding:0;font:inherit;font-size:13px;",
      "color:var(--rw-accent);cursor:pointer;font-weight:550}",
      // Карточки в ряду тянутся по самой высокой, поэтому длинный ответ
      // компании обрезаем — иначе он раздувает вообще все карточки.
      ".rw-reply{border-left:2px solid var(--rw-accent);padding:2px 0 2px 12px;font-size:14px;",
      "color:var(--rw-muted);display:-webkit-box;-webkit-line-clamp:4;",
      "-webkit-box-orient:vertical;overflow:hidden}",
      ".rw-reply b{color:var(--rw-fg);font-weight:600;display:block;font-size:13px}",
      ".rw-foot{display:flex;align-items:center;gap:10px;margin-top:auto;padding-top:4px}",
      ".rw-ava{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;",
      "font-size:13px;font-weight:650;color:#fff;flex:0 0 auto}",
      ".rw-who{min-width:0}",
      ".rw-name{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;",
      "text-overflow:ellipsis}",
      ".rw-meta{font-size:12px;color:var(--rw-muted)}",

      ".rw-nav{position:absolute;top:50%;transform:translateY(-50%);width:38px;height:38px;",
      "border-radius:50%;display:grid;place-items:center;padding:0;z-index:2;",
      "box-shadow:var(--rw-shadow);opacity:0;transition:opacity .15s ease}",
      ".rw-viewport:hover .rw-nav,.rw-nav:focus-visible{opacity:1}",
      "@media (hover:none){.rw-nav{display:none}}",
      ".rw-prev{left:-14px}.rw-next{right:-14px}",
      ".rw-nav[disabled]{opacity:0!important;pointer-events:none}",
      ".rw-nav svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:2}",

      ".rw-dots{display:flex;justify-content:center;gap:7px;margin-top:14px}",
      ".rw-dot{width:7px;height:7px;padding:0;border:0;border-radius:50%;cursor:pointer;",
      "background:var(--rw-border);transition:.2s ease}",
      ".rw-dot-on{background:var(--rw-accent);width:22px;border-radius:999px}",

      ".rw-skeleton{background:var(--rw-card);border:1px solid var(--rw-border);",
      "border-radius:var(--rw-radius);min-height:190px;flex:0 0 var(--rw-card-w,var(--rw-card-min));",
      "opacity:.55;animation:rw-pulse 1.4s ease-in-out infinite}",
      "@keyframes rw-pulse{50%{opacity:.25}}",

      ".rw-modal{position:fixed;inset:0;z-index:2147483000;background:rgba(11,14,20,.55);",
      "display:flex;align-items:center;justify-content:center;padding:16px}",
      ".rw-modal iframe{width:100%;max-width:520px;height:min(720px,92vh);border:0;",
      "border-radius:var(--rw-radius);background:var(--rw-card)}",
      ".rw-close{position:absolute;top:12px;right:14px;width:34px;height:34px;border-radius:50%;",
      "border:0;background:rgba(255,255,255,.9);color:#151922;font-size:20px;cursor:pointer;",
      "line-height:1}",
      "@media (max-width:520px){.rw-title{font-size:18px}.rw-card{padding:16px}}"
    ].join("");
  }

  /* --------------------------------------------------------- сама карусель */

  function Widget(mount, config) {
    this.mount = mount;
    this.config = config;
    this.settings = coerce(config.settings);
    this.reviews = [];
    this.stats = { count: 0, average: 0 };
    this.page = 0;
    this.perView = 1;
    this.timer = null;
    this.paused = false;
  }

  Widget.prototype.render = function () {
    var host = el("div");
    host.setAttribute("data-reviews-widget-root", "");
    this.mount.appendChild(host);

    this.root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
    this.style = el("style");
    this.style.textContent = css(this.settings);
    this.root.appendChild(this.style);

    this.box = el("div");
    this.applyTheme();
    this.root.appendChild(this.box);

    this.buildHead();
    this.buildBody();
    this.showSkeleton();
    this.load();
  };

  Widget.prototype.applyTheme = function () {
    // rw-dark — тёмная тема принудительно, rw-auto — по системной настройке.
    var theme = this.settings.theme;
    this.box.className = "rw " + (theme === "dark" ? "rw-dark"
      : theme === "auto" ? "rw-auto" : "rw-light");
  };

  Widget.prototype.buildHead = function () {
    var head = el("div", "rw-head");
    this.head = head;

    var left = el("div");
    left.appendChild(el("h3", "rw-title", this.settings.title));
    this.summary = el("div", "rw-summary");
    left.appendChild(this.summary);
    head.appendChild(left);
    head.appendChild(el("div", "rw-spacer"));

    if (this.settings.show_form_button) {
      var btn = el("button", "rw-btn rw-btn-primary", this.settings.form_button_text);
      btn.type = "button";
      var self = this;
      btn.addEventListener("click", function () { self.openForm(); });
      head.appendChild(btn);
    }
    this.box.appendChild(head);
  };

  Widget.prototype.buildBody = function () {
    var grid = this.settings.layout === "grid";
    this.viewport = el("div", "rw-viewport");
    this.track = el("div", grid ? "rw-grid" : "rw-track");

    if (!grid) {
      this.track.tabIndex = 0;
      this.track.setAttribute("role", "region");
      this.track.setAttribute("aria-roledescription", "карусель");
      this.track.setAttribute("aria-label", this.settings.title);
      this.viewport.appendChild(this.nav("prev", "M12.5 15L7.5 10l5-5"));
      this.viewport.appendChild(this.nav("next", "M7.5 5l5 5-5 5"));

      var self = this;
      var ticking = false;
      this.track.addEventListener("scroll", function () {
        if (ticking) return;
        ticking = true;
        window.requestAnimationFrame(function () {
          ticking = false;
          self.syncPage();
        });
      }, { passive: true });
      this.track.addEventListener("keydown", function (event) {
        if (event.key === "ArrowRight") { self.go(self.page + 1); event.preventDefault(); }
        if (event.key === "ArrowLeft") { self.go(self.page - 1); event.preventDefault(); }
      });
      ["pointerenter", "focusin", "touchstart"].forEach(function (name) {
        self.viewport.addEventListener(name, function () { self.paused = true; }, { passive: true });
      });
      ["pointerleave", "focusout"].forEach(function (name) {
        self.viewport.addEventListener(name, function () { self.paused = false; });
      });
    }

    this.viewport.appendChild(this.track);
    this.box.appendChild(this.viewport);

    if (!grid) {
      this.dots = el("div", "rw-dots");
      this.box.appendChild(this.dots);
    }
  };

  Widget.prototype.nav = function (kind, path) {
    var btn = el("button", "rw-btn rw-nav rw-" + kind);
    btn.type = "button";
    btn.setAttribute("aria-label", kind === "prev" ? "Предыдущие отзывы" : "Следующие отзывы");
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("aria-hidden", "true");
    var line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", path);
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("stroke-linejoin", "round");
    svg.appendChild(line);
    btn.appendChild(svg);
    var self = this;
    btn.addEventListener("click", function () { self.go(self.page + (kind === "prev" ? -1 : 1)); });
    this[kind + "Btn"] = btn;
    return btn;
  };

  Widget.prototype.showSkeleton = function () {
    for (var i = 0; i < 3; i++) this.track.appendChild(el("div", "rw-skeleton"));
  };

  Widget.prototype.load = function () {
    var self = this;
    var url = this.config.api + "/api/v1/widget?site=" + encodeURIComponent(this.config.site) +
      "&limit=" + this.settings.limit + "&min_rating=" + this.settings.min_rating;
    // В превью кабинета кеш только мешает — правки должны быть видны сразу.
    if (this.config.nocache) url += "&_=" + new Date().getTime();
    request(url, function (error, data) {
      if (error || !data || data.error) {
        self.destroy();
        return;
      }
      // Настройки из админки применяются поверх зашитых, но data-атрибуты
      // на теге script всё равно главнее — их владелец сайта задал руками.
      self.settings = coerce(merge(self.settings, data.settings, self.config.overrides));
      self.style.textContent = css(self.settings);
      self.applyTheme();
      self.reviews = data.reviews || [];
      self.stats = data.stats || { count: 0, average: 0 };
      self.paint();
    });
  };

  Widget.prototype.paint = function () {
    if (!this.reviews.length) {
      this.destroy();
      return;
    }
    this.track.textContent = "";
    for (var i = 0; i < this.reviews.length; i++) {
      this.track.appendChild(this.card(this.reviews[i], i));
    }
    this.paintSummary();
    if (this.settings.layout === "carousel") {
      this.measure();
      this.observe();
      this.buildDots();
      this.startAutoplay();
    }
    if (this.settings.seo_schema) this.injectSchema();
  };

  Widget.prototype.paintSummary = function () {
    this.summary.textContent = "";
    if (!this.settings.show_summary || !this.stats.count) return;
    var average = Number(this.stats.average || 0);
    this.summary.appendChild(el("span", "rw-avg", average.toFixed(1).replace(".", ",")));
    this.summary.appendChild(stars(average, 15));
    var count = this.stats.count;
    var word = count % 10 === 1 && count % 100 !== 11 ? " отзыв"
      : [2, 3, 4].indexOf(count % 10) >= 0 && [12, 13, 14].indexOf(count % 100) < 0 ? " отзыва"
        : " отзывов";
    this.summary.appendChild(el("span", null, count + word));
  };

  Widget.prototype.card = function (review, index) {
    var card = el("article", "rw-card");
    card.setAttribute("aria-label", "Отзыв " + (index + 1) + " из " + this.reviews.length);

    var top = el("div", "rw-card-top");
    top.appendChild(stars(review.rating));
    if (review.pinned) top.appendChild(el("span", "rw-pin", "Выбор редакции"));
    card.appendChild(top);

    var text = el("p", "rw-text rw-clamp", review.text);
    card.appendChild(text);

    var more = el("button", "rw-more", "Читать полностью");
    more.type = "button";
    more.hidden = true;
    more.addEventListener("click", function () {
      var open = text.classList.toggle("rw-clamp");
      more.textContent = open ? "Читать полностью" : "Свернуть";
    });
    card.appendChild(more);
    // Кнопку показываем, только если текст реально не помещается.
    window.setTimeout(function () {
      if (text.scrollHeight - text.clientHeight > 4) more.hidden = false;
    }, 60);

    if (review.reply) {
      var reply = el("div", "rw-reply");
      reply.appendChild(el("b", null, "Ответ компании"));
      reply.appendChild(document.createTextNode(review.reply));
      card.appendChild(reply);
    }

    var foot = el("div", "rw-foot");
    var ava = el("div", "rw-ava", review.initials || "?");
    ava.style.background = "hsl(" + avatarHue(review.author || "") + " 62% 48%)";
    ava.setAttribute("aria-hidden", "true");
    foot.appendChild(ava);

    var who = el("div", "rw-who");
    who.appendChild(el("div", "rw-name", review.author || "Аноним"));
    var meta = [formatDate(review.date), SOURCE_TITLES[review.source] || ""]
      .filter(Boolean).join(" · ");
    who.appendChild(el("div", "rw-meta", meta));
    foot.appendChild(who);
    card.appendChild(foot);
    return card;
  };

  /* ------------------------------------------------------- прокрутка/пагинация */

  Widget.prototype.measure = function () {
    var width = this.viewport.clientWidth || this.settings.card_min_width;
    var gap = 16;
    var perView = Math.floor((width + gap) / (this.settings.card_min_width + gap));
    this.perView = Math.max(1, Math.min(MAX_PER_VIEW, perView, this.reviews.length));
    var cardWidth = (width - gap * (this.perView - 1)) / this.perView;
    this.box.style.setProperty("--rw-card-w", Math.floor(cardWidth) + "px");
    this.step = cardWidth + gap;
    this.pages = Math.max(1, Math.ceil(this.reviews.length / this.perView));
  };

  Widget.prototype.observe = function () {
    var self = this;
    var replan = function () {
      self.measure();
      self.buildDots();
      self.syncPage();
    };
    if (window.ResizeObserver) {
      this.ro = new window.ResizeObserver(replan);
      this.ro.observe(this.viewport);
    } else {
      window.addEventListener("resize", replan);
    }
  };

  Widget.prototype.buildDots = function () {
    if (!this.dots) return;
    this.dots.textContent = "";
    if (this.pages < 2) return;
    var self = this;
    for (var i = 0; i < this.pages; i++) {
      var dot = el("button", "rw-dot" + (i === this.page ? " rw-dot-on" : ""));
      dot.type = "button";
      dot.setAttribute("aria-label", "Страница " + (i + 1));
      (function (page) {
        dot.addEventListener("click", function () { self.go(page); });
      })(i);
      this.dots.appendChild(dot);
    }
    this.syncNav();
  };

  Widget.prototype.go = function (page) {
    if (this.pages < 1) return;
    // Долистали до конца — возвращаемся в начало, карусель бесконечная.
    if (page >= this.pages) page = 0;
    if (page < 0) page = this.pages - 1;
    this.page = page;
    var left = Math.round(page * this.step * this.perView);
    try {
      this.track.scrollTo({ left: left, behavior: reducedMotion ? "auto" : "smooth" });
    } catch (e) {
      this.track.scrollLeft = left;
    }
    this.syncDots();
  };

  Widget.prototype.syncPage = function () {
    if (!this.step) return;
    var page = Math.round(this.track.scrollLeft / (this.step * this.perView));
    // На последней странице скролл упирается в край и не совпадает с шагом.
    if (this.track.scrollLeft + this.track.clientWidth >= this.track.scrollWidth - 2) {
      page = this.pages - 1;
    }
    page = Math.max(0, Math.min(this.pages - 1, page));
    if (page !== this.page) {
      this.page = page;
      this.syncDots();
    }
  };

  Widget.prototype.syncDots = function () {
    if (!this.dots) return;
    var nodes = this.dots.children;
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].className = "rw-dot" + (i === this.page ? " rw-dot-on" : "");
      if (i === this.page) nodes[i].setAttribute("aria-current", "true");
      else nodes[i].removeAttribute("aria-current");
    }
    this.syncNav();
  };

  Widget.prototype.syncNav = function () {
    if (!this.prevBtn) return;
    var single = this.pages < 2;
    this.prevBtn.disabled = single;
    this.nextBtn.disabled = single;
  };

  Widget.prototype.startAutoplay = function () {
    if (!this.settings.autoplay || reducedMotion || this.pages < 2) return;
    var self = this;
    this.timer = window.setInterval(function () {
      if (self.paused || document.hidden) return;
      self.go(self.page + 1);
    }, Math.max(2000, this.settings.interval));
  };

  /* ---------------------------------------------------------- форма и SEO */

  Widget.prototype.openForm = function () {
    var self = this;
    var modal = el("div", "rw-modal");
    var frame = el("iframe");
    frame.src = this.config.api + "/f/" + encodeURIComponent(this.config.site) + "?embed=1";
    frame.title = "Форма отзыва";
    modal.appendChild(frame);

    var close = el("button", "rw-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Закрыть");
    modal.appendChild(close);

    function shut() {
      if (modal.parentNode) modal.parentNode.removeChild(modal);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("message", onMessage);
      self.paused = false;
    }
    function onKey(event) { if (event.key === "Escape") shut(); }
    function onMessage(event) {
      if (event.data && event.data.type === "rw:submitted") {
        window.setTimeout(shut, 2500);
      }
    }
    modal.addEventListener("click", function (event) { if (event.target === modal) shut(); });
    close.addEventListener("click", shut);
    document.addEventListener("keydown", onKey);
    window.addEventListener("message", onMessage);

    this.paused = true;
    this.root.appendChild(modal);
    close.focus();
  };

  Widget.prototype.injectSchema = function () {
    if (!this.stats.count || document.getElementById("rw-schema-" + this.config.site)) return;
    var data = {
      "@context": "https://schema.org",
      "@type": "Organization",
      name: this.config.name || this.settings.title,
      aggregateRating: {
        "@type": "AggregateRating",
        ratingValue: String(this.stats.average),
        reviewCount: String(this.stats.count),
        bestRating: "5",
        worstRating: "1"
      }
    };
    var node = el("script");
    node.type = "application/ld+json";
    node.id = "rw-schema-" + this.config.site;
    node.textContent = JSON.stringify(data).replace(/</g, "\\u003c");
    document.head.appendChild(node);
  };

  Widget.prototype.destroy = function () {
    if (this.timer) window.clearInterval(this.timer);
    if (this.ro) this.ro.disconnect();
    if (this.mount && this.mount.parentNode && this.mount.hasAttribute("data-rw-auto")) {
      this.mount.parentNode.removeChild(this.mount);
    } else if (this.box) {
      this.box.textContent = "";
    }
  };

  /* ------------------------------------------------------------- запуск */

  function attr(node, name) {
    return (node && node.getAttribute && node.getAttribute(name)) || "";
  }

  function configFor(node) {
    // Настройки могут висеть и на теге script, и на контейнере. Контейнер
    // главнее: он ближе к месту, где виджет реально стоит.
    var overrides = node === CURRENT
      ? fromDataset(CURRENT)
      : merge(fromDataset(CURRENT), fromDataset(node));
    return {
      site: attr(node, "data-site") || attr(CURRENT, "data-site") || BAKED.site || "",
      api: attr(node, "data-api") || attr(CURRENT, "data-api") || apiBase(),
      name: BAKED.name || "",
      nocache: !!BAKED.nocache,
      settings: merge(BAKED.settings, overrides),
      overrides: overrides
    };
  }

  function mountPoint(node) {
    if (node && node.tagName !== "SCRIPT") return node;
    var holder = el("div");
    holder.setAttribute("data-rw-auto", "");
    // Обёртку мы создали сами — её тоже незачем отдавать стилям сайта.
    holder.style.setProperty("all", "initial", "important");
    holder.style.setProperty("display", "block", "important");
    if (node && node.parentNode) node.parentNode.insertBefore(holder, node.nextSibling);
    else document.body.appendChild(holder);
    return holder;
  }

  function boot() {
    var targets = [];
    // Явные контейнеры на странице — если их нет, встаём на место тега script.
    var explicit = document.querySelectorAll("[data-reviews-widget]");
    for (var i = 0; i < explicit.length; i++) {
      if (!explicit[i].hasAttribute("data-rw-done")) targets.push(explicit[i]);
    }
    if (!targets.length && CURRENT) targets.push(CURRENT);

    for (var j = 0; j < targets.length; j++) {
      var node = targets[j];
      var config = configFor(node);
      if (!config.site) continue;
      node.setAttribute("data-rw-done", "1");
      try {
        new Widget(mountPoint(node), config).render();
      } catch (error) {
        if (window.console) window.console.warn("[reviews-widget]", error);
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
