/* Логика формы: собрать состояние, посчитать итоги, отправить на сервер за PDF.
   Итоги считаются в копейках целыми числами — ровно как в models.py, иначе
   цифра на экране и цифра в файле рано или поздно разойдутся. */

'use strict';

const form = document.getElementById('form');
const itemsBody = document.getElementById('items');
const sectionsBox = document.getElementById('sections');
const banner = document.getElementById('banner');
const previewCard = document.querySelector('.preview-card');
const previewFrame = document.getElementById('previewFrame');

const logos = { sender: null, client: null };
let previewUrl = null;
let bannerTimer = null;
/** Какой черновик открыт: отправлять можно только сохранённое. */
let currentSlug = '';
let publicUrl = '';

const CURRENCIES = {
  RUB: '₽', USD: '$', EUR: '€', KZT: '₸', BYN: 'Br', UAH: '₴',
};

const SWATCHES = ['#1f5eff', '#0f766e', '#b42318', '#7c3aed', '#c2410c', '#0b7285', '#1f2937'];

/* --- мелкие утилиты --------------------------------------------------------- */

const $ = (name) => form.elements[name];

function num(value) {
  const text = String(value ?? '').replace(/\s/g, '').replace(',', '.').replace(/[^\d.\-]/g, '');
  const parsed = parseFloat(text);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Рубли → копейки. round(x*100) — единственный способ не поймать 0.1+0.2. */
const cents = (value) => Math.round(num(value) * 100);

function formatCents(value, currency) {
  const sign = value < 0 ? '−' : '';
  const whole = Math.floor(Math.abs(value) / 100);
  const rest = String(Math.abs(value) % 100).padStart(2, '0');
  const grouped = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  const symbol = CURRENCIES[currency] || currency || '';
  return `${sign}${grouped},${rest} ${symbol}`;
}

function show(message, kind = 'ok') {
  clearTimeout(bannerTimer);
  banner.textContent = message;
  banner.dataset.kind = kind;
  banner.hidden = !message;
  if (message && kind === 'ok') bannerTimer = setTimeout(() => { banner.hidden = true; }, 4000);
}

/* --- позиции ---------------------------------------------------------------- */

/** «120000.00» → «120000», «1.500» → «1,5». В форме Decimal-хвосты только мешают.
    Дробная часть в шаблоне обязательна — иначе «1000» превратилось бы в «1». */
function pretty(value) {
  const text = String(value ?? '').trim();
  if (!/^-?\d+[.,]\d+$/.test(text)) return text;
  return text.replace(',', '.').replace(/0+$/, '').replace(/\.$/, '').replace('.', ',');
}

function addItem(data = {}) {
  const row = document.getElementById('itemRow').content.firstElementChild.cloneNode(true);
  for (const field of row.querySelectorAll('[data-field]')) {
    const value = data[field.dataset.field];
    if (value === undefined || value === null) continue;
    field.value = field.classList.contains('num') ? pretty(value) : value;
  }
  row.querySelector('.remove').addEventListener('click', () => {
    row.remove();
    recalc();
  });
  makeDraggable(row);
  itemsBody.append(row);
  autosize(row.querySelector('.item-desc'));
  return row;
}

function readItems() {
  return [...itemsBody.querySelectorAll('.item')].map((row) => {
    const item = {};
    for (const field of row.querySelectorAll('[data-field]')) item[field.dataset.field] = field.value;
    return item;
  });
}

/** Перетаскивание строк: порядок позиций в смете обычно правят уже по ходу. */
function makeDraggable(row) {
  const handle = row.querySelector('.handle');
  handle.addEventListener('mousedown', () => { row.draggable = true; });
  row.addEventListener('dragstart', () => row.classList.add('is-dragging'));
  row.addEventListener('dragend', () => {
    row.classList.remove('is-dragging');
    row.draggable = false;
    recalc();
  });
}

itemsBody.addEventListener('dragover', (event) => {
  event.preventDefault();
  const dragging = itemsBody.querySelector('.is-dragging');
  if (!dragging) return;
  const after = [...itemsBody.querySelectorAll('.item:not(.is-dragging)')].find((row) => {
    const box = row.getBoundingClientRect();
    return event.clientY < box.top + box.height / 2;
  });
  itemsBody.insertBefore(dragging, after || null);
});

function autosize(area) {
  if (!area) return;
  area.style.height = 'auto';
  area.style.height = `${Math.min(area.scrollHeight, 160)}px`;
}

/* --- дополнительные блоки --------------------------------------------------- */

function addSection(data = {}) {
  const box = document.getElementById('sectionRow').content.firstElementChild.cloneNode(true);
  for (const field of box.querySelectorAll('[data-field]')) {
    if (data[field.dataset.field]) field.value = data[field.dataset.field];
  }
  box.querySelector('.remove').addEventListener('click', () => box.remove());
  sectionsBox.append(box);
  return box;
}

function readSections() {
  return [...sectionsBox.querySelectorAll('.section-row')].map((box) => {
    const block = {};
    for (const field of box.querySelectorAll('[data-field]')) block[field.dataset.field] = field.value;
    return block;
  });
}

/* --- состояние -------------------------------------------------------------- */

function collect() {
  const party = (prefix) => ({
    name: $(`${prefix}.name`).value,
    person: $(`${prefix}.person`).value,
    position: $(`${prefix}.position`).value,
    phone: $(`${prefix}.phone`)?.value || '',
    email: $(`${prefix}.email`)?.value || '',
    site: $(`${prefix}.site`)?.value || '',
    address: $(`${prefix}.address`).value,
    details: $(`${prefix}.details`).value,
    logo: logos[prefix] || '',
  });

  return {
    number: $('number').value,
    issued_at: $('issued_at').value,
    valid_days: $('valid_days').value,
    subject: $('subject').value,
    intro: $('intro').value,
    sender: party('sender'),
    client: party('client'),
    items: readItems(),
    sections: readSections(),
    currency: $('currency').value,
    vat_mode: $('vat_mode').value,
    vat_rate: $('vat_rate').value,
    discount_percent: $('discount_percent').value,
    lead_time: $('lead_time').value,
    payment_terms: $('payment_terms').value,
    notes: $('notes').value,
    accent: $('accent').value,
    template: $('template').value,
    show_signature: $('show_signature').checked,
  };
}

function apply(data) {
  const set = (name, value) => { if ($(name)) $(name).value = value ?? ''; };

  set('number', data.number);
  set('issued_at', (data.issued_at || '').slice(0, 10));
  set('valid_days', data.valid_days ?? 14);
  set('subject', data.subject);
  set('intro', data.intro);
  set('currency', data.currency || 'RUB');
  set('vat_mode', data.vat_mode || 'none');
  set('vat_rate', pretty(data.vat_rate ?? 20));
  set('discount_percent', pretty(data.discount_percent ?? 0));
  set('lead_time', data.lead_time);
  set('payment_terms', data.payment_terms);
  set('notes', data.notes);
  set('accent', data.accent || '#1f5eff');
  if (data.template) set('template', data.template);
  $('show_signature').checked = data.show_signature !== false;

  for (const prefix of ['sender', 'client']) {
    const party = data[prefix] || {};
    for (const field of ['name', 'person', 'position', 'phone', 'email', 'site', 'address', 'details']) {
      set(`${prefix}.${field}`, party[field]);
    }
    setLogo(prefix, party.logo ? asDataUrl(party.logo) : null);
  }

  itemsBody.innerHTML = '';
  (data.items || []).forEach(addItem);
  if (!itemsBody.children.length) addItem();

  sectionsBox.innerHTML = '';
  (data.sections || []).forEach(addSection);

  // Адрес для отправки подставляем из карточки заказчика — почти всегда он же.
  const sendTo = document.getElementById('sendTo');
  if (sendTo && !sendTo.value) sendTo.value = (data.client || {}).email || '';

  applyAccent();
  recalc();
}

/** Черновик хранит логотип голым base64, а <img> нужен полноценный data-URL. */
function asDataUrl(value) {
  if (!value) return null;
  if (value.startsWith('data:')) return value;
  const type = value.startsWith('/9j/') ? 'image/jpeg' : 'image/png';
  return `data:${type};base64,${value}`;
}

/* --- расчёты ---------------------------------------------------------------- */

function totals() {
  const currency = $('currency').value;
  const vatMode = $('vat_mode').value;
  const vatRate = num($('vat_rate').value);
  const generalDiscount = num($('discount_percent').value);

  let gross = 0;
  let itemDiscount = 0;

  for (const row of itemsBody.querySelectorAll('.item')) {
    const read = (field) => row.querySelector(`[data-field="${field}"]`).value;
    const line = Math.round(num(read('quantity')) * cents(read('price')));
    const off = Math.round((line * num(read('discount_percent'))) / 100);
    gross += line;
    itemDiscount += off;
    row.querySelector('[data-role="sum"]').textContent = formatCents(line - off, currency);
  }

  const subtotal = gross - itemDiscount;
  const discount = Math.round((subtotal * generalDiscount) / 100);
  const net = subtotal - discount;

  let vat = 0;
  let total = net;
  if (vatMode === 'added') {
    vat = Math.round((net * vatRate) / 100);
    total = net + vat;
  } else if (vatMode === 'included') {
    vat = Math.round((net * vatRate) / (100 + vatRate));
  }

  return { currency, gross, itemDiscount, subtotal, discount, net, vat, total, vatMode, vatRate };
}

function recalc() {
  const sums = totals();
  const list = document.getElementById('totals');
  const rows = [];

  if (sums.itemDiscount || sums.discount) {
    rows.push(['Сумма по позициям', sums.gross, '']);
  }
  if (sums.itemDiscount) rows.push(['Скидки по позициям', -sums.itemDiscount, '']);
  if (sums.discount) {
    rows.push([`Скидка ${trim($('discount_percent').value)}%`, -sums.discount, '']);
  }
  if (sums.vatMode === 'added') {
    rows.push(['Без НДС', sums.net, '']);
    rows.push([`НДС ${trim($('vat_rate').value)}%`, sums.vat, '']);
  } else if (sums.vatMode === 'included') {
    rows.push([`В том числе НДС ${trim($('vat_rate').value)}%`, sums.vat, '']);
  }
  rows.push(['К оплате', sums.total, 'row-total']);

  list.innerHTML = '';
  for (const [caption, value, cls] of rows) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = caption;
    dd.textContent = formatCents(value, sums.currency);
    if (cls) { dt.className = cls; dd.className = cls; }
    list.append(dt, dd);
  }

  document.getElementById('inWords').textContent =
    sums.vatMode === 'none' ? 'НДС не облагается' : '';
}

const trim = (value) => String(num(value)).replace('.', ',');

/* --- логотипы --------------------------------------------------------------- */

function setLogo(which, dataUrl) {
  const drop = document.querySelector(`[data-logo="${which}"]`);
  const img = drop.querySelector('img');
  const clear = drop.querySelector('.logo-drop__clear');
  logos[which] = dataUrl || null;
  img.hidden = !dataUrl;
  clear.hidden = !dataUrl;
  drop.classList.toggle('has-logo', Boolean(dataUrl));
  if (dataUrl) img.src = dataUrl;
}

function readLogoFile(which, file) {
  if (!file) return;
  if (!/^image\/(png|jpeg)$/.test(file.type)) {
    show('Логотип нужен в PNG или JPEG. SVG сначала сохраните картинкой.', 'error');
    return;
  }
  if (file.size > 6 * 1024 * 1024) {
    show('Файл больше 6 МБ — уменьшите логотип.', 'error');
    return;
  }
  const reader = new FileReader();
  reader.onload = () => setLogo(which, String(reader.result));
  reader.onerror = () => show('Не удалось прочитать файл логотипа.', 'error');
  reader.readAsDataURL(file);
}

for (const drop of document.querySelectorAll('.logo-drop')) {
  const which = drop.dataset.logo;
  const picker = drop.querySelector('input[type="file"]');

  drop.addEventListener('click', (event) => {
    if (!event.target.closest('.logo-drop__clear')) picker.click();
  });
  picker.addEventListener('change', () => readLogoFile(which, picker.files[0]));
  drop.querySelector('.logo-drop__clear').addEventListener('click', () => {
    setLogo(which, null);
    picker.value = '';
  });

  drop.addEventListener('dragover', (event) => {
    event.preventDefault();
    drop.classList.add('is-over');
  });
  drop.addEventListener('dragleave', () => drop.classList.remove('is-over'));
  drop.addEventListener('drop', (event) => {
    event.preventDefault();
    drop.classList.remove('is-over');
    readLogoFile(which, event.dataTransfer.files[0]);
  });
}

/* --- обращения к серверу ---------------------------------------------------- */

async function buildPdf() {
  const response = await fetch('/api/render', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collect()),
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(problem.error || `сервер ответил ${response.status}`);
  }
  const warnings = response.headers.get('X-Proposal-Warnings');
  if (warnings) show(decodeURIComponent(warnings), 'warn');
  const name = (response.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
  return { blob: await response.blob(), name: name ? name[1] : 'proposal.pdf' };
}

async function withBusy(button, action) {
  button.disabled = true;
  try {
    await action();
  } catch (error) {
    show(String(error.message || error), 'error');
  } finally {
    button.disabled = false;
  }
}

function download(button) {
  return withBusy(button, async () => {
    const { blob, name } = await buildPdf();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  });
}

document.getElementById('download').addEventListener('click', (e) => download(e.target));
document.getElementById('download2').addEventListener('click', (e) => download(e.target));

document.getElementById('preview').addEventListener('click', (event) => withBusy(event.target, async () => {
  const { blob } = await buildPdf();
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(blob);
  previewFrame.src = previewUrl;
  previewCard.classList.add('is-visible');
  previewCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}));

document.getElementById('loadDemo').addEventListener('click', (event) => withBusy(event.target, async () => {
  const response = await fetch('/api/demo');
  apply(await response.json());
  currentSlug = '';
  applyDelivery(null);
  show('Загружен пример — правьте под себя.');
}));

document.getElementById('saveDraft').addEventListener('click', (event) => withBusy(event.target, async () => {
  const slugField = document.getElementById('draftSlug');
  const slug = (slugField.value || suggestSlug()).trim();
  if (!slug) throw new Error('придумайте имя черновика');
  slugField.value = slug;
  const response = await fetch(`/api/drafts/${encodeURIComponent(slug)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(collect()),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'не сохранилось');
  currentSlug = slug;
  show(`Черновик «${slug}» сохранён.`);
  await Promise.all([refreshDrafts(), loadDelivery()]);
}));

function suggestSlug() {
  const source = `${$('client.name').value} ${$('number').value}`.toLowerCase();
  const map = { а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e', ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's', т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e', ю: 'yu', я: 'ya' };
  return [...source]
    .map((ch) => (map[ch] !== undefined ? map[ch] : (/[a-z0-9]/.test(ch) ? ch : '-')))
    .join('')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 48) || 'draft';
}

async function refreshDrafts() {
  const response = await fetch('/api/drafts');
  const { drafts } = await response.json();
  const list = document.getElementById('drafts');
  list.innerHTML = '';
  refreshFunnel();
  if (!drafts.length) {
    list.innerHTML = '<li class="muted">Пока пусто. Сохранённые предложения лягут сюда.</li>';
    return;
  }
  for (const draft of drafts) {
    const item = document.createElement('li');

    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'open';
    open.textContent = `№ ${draft.number} — ${draft.client || draft.subject || draft.slug}`;
    open.addEventListener('click', async () => {
      const loaded = await fetch(`/api/drafts/${encodeURIComponent(draft.slug)}`);
      if (!loaded.ok) { show('Черновик не открылся.', 'error'); return; }
      const data = await loaded.json();
      apply(data);
      currentSlug = draft.slug;
      document.getElementById('draftSlug').value = draft.slug;
      applyDelivery(data.delivery);
      show(`Открыт черновик «${draft.slug}».`);
    });

    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.dataset.status = draft.status;
    tag.textContent = draft.status_label;

    const when = document.createElement('span');
    when.className = 'when';
    when.textContent = draft.total;

    const drop = document.createElement('button');
    drop.type = 'button';
    drop.className = 'drop';
    drop.textContent = '×';
    drop.title = 'Удалить черновик';
    drop.addEventListener('click', async () => {
      if (!confirm(`Удалить черновик «${draft.slug}»?`)) return;
      await fetch(`/api/drafts/${encodeURIComponent(draft.slug)}`, { method: 'DELETE' });
      await refreshDrafts();
    });

    item.append(open, tag, when, drop);
    list.append(item);
  }
}

/* --- отправка и воронка ----------------------------------------------------- */

const sendState = document.getElementById('sendState');
const sendHint = document.getElementById('sendHint');

/** Показать состояние доставки открытого черновика. */
function applyDelivery(state) {
  const events = document.getElementById('events');
  events.innerHTML = '';

  if (!currentSlug) {
    sendState.textContent = 'Сначала сохраните черновик — отправляем именно сохранённое.';
    sendState.dataset.status = 'draft';
    sendHint.textContent = '';
    return;
  }
  if (!state || state.status === 'draft') {
    sendState.textContent = 'Ещё не отправляли.';
    sendState.dataset.status = 'draft';
  } else {
    sendState.textContent = `${capitalize(state.status_label)}${state.comment ? `: «${state.comment}»` : ''}`;
    sendState.dataset.status = state.status;
  }

  if (state && state.link) {
    sendHint.innerHTML = '';
    sendHint.append('Ссылка для заказчика: ');
    const link = document.createElement('a');
    link.href = state.link;
    link.textContent = state.link;
    link.target = '_blank';
    link.rel = 'noopener';
    sendHint.append(link);
  } else if (!publicUrl) {
    sendHint.textContent = 'Публичный адрес не задан: письмо уйдёт без ссылки, статусы «просмотрено» и «принято» отмечаться не будут.';
  } else {
    sendHint.textContent = '';
  }

  for (const event of (state && state.events) || []) {
    const row = document.createElement('li');
    const when = document.createElement('time');
    when.textContent = new Date(event.at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
    const what = document.createElement('span');
    what.textContent = event.detail ? `${event.label} — ${event.detail}` : event.label;
    row.append(when, what);
    events.append(row);
  }
}

const capitalize = (text) => (text ? text[0].toUpperCase() + text.slice(1) : '');

async function loadDelivery() {
  if (!currentSlug) { applyDelivery(null); return; }
  const response = await fetch(`/api/drafts/${encodeURIComponent(currentSlug)}`);
  if (!response.ok) { applyDelivery(null); return; }
  const data = await response.json();
  applyDelivery(data.delivery);
}

async function callSend(dryRun) {
  const response = await fetch('/api/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      slug: currentSlug,
      to: document.getElementById('sendTo').value,
      subject: document.getElementById('sendSubject').value,
      body: document.getElementById('sendBody').value,
      dry_run: dryRun,
    }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `сервер ответил ${response.status}`);
  return result;
}

document.getElementById('previewMail').addEventListener('click', (event) => withBusy(event.target, async () => {
  const result = await callSend(true);
  // Подставляем в поля то, что ушло бы, — дальше это можно править руками.
  document.getElementById('sendSubject').value = result.subject;
  document.getElementById('sendBody').value = result.body;
  document.querySelector('.send-more').open = true;
  show(`Так уйдёт письмо на ${result.to}. Ничего не отправлено.`);
  (result.warnings || []).forEach((warning) => show(warning, 'warn'));
}));

document.getElementById('sendMail').addEventListener('click', (event) => withBusy(event.target, async () => {
  const to = document.getElementById('sendTo').value || $('client.email').value;
  if (!confirm(`Отправить предложение на ${to}?`)) return;
  const result = await callSend(false);
  show(`Отправлено на ${result.to}.`);
  (result.warnings || []).forEach((warning) => show(warning, 'warn'));
  await Promise.all([loadDelivery(), refreshDrafts()]);
}));

async function refreshFunnel() {
  const response = await fetch('/api/funnel');
  if (!response.ok) return;
  const data = await response.json();
  const box = document.getElementById('funnel');
  box.innerHTML = '';
  if (!data.total) return;

  const rows = [
    ['Черновики', String(data.counts.draft)],
    ['Отправлено', String(data.delivered)],
    ['Просмотрено', String(data.counts.viewed + data.counts.accepted + data.counts.declined)],
    ['Принято', String(data.counts.accepted), true],
  ];
  if (data.delivered) rows.push(['Конверсия', `${data.conversion}%`, true]);
  for (const [currency, bucket] of Object.entries(data.money || {})) {
    if (bucket.accepted !== bucket.open) rows.push([`Ждёт ответа (${currency})`, bucket.open]);
    rows.push([`Принято на сумму`, bucket.accepted, true]);
  }

  for (const [caption, value, accent] of rows) {
    const dt = document.createElement('dt');
    const dd = document.createElement('dd');
    dt.textContent = caption;
    dd.textContent = value;
    if (accent) dd.className = 'accent';
    box.append(dt, dd);
  }
}

/* --- настройки почты -------------------------------------------------------- */

function applySmtp(smtp) {
  const set = (name, value) => {
    const field = document.querySelector(`[name="smtp.${name}"]`);
    if (field && value !== undefined && value !== null && value !== '') field.value = value;
  };
  set('host', smtp.host);
  set('port', smtp.port);
  set('user', smtp.user);
  set('sender', smtp.sender);
  set('ssl_mode', smtp.ssl_mode);

  document.getElementById('smtpSummary').textContent = smtp.configured
    ? `${smtp.host} — ${smtp.sender}`
    : 'не настроена — письма отправлять нечем';
  document.getElementById('smtpBox').open = !smtp.configured;
}

document.getElementById('saveSmtp').addEventListener('click', (event) => withBusy(event.target, async () => {
  const value = (name) => document.querySelector(`[name="smtp.${name}"]`).value;
  const response = await fetch('/api/smtp', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      host: value('host'),
      port: value('port'),
      user: value('user'),
      password: value('password'),
      sender: value('sender'),
      ssl_mode: value('ssl_mode'),
      check: true,
    }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'не сохранилось');
  document.querySelector('[name="smtp.password"]').value = '';
  applySmtp(result);
  if (result.check_error) throw new Error(result.check_error);
  show(result.check || 'Настройки почты сохранены.');
}));

/* --- оформление ------------------------------------------------------------- */

function applyAccent() {
  document.documentElement.style.setProperty('--accent', $('accent').value);
}

function buildSwatches() {
  const box = document.getElementById('swatches');
  for (const color of SWATCHES) {
    const button = document.createElement('button');
    button.type = 'button';
    button.style.background = color;
    button.title = color;
    button.addEventListener('click', () => {
      $('accent').value = color;
      applyAccent();
    });
    box.append(button);
  }
}

/* --- запуск ----------------------------------------------------------------- */

form.addEventListener('input', (event) => {
  if (event.target.matches('.item-desc')) autosize(event.target);
  if (event.target.name === 'accent') applyAccent();
  recalc();
});

document.getElementById('addItem').addEventListener('click', () => addItem().querySelector('.item-title').focus());
document.getElementById('addSection').addEventListener('click', () => addSection().querySelector('input').focus());

async function start() {
  buildSwatches();
  const response = await fetch('/api/state');
  const state = await response.json();

  const select = document.getElementById('template');
  for (const [key, caption] of Object.entries(state.templates)) {
    const option = document.createElement('option');
    option.value = key;
    option.textContent = caption;
    select.append(option);
  }

  document.getElementById('fontNote').textContent = state.font
    ? `шрифт: ${state.font}`
    : 'шрифт не найден';
  if (state.font_error) show(state.font_error, 'error');

  publicUrl = state.public_url || '';
  applySmtp(state.smtp || {});
  applyDelivery(null);

  $('issued_at').value = new Date().toISOString().slice(0, 10);
  $('number').value = state.next_number || '1';
  addItem();
  applyAccent();
  recalc();
  await refreshDrafts();
}

start().catch((error) => show(`Не удалось загрузить форму: ${error}`, 'error'));
