/* Приложение целиком: камера, сжатие снимка, отправка, живой список карточек
   с текстом, который можно править, копировать и отправлять.
   Ванильный JS без сборки — так проще править и нечему протухать.

   Три вещи стоит объяснить заранее:

   * Фото сжимается на телефоне до отправки. Снимок с камеры весит 3–8 МБ и
     имеет 4000 px по длинной стороне, а движку хватает 2000 px: так и трафик
     в разы меньше, и HEIC с iPhone по дороге превращается в JPEG, который
     понимают оба движка.
   * Отправка идёт через XMLHttpRequest, а не fetch, ради upload.progress:
     на мобильной сети даже полмегабайта уходят не мгновенно.
   * За результатом следит EventSource; если он не поднялся (старый браузер,
     сеть через прокси), включается опрос раз в две секунды. */

const api = {
  config: () => fetch("/api/config").then((r) => r.json()),
  jobs: () => fetch("/api/jobs").then((r) => r.json()),
  job: (id) => fetch(`/api/jobs/${id}`).then((r) => r.json()),
  remove: (id) => fetch(`/api/jobs/${id}`, { method: "DELETE" }).then((r) => r.json()),
  save: (id, text) =>
    fetch(`/api/jobs/${id}/text`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    }).then((r) => r.json()),
};

const ui = {
  camera: document.getElementById("camera"),
  gallery: document.getElementById("gallery"),
  engine: document.getElementById("engine"),
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
  editing: new Set(), // карточки, в которых сейчас правят текст
  saveTimers: new Map(),
  maxUpload: 15 * 1024 * 1024,
  accept: ["image/jpeg", "image/png", "image/webp"],
};

const MAX_SIDE = 2000;
const JPEG_QUALITY = 0.85;

const STATUS = {
  queued: "в очереди",
  running: "распознаю…",
  done: "готово",
  failed: "ошибка",
  cancelled: "отменено",
};

const ENGINE = { claude: "Claude", yandex: "Яндекс", fake: "заглушка" };

/* --- мелкие помощники ---------------------------------------------------- */

function size(bytes) {
  if (!bytes) return "";
  const units = ["Б", "КБ", "МБ"];
  let value = bytes;
  let step = 0;
  while (value >= 1024 && step < units.length - 1) {
    value /= 1024;
    step += 1;
  }
  return `${value >= 10 || step === 0 ? Math.round(value) : value.toFixed(1)} ${units[step]}`;
}

function when(seconds) {
  if (!seconds) return "";
  const date = new Date(seconds * 1000);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  const time = date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return sameDay ? time : `${date.toLocaleDateString("ru-RU", { day: "numeric", month: "short" })}, ${time}`;
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

/* --- сжатие снимка ------------------------------------------------------- */

async function decode(file) {
  // createImageBitmap умеет учитывать EXIF-поворот — иначе снимок с телефона
  // придёт лежащим на боку, и движок будет читать текст поперёк.
  if ("createImageBitmap" in window) {
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      /* Safari постарше не знает imageOrientation, но сам поворачивает при рисовании */
    }
  }
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("не удалось прочитать фото"));
    };
    image.src = url;
  });
}

async function shrink(file) {
  let source;
  try {
    source = await decode(file);
  } catch {
    // Браузер не смог прочитать файл (например, HEIC в Chrome на Android).
    // Если формат из тех, что сервер принимает, — отправляем как есть.
    if (state.accept.includes(file.type)) return file;
    throw new Error("не удалось прочитать это фото — попробуйте снять заново");
  }
  const width = source.naturalWidth || source.width;
  const height = source.naturalHeight || source.height;
  const scale = Math.min(1, MAX_SIDE / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
  if (source.close) source.close();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
  if (!blob) throw new Error("не удалось сжать фото");
  return blob;
}

/* --- отправка ------------------------------------------------------------ */

function send(blob, name) {
  if (blob.size > state.maxUpload) {
    toast(`«${name}» больше ${size(state.maxUpload)} — столько сервер не примет`, true);
    return;
  }
  const row = document.createElement("div");
  row.className = "upload";
  const label = document.createElement("span");
  label.textContent = name;
  const percent = document.createElement("span");
  percent.className = "pct";
  percent.textContent = "0%";
  row.append(label, percent);
  ui.uploads.append(row);
  ui.uploads.hidden = false;

  const request = new XMLHttpRequest();
  request.open("POST", `/api/jobs?name=${encodeURIComponent(name)}`);
  request.setRequestHeader("Content-Type", blob.type || "application/octet-stream");

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
      toast(payload.error || `не удалось отправить «${name}»`, true);
      return;
    }
    upsert(payload);
    watch(payload.id);
  });
  request.addEventListener("error", () => {
    finish();
    toast(`сеть оборвалась на «${name}»`, true);
  });
  request.addEventListener("abort", finish);
  request.send(blob);
}

async function pick(input) {
  const files = Array.from(input.files);
  input.value = "";
  for (const file of files) {
    const name = file.name || `фото ${when(Date.now() / 1000)}.jpg`;
    try {
      send(await shrink(file), name.replace(/\.(heic|heif|png|webp)$/i, ".jpg"));
    } catch (error) {
      toast(error.message || "не удалось подготовить фото", true);
    }
  }
}

ui.camera.addEventListener("change", () => pick(ui.camera));
ui.gallery.addEventListener("change", () => pick(ui.gallery));

/* --- карточки ------------------------------------------------------------ */

function describe(job) {
  if (job.status === "failed") return job.error || "ошибка";
  if (job.status === "done") return job.edited ? "готово · с правками" : "готово";
  return STATUS[job.status] || job.status;
}

function render(job) {
  let node = ui.jobs.querySelector(`[data-id="${job.id}"]`);
  if (!node) {
    node = ui.template.content.firstElementChild.cloneNode(true);
    node.dataset.id = job.id;
    node.querySelector(".job-remove").addEventListener("click", () => remove(job.id));
    node.querySelector(".job-cancel").addEventListener("click", () => remove(job.id));
    node.querySelector(".job-copy").addEventListener("click", () => copyText(job.id));
    node.querySelector(".job-share").addEventListener("click", () => shareText(job.id));
    const area = node.querySelector(".job-text");
    area.addEventListener("focus", () => state.editing.add(job.id));
    area.addEventListener("input", () => scheduleSave(job.id, area));
    area.addEventListener("blur", () => {
      state.editing.delete(job.id);
      flushSave(job.id, area);
    });
    const thumb = node.querySelector(".job-thumb");
    thumb.src = `/api/jobs/${job.id}/image`;
    thumb.hidden = false;
    thumb.addEventListener("error", () => {
      thumb.hidden = true;
    });
    ui.jobs.prepend(node);
  }

  node.className = `job ${job.status}`;
  if (job.status === "running") node.classList.add("pulsing");

  node.querySelector(".job-name").textContent = job.name;
  node.querySelector(".job-meta").textContent = [
    ENGINE[job.engine] || job.engine,
    size(job.size),
    when(job.finished || job.created),
  ].filter(Boolean).join(" · ");
  const bar = node.querySelector(".bar > i");
  bar.style.width = job.status === "queued" ? "8%" : job.status === "running" ? "60%" : "100%";
  node.querySelector(".job-status").textContent = describe(job);

  const done = job.status === "done";
  const active = job.status === "queued" || job.status === "running";
  const area = node.querySelector(".job-text");
  area.hidden = !done;
  area.readOnly = !done;
  // Пока человек правит текст, сервер его не перебивает.
  if (done && !state.editing.has(job.id) && area.value !== job.text) {
    area.value = job.text;
    grow(area);
  }
  node.querySelector(".job-copy").hidden = !done;
  node.querySelector(".job-share").hidden = !done;
  node.querySelector(".job-cancel").hidden = !active;
}

function grow(area) {
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight + 4, 600)}px`;
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
  if (active && !confirm("Отменить распознавание?")) return;
  if (!active && !confirm("Удалить фото и текст?")) return;
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

/* --- правка текста ------------------------------------------------------- */

function savedLabel(id) {
  return ui.jobs.querySelector(`[data-id="${id}"] .job-saved`);
}

function scheduleSave(id, area) {
  grow(area);
  clearTimeout(state.saveTimers.get(id));
  const label = savedLabel(id);
  if (label) {
    label.textContent = "сохраняю…";
    label.className = "job-saved";
    label.hidden = false;
  }
  // Не после каждой буквы: телефон печатает медленно, а запросов было бы сотни.
  state.saveTimers.set(id, setTimeout(() => flushSave(id, area), 800));
}

async function flushSave(id, area) {
  clearTimeout(state.saveTimers.get(id));
  state.saveTimers.delete(id);
  const job = state.jobs.get(id);
  if (!job || area.value === job.text) {
    const label = savedLabel(id);
    if (label && label.textContent === "сохраняю…") label.hidden = true;
    return;
  }
  const label = savedLabel(id);
  try {
    const saved = await api.save(id, area.value);
    if (saved.error) throw new Error(saved.error);
    state.jobs.set(id, saved);
    if (label) {
      label.textContent = "сохранено";
      label.className = "job-saved";
      label.hidden = false;
      setTimeout(() => {
        if (label.textContent === "сохранено") label.hidden = true;
      }, 2000);
    }
    const status = ui.jobs.querySelector(`[data-id="${id}"] .job-status`);
    if (status) status.textContent = describe(saved);
  } catch (error) {
    if (label) {
      label.textContent = `не сохранилось: ${error.message || "нет связи"}`;
      label.className = "job-saved bad";
      label.hidden = false;
    }
  }
}

/* --- копировать и поделиться --------------------------------------------- */

function currentText(id) {
  const area = ui.jobs.querySelector(`[data-id="${id}"] .job-text`);
  return area ? area.value : (state.jobs.get(id) || {}).text || "";
}

async function copyText(id) {
  const text = currentText(id);
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("скопировано");
  } catch {
    // Старый браузер или страница без HTTPS: буфер обмена закрыт, выделяем
    // текст, чтобы человек скопировал сам.
    const area = ui.jobs.querySelector(`[data-id="${id}"] .job-text`);
    if (area) {
      area.focus();
      area.select();
    }
    toast("выделено — нажмите «Копировать» в меню");
  }
}

async function shareText(id) {
  const text = currentText(id);
  const job = state.jobs.get(id);
  if (!text) return;
  if (!navigator.share) return copyText(id);
  try {
    await navigator.share({ title: job ? job.name : "Рукопись", text });
  } catch (error) {
    if (error && error.name !== "AbortError") copyText(id);
  }
}

/* --- слежение за результатом --------------------------------------------- */

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
      if (job.status === "done") toast("распознано");
    }
  });
  stream.addEventListener("error", () => {
    // Соединение оборвалось — добираем состояние опросом.
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
    const config = await api.config();
    state.maxUpload = config.maxUpload || state.maxUpload;
    state.accept = config.accept || state.accept;
    const engine = ENGINE[config.engine] || config.engine;
    ui.engine.textContent = `Распознаёт ${engine}; результаты хранятся ${Math.round(config.keepHours)} ч.`;
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
  for (const job of jobs.slice().reverse()) upsert(job);
  for (const job of jobs) {
    if (job.status === "queued" || job.status === "running") watch(job.id);
  }

  const params = new URLSearchParams(location.search);
  const wanted = params.get("job");
  const failed = params.get("error");
  if (wanted) {
    // Пришли из «Поделиться»: фото уже принято, покажем его карточку.
    try {
      upsert(await api.job(wanted));
      watch(wanted);
    } catch {
      /* задача могла не создаться — список выше уже всё показал */
    }
  }
  if (failed) toast(failed, true);
  if (wanted || failed) history.replaceState(null, "", "/");

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
