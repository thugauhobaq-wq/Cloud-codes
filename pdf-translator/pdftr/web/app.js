/* Приложение целиком: выбор файла, отправка с прогрессом, живой список задач.
   Ванильный JS без сборки — так проще править и нечему протухать.

   Две вещи стоит объяснить заранее:

   * Отправка идёт через XMLHttpRequest, а не fetch. Единственная причина —
     событие upload.progress: у fetch его нет, а на телефоне отправка файла на
     сотню мегабайт по мобильной сети занимает минуты, и молчащий экран в это
     время выглядит как зависшее приложение.
   * За прогрессом перевода следит EventSource. Если он не поднялся (старый
     браузер, сеть через прокси), включается опрос раз в две секунды: медленнее,
     но работает всегда. */

const api = {
  config: () => fetch("/api/config").then((r) => r.json()),
  jobs: () => fetch("/api/jobs").then((r) => r.json()),
  job: (id) => fetch(`/api/jobs/${id}`).then((r) => r.json()),
  remove: (id) => fetch(`/api/jobs/${id}`, { method: "DELETE" }).then((r) => r.json()),
};

const ui = {
  file: document.getElementById("file"),
  drop: document.getElementById("drop"),
  source: document.getElementById("source"),
  target: document.getElementById("target"),
  swap: document.getElementById("swap"),
  jobs: document.getElementById("jobs"),
  empty: document.getElementById("empty"),
  listTitle: document.getElementById("list-title"),
  uploads: document.getElementById("uploads"),
  toast: document.getElementById("toast"),
  install: document.getElementById("install"),
  template: document.getElementById("job-template"),
};

const state = {
  jobs: new Map(),
  streams: new Map(),
  maxUpload: 300 * 1024 * 1024,
};

const STATUS = {
  queued: "в очереди",
  running: "перевожу",
  done: "готово",
  failed: "ошибка",
  cancelled: "отменено",
};

const STAGE = {
  read: "разбираю страницы",
  translate: "перевожу текст",
  build: "собираю файл",
  done: "готово",
};

/* --- мелкие помощники ---------------------------------------------------- */

function size(bytes) {
  if (!bytes) return "";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let value = bytes;
  let step = 0;
  while (value >= 1024 && step < units.length - 1) {
    value /= 1024;
    step += 1;
  }
  return `${value >= 10 || step === 0 ? Math.round(value) : value.toFixed(1)} ${units[step]}`;
}

function plural(count, one, few, many) {
  const mod100 = count % 100;
  const mod10 = count % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

let toastTimer = 0;
function toast(message, bad = false) {
  ui.toast.textContent = message;
  ui.toast.classList.toggle("bad", bad);
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    ui.toast.hidden = true;
  }, bad ? 6000 : 3200);
}

function remember() {
  localStorage.setItem("pdftr.source", ui.source.value);
  localStorage.setItem("pdftr.target", ui.target.value);
}

/* --- языки --------------------------------------------------------------- */

function fillLanguages(config) {
  state.maxUpload = config.maxUpload || state.maxUpload;
  for (const [code, name] of Object.entries(config.languages)) {
    ui.source.append(new Option(name, code));
  }
  for (const [code, name] of Object.entries(config.targets)) {
    ui.target.append(new Option(name, code));
  }
  ui.source.value = localStorage.getItem("pdftr.source") || "auto";
  ui.target.value = localStorage.getItem("pdftr.target") || "ru";
  if (!ui.source.value) ui.source.value = "auto";
  if (!ui.target.value) ui.target.value = "ru";
}

ui.swap.addEventListener("click", () => {
  const from = ui.source.value;
  const to = ui.target.value;
  // «Определить язык» местами не меняется — менять его в обратную сторону не на что.
  if (from === "auto") {
    toast("Сначала выберите язык оригинала");
    return;
  }
  ui.source.value = to;
  ui.target.value = from;
  remember();
});

ui.source.addEventListener("change", remember);
ui.target.addEventListener("change", remember);

/* --- отправка файлов ----------------------------------------------------- */

function send(file) {
  if (!file) return;
  if (file.size > state.maxUpload) {
    toast(`«${file.name}» больше ${size(state.maxUpload)} — столько сервер не примет`, true);
    return;
  }
  const row = document.createElement("div");
  row.className = "upload";
  const label = document.createElement("span");
  label.textContent = file.name;
  const percent = document.createElement("span");
  percent.className = "pct";
  percent.textContent = "0%";
  row.append(label, percent);
  ui.uploads.append(row);
  ui.uploads.hidden = false;

  const query = new URLSearchParams({
    name: file.name,
    source: ui.source.value,
    target: ui.target.value,
  });
  const request = new XMLHttpRequest();
  request.open("POST", `/api/jobs?${query}`);
  request.setRequestHeader("Content-Type", "application/pdf");

  request.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    percent.textContent = `${Math.round((event.loaded / event.total) * 100)}%`;
  });

  const finish = () => {
    row.remove();
    if (!ui.uploads.children.length) ui.uploads.hidden = true;
  };

  request.addEventListener("load", () => {
    finish();
    let payload = {};
    try {
      payload = JSON.parse(request.responseText);
    } catch {
      payload = {};
    }
    if (request.status >= 400) {
      toast(payload.error || `не удалось отправить «${file.name}»`, true);
      return;
    }
    upsert(payload);
    watch(payload.id);
  });

  request.addEventListener("error", () => {
    finish();
    toast(`сеть оборвалась на «${file.name}»`, true);
  });
  request.addEventListener("abort", finish);
  request.send(file);
}

ui.file.addEventListener("change", () => {
  for (const file of ui.file.files) send(file);
  ui.file.value = "";
});

for (const event of ["dragenter", "dragover"]) {
  ui.drop.addEventListener(event, (e) => {
    e.preventDefault();
    ui.drop.classList.add("over");
  });
}
for (const event of ["dragleave", "drop"]) {
  ui.drop.addEventListener(event, () => ui.drop.classList.remove("over"));
}
ui.drop.addEventListener("drop", (event) => {
  event.preventDefault();
  for (const file of event.dataTransfer.files) {
    if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
      send(file);
    }
  }
});

/* --- список задач -------------------------------------------------------- */

function describe(job) {
  if (job.status === "failed") return job.error || "ошибка";
  if (job.status === "done") {
    const parts = [`${job.pages} ${plural(job.pages, "страница", "страницы", "страниц")}`];
    if (job.outSize) parts.push(size(job.outSize));
    if (job.detected && job.source === "auto") parts.push(`язык: ${job.detected}`);
    if (job.warnings && job.warnings.length) parts.push(job.warnings[0]);
    return parts.join(" · ");
  }
  if (job.status === "cancelled") return "отменено";
  if (job.status === "queued") return "в очереди";
  const stage = STAGE[job.stage] || STATUS[job.status];
  return job.pages ? `${stage} · ${job.pagesDone} из ${job.pages}` : stage;
}

function render(job) {
  let node = ui.jobs.querySelector(`[data-id="${job.id}"]`);
  if (!node) {
    node = ui.template.content.firstElementChild.cloneNode(true);
    node.dataset.id = job.id;
    node.querySelector(".job-remove").addEventListener("click", () => remove(job.id));
    node.querySelector(".job-cancel").addEventListener("click", () => remove(job.id));
    ui.jobs.prepend(node);
  }

  node.className = `job ${job.status}`;
  if (job.status === "done" && job.warnings && job.warnings.length) node.classList.add("warned");
  if (job.status === "running" && !job.percent) node.classList.add("pulsing");

  node.querySelector(".job-name").textContent = job.name;
  node.querySelector(".job-meta").textContent = [
    size(job.size),
    `${job.source === "auto" ? "авто" : job.source} → ${job.target}`,
  ].filter(Boolean).join(" · ");
  node.querySelector(".bar > i").style.width = `${job.status === "done" ? 100 : job.percent}%`;
  node.querySelector(".job-status").textContent = describe(job);

  const done = job.status === "done";
  const active = job.status === "queued" || job.status === "running";
  const open = node.querySelector(".job-open");
  const save = node.querySelector(".job-save");
  open.hidden = !done;
  save.hidden = !done;
  node.querySelector(".job-cancel").hidden = !active;
  if (done) {
    open.href = `/api/jobs/${job.id}/file?inline=1`;
    save.href = `/api/jobs/${job.id}/file`;
  }
}

function upsert(job) {
  if (!job || !job.id) return;
  state.jobs.set(job.id, job);
  render(job);
  refreshEmptiness();
}

function refreshEmptiness() {
  const any = state.jobs.size > 0;
  ui.empty.hidden = any;
  ui.listTitle.hidden = !any;
}

async function remove(id) {
  const job = state.jobs.get(id);
  const active = job && (job.status === "queued" || job.status === "running");
  if (active && !confirm("Отменить перевод?")) return;
  stopWatching(id);
  try {
    await api.remove(id);
  } catch {
    toast("не получилось удалить", true);
    return;
  }
  state.jobs.delete(id);
  ui.jobs.querySelector(`[data-id="${id}"]`)?.remove();
  refreshEmptiness();
}

/* --- слежение за прогрессом ---------------------------------------------- */

function stopWatching(id) {
  const stream = state.streams.get(id);
  if (!stream) return;
  if (stream.close) stream.close();
  if (stream.timer) clearInterval(stream.timer);
  state.streams.delete(id);
}

function watch(id) {
  if (state.streams.has(id)) return;
  if (!("EventSource" in window)) return poll(id);

  const stream = new EventSource(`/api/jobs/${id}/events`);
  state.streams.set(id, stream);
  stream.addEventListener("message", (event) => {
    let job;
    try {
      job = JSON.parse(event.data);
    } catch {
      return;
    }
    upsert(job);
    if (job.status !== "queued" && job.status !== "running") {
      stopWatching(id);
      if (job.status === "done") toast(`«${job.name}» переведён`);
    }
  });
  stream.addEventListener("error", () => {
    // Соединение оборвалось — браузер попробует сам, но если задача уже
    // закончилась, ждать нечего: добираем состояние опросом и закрываем.
    stopWatching(id);
    poll(id);
  });
}

function poll(id) {
  if (state.streams.has(id)) return;
  const holder = {
    timer: setInterval(async () => {
      let job;
      try {
        job = await api.job(id);
      } catch {
        return;
      }
      if (job.error && !job.id) return;
      upsert(job);
      if (job.status !== "queued" && job.status !== "running") stopWatching(id);
    }, 2000),
  };
  state.streams.set(id, holder);
}

/* --- запуск -------------------------------------------------------------- */

async function boot() {
  try {
    fillLanguages(await api.config());
  } catch {
    toast("сервер не отвечает", true);
    return;
  }

  let jobs = [];
  try {
    jobs = await api.jobs();
  } catch {
    jobs = [];
  }
  // Отрисовываем от старых к новым: каждая новая карточка встаёт наверх.
  for (const job of jobs.slice().reverse()) upsert(job);
  for (const job of jobs) {
    if (job.status === "queued" || job.status === "running") watch(job.id);
  }

  const wanted = new URLSearchParams(location.search).get("job");
  if (wanted) {
    // Пришли из «Поделиться»: файл уже принят, покажем его карточку.
    try {
      upsert(await api.job(wanted));
      watch(wanted);
    } catch {
      /* задача могла не создаться — список выше уже всё показал */
    }
    history.replaceState(null, "", "/");
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}

let installPrompt = null;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  ui.install.hidden = false;
});
ui.install.addEventListener("click", async () => {
  if (!installPrompt) return;
  ui.install.hidden = true;
  installPrompt.prompt();
  installPrompt = null;
});

document.addEventListener("visibilitychange", () => {
  // Вернулись в приложение после сна — перечитываем то, что могли пропустить.
  if (document.visibilityState !== "visible") return;
  for (const [id, job] of state.jobs) {
    if (job.status === "queued" || job.status === "running") watch(id);
  }
});

boot();
