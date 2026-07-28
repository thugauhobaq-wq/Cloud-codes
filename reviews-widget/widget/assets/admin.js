/* Кабинет модерации: список отзывов, действия и настройки виджета. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var STORE = "rw-admin";
  var auth = { site: "", token: "" };
  var tab = "pending";

  /* ---------------------------------------------------------------- API */

  function api(path, options) {
    options = options || {};
    return fetch(path, {
      method: options.method || "GET",
      headers: {
        "Content-Type": "application/json",
        "X-Site": auth.site,
        "X-Admin-Token": auth.token
      },
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || "Ошибка " + response.status);
        return data;
      });
    });
  }

  /* --------------------------------------------------------------- вход */

  function saved() {
    try { return JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { return {}; }
  }

  function enter(site, token) {
    auth = { site: site, token: token };
    return api("/api/admin/site").then(function (data) {
      try { localStorage.setItem(STORE, JSON.stringify(auth)); } catch (e) { /* приватный режим */ }
      $("gate").hidden = true;
      $("app").hidden = false;
      fillSite(data);
      return load();
    });
  }

  $("gateForm").addEventListener("submit", function (event) {
    event.preventDefault();
    $("gateError").hidden = true;
    enter($("gateSite").value.trim(), $("gateToken").value.trim()).catch(function (error) {
      $("gateError").textContent = error.message === "Ошибка 403"
        ? "Не подходит ключ или токен" : error.message;
      $("gateError").hidden = false;
    });
  });

  $("logout").addEventListener("click", function () {
    try { localStorage.removeItem(STORE); } catch (e) { /* ничего */ }
    location.reload();
  });

  /* ------------------------------------------------------------ вкладки */

  $("tabs").addEventListener("click", function (event) {
    var button = event.target.closest(".tab");
    if (!button) return;
    tab = button.dataset.tab;
    Array.prototype.forEach.call($("tabs").children, function (node) {
      node.classList.toggle("is-on", node === button);
    });
    $("listPanel").hidden = ["pending", "published", "rejected"].indexOf(tab) < 0;
    $("setupPanel").hidden = tab !== "setup";
    $("settingsPanel").hidden = tab !== "settings";
    if (!$("listPanel").hidden) load();
  });

  /* -------------------------------------------------------------- сайт */

  var site = null;

  function fillSite(data) {
    site = data.site;
    $("siteName").textContent = site.name;
    $("siteKey").textContent = site.key;
    paintCounts(data.counts);

    var origin = location.origin;
    $("embedCode").textContent =
      '<script src="' + origin + '/w/' + site.key + '.js" async><\/script>';
    $("formLink").textContent = origin + "/f/" + site.key;
    $("preview").src = "/preview/" + site.key;

    var form = $("settingsForm");
    var values = Object.assign({}, site.settings, {
      auto_approve: data.site.auto_approve,
      auto_approve_from: data.site.auto_approve_from,
      domains: (data.site.domains || []).join(", "),
      telegram_token: data.site.telegram_token,
      telegram_chat: data.site.telegram_chat
    });
    Array.prototype.forEach.call(form.elements, function (field) {
      if (!field.name || !(field.name in values)) return;
      if (field.type === "checkbox") field.checked = !!values[field.name];
      else field.value = values[field.name];
    });
  }

  function paintCounts(counts) {
    $("cntPending").textContent = counts.pending || 0;
    $("cntPublished").textContent = counts.published || 0;
    $("cntRejected").textContent = counts.rejected || 0;
  }

  $("settingsForm").addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.target;
    var settings = {};
    var payload = {};
    Array.prototype.forEach.call(form.elements, function (field) {
      if (!field.name) return;
      var value = field.type === "checkbox" ? field.checked : field.value;
      if (["auto_approve", "auto_approve_from", "domains",
        "telegram_token", "telegram_chat"].indexOf(field.name) >= 0) {
        payload[field.name] = value;
      } else {
        settings[field.name] = value;
      }
    });
    payload.settings = settings;

    api("/api/admin/site", { method: "POST", body: payload }).then(function (data) {
      fillSite(data);
      $("saved").hidden = false;
      $("preview").contentWindow.location.reload();
      setTimeout(function () { $("saved").hidden = true; }, 2500);
    }).catch(function (error) { alert(error.message); });
  });

  $("copyEmbed").addEventListener("click", function () {
    copy($("embedCode").textContent, $("copyEmbed"), "Скопировать");
  });
  $("copyForm").addEventListener("click", function () {
    copy($("formLink").textContent, $("copyForm"), "Скопировать ссылку");
  });

  function copy(value, button, original) {
    var done = function () {
      button.textContent = "Скопировано";
      setTimeout(function () { button.textContent = original; }, 1800);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(value).then(done, done);
    else done();
  }

  /* ------------------------------------------------------------- список */

  function load() {
    return api("/api/admin/reviews?status=" + tab).then(function (data) {
      paintCounts(data.counts);
      var list = $("list");
      list.textContent = "";
      $("empty").hidden = data.reviews.length > 0;
      data.reviews.forEach(function (review) { list.appendChild(card(review)); });
    }).catch(function (error) { console.warn(error); });
  }

  function formatDate(iso) {
    var date = new Date(iso);
    if (isNaN(date.getTime())) return "";
    return date.toLocaleString("ru-RU", {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"
    });
  }

  function card(review) {
    var node = $("cardTpl").content.cloneNode(true);
    var root = node.querySelector(".card");
    root.dataset.id = review.id;

    node.querySelector(".stars").textContent =
      "★".repeat(review.rating) + "☆".repeat(5 - review.rating);
    node.querySelector(".source").textContent =
      review.source === "telegram" ? "Telegram" : review.source === "form" ? "Сайт" : review.source;
    node.querySelector(".pin").hidden = !review.pinned;
    node.querySelector("time").textContent = formatDate(review.created_at);
    node.querySelector(".text").textContent = review.text;
    node.querySelector(".author").textContent = review.author;
    node.querySelector(".contact").textContent = review.contact || "";

    if (review.reply) {
      node.querySelector(".reply-box").hidden = false;
      node.querySelector(".reply-text").textContent = review.reply;
    }

    var publish = node.querySelector('[data-act="publish"]');
    var reject = node.querySelector('[data-act="reject"]');
    var pin = node.querySelector('[data-act="pin"]');
    if (review.status === "published") publish.hidden = true;
    if (review.status === "rejected") reject.hidden = true;
    pin.textContent = review.pinned ? "Открепить" : "Закрепить";
    pin.dataset.act = review.pinned ? "unpin" : "pin";

    root.addEventListener("click", function (event) {
      var button = event.target.closest("[data-act]");
      if (!button) return;
      act(review, button.dataset.act, root);
    });
    return node;
  }

  function act(review, action, root) {
    var body = { action: action };
    if (action === "reply") {
      var reply = prompt("Ответ компании (виден всем на сайте):", review.reply || "");
      if (reply === null) return;
      body.reply = reply;
    }
    if (action === "delete" && !confirm("Удалить отзыв навсегда?")) return;

    api("/api/admin/reviews/" + review.id, { method: "POST", body: body })
      .then(function () { load(); })
      .catch(function (error) { alert(error.message); });
    root.style.opacity = ".5";
  }

  /* ------------------------------------------------------------- старт */

  var stored = saved();
  var fromUrl = new URLSearchParams(location.search).get("site");
  if (fromUrl) $("gateSite").value = fromUrl;
  if (stored.site && stored.token) {
    enter(stored.site, stored.token).catch(function () { /* токен протух — покажем вход */ });
  } else if (stored.site) {
    $("gateSite").value = stored.site;
  }
})();
