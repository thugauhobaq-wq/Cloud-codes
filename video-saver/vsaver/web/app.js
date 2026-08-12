/* Страница целиком: одна форма, список задач и прогресс.

   Каркаса нет и не надо — весь интерфейс это форма, список карточек и поток
   событий с сервера. Прогресс приходит через EventSource: соединение держит
   сервер, браузер сам переподключается при обрыве, и никакого опроса по таймеру
   писать не приходится.

   Задачи хранятся на сервере и привязаны к cookie сессии, поэтому список
   переживает перезагрузку страницы: при открытии он просто загружается заново. */

const api = {
  config: () => fetch("/api/config").then(asJson),
  jobs: () => fetch("/api/jobs").then(asJson),
  create: (body) =>
    fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(asJson),
  remove: (id) => fetch(`/api/jobs/${id}`, { method: "DELETE" }).then(asJson),
};

async function asJson(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && payload.error) || `сервер ответил ${response.status}`;
    const failure = new Error(message);
    failure.status = response.status;
    throw failure;
  }
  return payload;
}

const dom = {
  form: document.getElementById("form"),
  url: document.getElementById("url"),
  paste: document.getElementById("paste"),
  go: document.getElementById("go"),
  quality: document.getElementById("quality"),
  qualityBox: document.getElementById("quality-box"),
  hint: document.getElementById("hint"),
  jobs: document.getElementById("jobs"),
  empty: document.getElementById("empty"),
  listTitle: document.getElementById("list-title"),
  keepNote: document.getElementById("keep-note"),
  toast: document.getElementById("toast"),
  install: document.getElementById("install"),
  template: document.getElementById("job-template"),
};

const cards = new Map(); // id задачи -> { node, source }
let config = null;

// --- запуск -----------------------------------------------------------------

init();

async function init() {
  bindForm();
  bindInstall();
  takeShared();
  try {
    config = await api.config();
    applyConfig(config);
  } catch (error) {
    say("Сервер не отвечает. Обновите страницу.");
  }
  try {
    const jobs = await api.jobs();
    jobs.reverse().forEach(show);
  } catch (error) {
    /* пустой список — не беда, просто ещё ничего не качали */
  }
  refreshEmpty();
}

function applyConfig(data) {
  const qualities = data.qualities || {};
  dom.quality.innerHTML = "";
  for (const [value, label] of Object.entries(qualities)) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    if (value === data.defaultQuality) option.selected = true;
    dom.quality.append(option);
  }
  const limits = data.limits || {};
  const parts = [];
  if (limits.maxDuration) parts.push(`до ${humanTime(limits.maxDuration)}`);
  if (limits.maxSize) parts.push(`до ${humanSize(limits.maxSize)}`);
  if (limits.perHour) parts.push(`${limits.perHour} в час с одного адреса`);
  dom.hint.textContent = parts.length ? `Ограничения: ${parts.join(", ")}.` : "";
  if (limits.keepHours) {
    dom.keepNote.textContent =
      `Скачанные файлы удаляются с сервера через ${humanTime(limits.keepHours * 3600)} — ` +
      "сохраните файл к себе сразу.";
  }
}

// --- форма ------------------------------------------------------------------

function bindForm() {
  dom.form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = dom.url.value.trim();
    if (!url) return;

    const kind = dom.form.querySelector("input[name=kind]:checked").value;
    dom.go.disabled = true;
    try {
      const job = await api.create({ url, kind, quality: dom.quality.value });
      dom.url.value = "";
      show(job);
      refreshEmpty();
    } catch (error) {
      say(error.message);
    } finally {
      dom.go.disabled = false;
    }
  });

  dom.form.addEventListener("change", (event) => {
    if (event.target.name === "kind") {
      // Для звука качество видео ни на что не влияет — прячем, чтобы не врать.
      dom.qualityBox.hidden = event.target.value === "audio";
    }
  });

  // Кнопка «Вставить» появляется только там, где браузер даёт читать буфер:
  // на телефоне это заметно быстрее, чем долгое нажатие и выбор в меню.
  if (navigator.clipboard && navigator.clipboard.readText) {
    dom.paste.hidden = false;
    dom.paste.addEventListener("click", async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          dom.url.value = text.trim();
          dom.url.focus();
        }
      } catch (error) {
        say("Браузер не дал прочитать буфер — вставьте вручную.");
      }
    });
  }
}

/* Ссылка, прилетевшая через «Поделиться» с телефона. Android кладёт её то в
   url, то в text — в зависимости от того, чем поделились: приложение YouTube,
   например, отдаёт текст с подписью, внутри которого ссылка. */
function takeShared() {
  const params = new URLSearchParams(location.search);
  const raw = params.get("url") || params.get("text") || "";
  if (!raw) return;
  const found = raw.match(/https?:\/\/\S+/);
  dom.url.value = (found ? found[0] : raw).trim();
  history.replaceState(null, "", location.pathname);
}

// --- карточки ---------------------------------------------------------------

function show(job) {
  let card = cards.get(job.id);
  if (!card) {
    const node = dom.template.content.firstElementChild.cloneNode(true);
    node.dataset.id = job.id;
    card = { node, source: null, previewed: false };
    cards.set(job.id, card);
    bindCard(card, job.id);
    dom.jobs.prepend(node);
  }
  paint(card, job);
  if (isActive(job.status)) subscribe(card, job.id);
  else unsubscribe(card);
  refreshEmpty();
}

function bindCard(card, id) {
  const { node } = card;
  node.querySelector(".job-cancel").addEventListener("click", async () => {
    try {
      await api.remove(id);
    } catch (error) {
      say(error.message);
    }
  });
  node.querySelector(".job-remove").addEventListener("click", async () => {
    try {
      await api.remove(id);
    } catch (error) {
      /* уже удалена — просто убираем карточку */
    }
    unsubscribe(card);
    cards.delete(id);
    node.remove();
    refreshEmpty();
  });
  node.querySelector(".job-preview").addEventListener("click", () => {
    const box = node.querySelector(".player");
    if (card.previewed) {
      box.hidden = !box.hidden;
      return;
    }
    card.previewed = true;
    box.hidden = false;
    box.append(buildPlayer(id, node.dataset.kind));
  });
}

function paint(card, job) {
  const { node } = card;
  node.dataset.kind = job.kind;
  node.dataset.status = job.status;

  node.querySelector(".job-name").textContent = job.title || job.url;
  node.querySelector(".job-meta").textContent = metaLine(job);

  const bar = node.querySelector(".bar i");
  bar.style.width = `${job.status === "done" ? 100 : job.percent}%`;

  node.querySelector(".job-status").textContent = statusLine(job);

  const save = node.querySelector(".job-save");
  const preview = node.querySelector(".job-preview");
  const cancel = node.querySelector(".job-cancel");
  const done = job.status === "done";
  save.hidden = !done;
  preview.hidden = !done;
  cancel.hidden = !isActive(job.status);
  if (done) {
    save.href = `/api/jobs/${job.id}/file`;
    save.setAttribute("download", "");
  }
}

function buildPlayer(id, kind) {
  const player = document.createElement(kind === "audio" ? "audio" : "video");
  player.controls = true;
  player.preload = "metadata";
  player.src = `/api/jobs/${id}/file?inline=1`;
  return player;
}

function metaLine(job) {
  const parts = [];
  if (job.site) parts.push(job.site);
  if (job.duration) parts.push(humanTime(job.duration));
  if (job.kind === "audio") parts.push("звук");
  else if (job.quality) parts.push(`${job.quality}p`);
  return parts.join(" · ");
}

function statusLine(job) {
  switch (job.status) {
    case "queued":
      return "В очереди";
    case "running":
      return runningLine(job);
    case "done":
      return job.total ? `Готово · ${humanSize(job.total)}` : "Готово";
    case "cancelled":
      return "Отменено";
    case "failed":
      return job.error || "Не получилось";
    default:
      return job.status;
  }
}

function runningLine(job) {
  if (job.stage === "probe") return "Смотрим, что по ссылке…";
  if (job.stage === "convert") return "Собираем файл…";
  const parts = [`${job.percent}%`];
  if (job.total) parts.push(`${humanSize(job.downloaded)} из ${humanSize(job.total)}`);
  if (job.speed) parts.push(`${humanSize(job.speed)}/с`);
  if (job.eta) parts.push(`осталось ${humanTime(job.eta)}`);
  return parts.join(" · ");
}

// --- поток событий ----------------------------------------------------------

function subscribe(card, id) {
  if (card.source) return;
  const source = new EventSource(`/api/jobs/${id}/events`);
  card.source = source;
  source.addEventListener("message", (event) => {
    let job = null;
    try {
      job = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    paint(card, job);
    if (!isActive(job.status)) unsubscribe(card);
  });
  source.addEventListener("error", () => {
    // EventSource переподключается сам; закрываем только когда сервер сказал,
    // что задача завершилась, — иначе потеряем прогресс на первом же чихе сети.
    if (source.readyState === EventSource.CLOSED) card.source = null;
  });
}

function unsubscribe(card) {
  if (card.source) {
    card.source.close();
    card.source = null;
  }
}

function isActive(status) {
  return status === "queued" || status === "running";
}

// --- мелочи -----------------------------------------------------------------

function refreshEmpty() {
  const any = cards.size > 0;
  dom.empty.hidden = any;
  dom.listTitle.hidden = !any;
}

let toastTimer = 0;
function say(message) {
  dom.toast.textContent = message;
  dom.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    dom.toast.hidden = true;
  }, 6000);
}

function humanSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 ** 3) return `${(value / 1024 ** 3).toFixed(1)} ГБ`;
  if (value >= 1024 ** 2) return `${(value / 1024 ** 2).toFixed(0)} МБ`;
  if (value >= 1024) return `${(value / 1024).toFixed(0)} КБ`;
  return `${value} Б`;
}

function humanTime(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (hours) return minutes ? `${hours} ч ${minutes} мин` : `${hours} ч`;
  if (minutes) return `${minutes} мин`;
  return `${total} с`;
}

function bindInstall() {
  let prompt = null;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    prompt = event;
    dom.install.hidden = false;
  });
  dom.install.addEventListener("click", async () => {
    if (!prompt) return;
    dom.install.hidden = true;
    prompt.prompt();
    prompt = null;
  });
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
  }
}
