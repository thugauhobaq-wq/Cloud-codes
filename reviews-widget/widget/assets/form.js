/* Логика формы отзыва. Отправляет JSON в /api/v1/reviews. */

(function () {
  "use strict";

  var CONFIG = window.__RW_FORM__ || {};
  var LABELS = ["", "Ужасно", "Плохо", "Нормально", "Хорошо", "Отлично"];
  var STAR_PATH = "M10 1.6l2.5 5.1 5.6.8-4 3.9 1 5.6-5.1-2.7-5 2.7 1-5.6-4.1-3.9 5.6-.8z";

  var $ = function (id) { return document.getElementById(id); };
  var opened = Date.now();
  var rating = 0;
  var sending = false;

  if (CONFIG.embed) document.body.classList.add("embed");
  if (CONFIG.name) {
    // Название компании уже может быть в кавычках — свои не добавляем.
    document.title = "Отзыв — " + CONFIG.name;
    $("title").textContent = CONFIG.name;
  }
  if (CONFIG.accent) document.documentElement.style.setProperty("--accent", CONFIG.accent);

  /* ------------------------------------------------------------- звёзды */

  var starsBox = $("stars");
  var buttons = [];

  function paintStars(value) {
    buttons.forEach(function (btn, index) {
      var on = index < value;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-checked", index + 1 === rating ? "true" : "false");
    });
    $("ratingLabel").textContent = LABELS[value] || "Выберите оценку";
  }

  for (var i = 1; i <= 5; i++) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "star";
    btn.setAttribute("role", "radio");
    btn.setAttribute("aria-checked", "false");
    btn.setAttribute("aria-label", i + " из 5");
    btn.tabIndex = i === 1 ? 0 : -1;
    btn.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="' + STAR_PATH +
      '"></path></svg>';
    (function (value, node) {
      node.addEventListener("click", function () {
        rating = value;
        buttons.forEach(function (b, idx) { b.tabIndex = idx === value - 1 ? 0 : -1; });
        paintStars(value);
      });
      node.addEventListener("mouseenter", function () { paintStars(value); });
    })(i, btn);
    buttons.push(btn);
    starsBox.appendChild(btn);
  }

  starsBox.addEventListener("mouseleave", function () { paintStars(rating); });
  starsBox.addEventListener("keydown", function (event) {
    var delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!delta) return;
    event.preventDefault();
    var next = Math.min(5, Math.max(1, (rating || 0) + delta));
    rating = next;
    buttons.forEach(function (b, idx) { b.tabIndex = idx === next - 1 ? 0 : -1; });
    buttons[next - 1].focus();
    paintStars(next);
  });

  /* ------------------------------------------------------------ отправка */

  var text = $("text");
  text.addEventListener("input", function () {
    $("counter").textContent = String(text.value.length);
  });

  function fail(message) {
    var box = $("error");
    box.textContent = message;
    box.hidden = false;
  }

  $("form").addEventListener("submit", function (event) {
    event.preventDefault();
    if (sending) return;
    $("error").hidden = true;

    if (!rating) return fail("Поставьте оценку от 1 до 5");
    if (text.value.trim().length < 8) return fail("Напишите пару слов — хотя бы 8 символов");

    var payload = {
      site: CONFIG.site,
      rating: rating,
      text: text.value,
      author: $("author").value,
      contact: $("contact").value,
      website: $("website").value,
      elapsed: (Date.now() - opened) / 1000
    };

    sending = true;
    $("submit").disabled = true;
    $("submit").textContent = "Отправляем…";

    var xhr = new XMLHttpRequest();
    xhr.open("POST", (CONFIG.api || "") + "/api/v1/reviews", true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4) return;
      sending = false;
      $("submit").disabled = false;
      $("submit").textContent = "Отправить отзыв";

      var data = {};
      try { data = JSON.parse(xhr.responseText); } catch (e) { /* пустой ответ */ }

      if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
        $("form").hidden = true;
        document.querySelector(".sheet-head").hidden = true;
        if (data.status === "published") {
          $("doneText").textContent = "Отзыв уже опубликован на сайте.";
        }
        $("done").hidden = false;
        try {
          if (window.parent !== window) {
            window.parent.postMessage({ type: "rw:submitted", status: data.status }, "*");
          }
        } catch (e) { /* другой origin — не страшно */ }
      } else {
        fail(data.error || "Не получилось отправить. Попробуйте ещё раз чуть позже.");
      }
    };
    xhr.onerror = function () {
      sending = false;
      $("submit").disabled = false;
      $("submit").textContent = "Отправить отзыв";
      fail("Нет связи с сервером. Проверьте интернет и попробуйте снова.");
    };
    xhr.send(JSON.stringify(payload));
  });
})();
