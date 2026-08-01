/*
 * Браузерный смоук виджета.
 *
 * Питоновские тесты проверяют HTTP и базу, но виджет — это код, который
 * исполняется на чужой странице. Всё, что тут проверяется, руками ловилось
 * как настоящие баги: невидимая форма входа, потерянный ключ у явного
 * контейнера, протёкшая изоляция стилей.
 *
 * Запуск:
 *   npm install playwright            (или npm ci в CI)
 *   node tests/browser/smoke.mjs http://127.0.0.1:8099 КЛЮЧ ТОКЕН
 */

import { chromium } from 'playwright';
import { writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [BASE, KEY, TOKEN] = process.argv.slice(2);
if (!BASE || !KEY || !TOKEN) {
  console.error('нужны аргументы: <адрес> <ключ сайта> <токен админки>');
  process.exit(2);
}

const EXECUTABLE = process.env.PLAYWRIGHT_CHROMIUM_PATH || undefined;
const browser = await chromium.launch(EXECUTABLE ? { executablePath: EXECUTABLE } : {});
const results = [];
let failed = 0;

async function check(name, fn) {
  const page = await browser.newPage({ viewport: { width: 1180, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().includes('favicon')) errors.push(`console: ${m.text()}`);
  });
  try {
    await fn(page);
    if (errors.length) throw new Error(`ошибки в консоли:\n    ${errors.join('\n    ')}`);
    results.push(`  ok   ${name}`);
  } catch (error) {
    failed += 1;
    results.push(`  ПАДАЕТ ${name}\n    ${error.message}`);
  } finally {
    await page.close();
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

/** Дожидается, пока виджет построит карусель внутри Shadow DOM. */
async function widgetState(page) {
  await page.waitForFunction(() => {
    const host = document.querySelector('[data-reviews-widget-root]');
    return host && host.shadowRoot && host.shadowRoot.querySelector('.rw-card');
  }, null, { timeout: 15000 });
  return page.evaluate(() => {
    const root = document.querySelector('[data-reviews-widget-root]').shadowRoot;
    const card = root.querySelector('.rw-card');
    const text = root.querySelector('.rw-text');
    return {
      cards: root.querySelectorAll('.rw-card').length,
      dots: root.querySelectorAll('.rw-dot').length,
      grid: !!root.querySelector('.rw-grid'),
      summary: (root.querySelector('.rw-summary') || {}).textContent || '',
      cardWidth: parseFloat(getComputedStyle(card).width),
      cardBackground: getComputedStyle(card).backgroundColor,
      textColor: getComputedStyle(text).color,
      fontFamily: getComputedStyle(text).fontFamily,
      hasFormButton: !!root.querySelector('.rw-btn-primary'),
    };
  });
}

/* ------------------------------------------------------------- проверки */

await check('виджет встаёт на страницу и строит карусель', async (page) => {
  await page.goto(`${BASE}/preview/${KEY}`, { waitUntil: 'networkidle' });
  const state = await widgetState(page);
  assert(state.cards > 3, `карточек всего ${state.cards}`);
  assert(state.dots > 1, `точек пагинации ${state.dots}`);
  assert(/\d/.test(state.summary), `в шапке нет оценки: "${state.summary}"`);
  assert(state.hasFormButton, 'нет кнопки «Оставить отзыв»');
});

await check('стрелка листает карусель', async (page) => {
  await page.goto(`${BASE}/preview/${KEY}`, { waitUntil: 'networkidle' });
  await widgetState(page);
  const moved = await page.evaluate(async () => {
    const root = document.querySelector('[data-reviews-widget-root]').shadowRoot;
    const track = root.querySelector('.rw-track');
    const before = track.scrollLeft;
    root.querySelector('.rw-next').click();
    await new Promise((done) => setTimeout(done, 900));
    return { before, after: track.scrollLeft };
  });
  assert(moved.after > moved.before, `трек не сдвинулся: ${JSON.stringify(moved)}`);
});

await check('на узком экране карточка одна в ряд', async (page) => {
  await page.setViewportSize({ width: 390, height: 760 });
  await page.goto(`${BASE}/preview/${KEY}`, { waitUntil: 'networkidle' });
  const state = await widgetState(page);
  assert(state.cardWidth > 250 && state.cardWidth < 390,
    `ширина карточки ${state.cardWidth} при экране 390`);
});

await check('стили сайта не достают до виджета и наоборот', async (page) => {
  // Страница с глобальным !important — на такой изоляция протекала.
  const dir = mkdtempSync(join(tmpdir(), 'rw-smoke-'));
  const file = join(dir, 'hostile.html');
  writeFileSync(file, `<!doctype html><meta charset="utf-8">
    <style>
      * { color: red !important; font-family: cursive !important; font-size: 31px !important; }
      article, p, div { background: yellow !important; border: 4px dashed lime !important; }
    </style>
    <h1>Чужой сайт с агрессивным CSS</h1>
    <script src="${BASE}/embed.js" data-site="${KEY}" data-theme="light" async></script>
    <p id="after">Абзац после виджета</p>`);

  await page.goto(`file://${file}`, { waitUntil: 'networkidle' });
  const state = await widgetState(page);
  assert(state.cardBackground === 'rgb(255, 255, 255)',
    `фон карточки перекрасили: ${state.cardBackground}`);
  assert(state.textColor !== 'rgb(255, 0, 0)', `цвет текста перекрасили: ${state.textColor}`);
  assert(!/cursive/.test(state.fontFamily), `шрифт перекрасили: ${state.fontFamily}`);

  const wrappers = await page.evaluate(() => {
    const host = document.querySelector('[data-reviews-widget-root]');
    return {
      host: getComputedStyle(host).backgroundColor,
      holder: getComputedStyle(host.parentElement).backgroundColor,
      // Страница должна остаться такой, какой её сделал владелец сайта.
      pageParagraph: getComputedStyle(document.getElementById('after')).backgroundColor,
    };
  });
  assert(wrappers.host === 'rgba(0, 0, 0, 0)', `обёртке виджета достался фон: ${wrappers.host}`);
  assert(wrappers.holder === 'rgba(0, 0, 0, 0)', `контейнеру достался фон: ${wrappers.holder}`);
  assert(wrappers.pageParagraph === 'rgb(255, 255, 0)',
    `виджет повлиял на страницу: ${wrappers.pageParagraph}`);
});

await check('ключ с тега script работает и с явным контейнером', async (page) => {
  const dir = mkdtempSync(join(tmpdir(), 'rw-smoke-'));
  const file = join(dir, 'container.html');
  writeFileSync(file, `<!doctype html><meta charset="utf-8">
    <h1>Виджет в своём месте страницы</h1>
    <div data-reviews-widget></div>
    <script src="${BASE}/embed.js" data-site="${KEY}" data-layout="grid" data-limit="6"
            async></script>`);
  await page.goto(`file://${file}`, { waitUntil: 'networkidle' });
  const state = await widgetState(page);
  assert(state.grid, 'data-layout=grid не применился');
  assert(state.cards === 6, `data-limit=6 не применился, карточек ${state.cards}`);
  const inPlace = await page.evaluate(() =>
    document.querySelector('[data-reviews-widget-root]')
      .parentElement.hasAttribute('data-reviews-widget'));
  assert(inPlace, 'виджет встал не в свой контейнер');
});

await check('отзыв проходит путь: форма → модерация → сайт', async (page) => {
  const marker = `Смоук-проверка ${Date.now()}: кофе горячий, счёт быстрый`;

  await page.goto(`${BASE}/preview/${KEY}`, { waitUntil: 'networkidle' });
  await widgetState(page);
  await page.locator('[data-reviews-widget-root]').locator('.rw-btn-primary').click();
  const form = page.frameLocator('.rw-modal iframe');
  await form.locator('.star').nth(4).click();
  await form.locator('#text').fill(marker);
  await form.locator('#author').fill('Смоук-гость');
  // Антибот отклоняет форму, заполненную быстрее трёх секунд.
  await page.waitForTimeout(3300);
  await form.locator('#submit').click();
  await form.locator('#done').waitFor({ state: 'visible', timeout: 10000 });

  // Отзыв на модерации — на сайте его пока нет.
  const beforeModeration = await page.evaluate(async (args) => {
    const response = await fetch(`${args.base}/api/v1/widget?site=${args.key}&_=${Date.now()}`);
    const data = await response.json();
    return data.reviews.some((review) => review.text.includes(args.marker));
  }, { base: BASE, key: KEY, marker });
  assert(!beforeModeration, 'непроверенный отзыв уже виден на сайте');

  // Владелец публикует его в кабинете.
  await page.goto(`${BASE}/admin`, { waitUntil: 'networkidle' });
  await page.fill('#gateSite', KEY);
  await page.fill('#gateToken', TOKEN);
  await page.click('#gateForm button');
  await page.locator('#app').waitFor({ state: 'visible', timeout: 10000 });
  const gateHidden = await page.evaluate(() =>
    getComputedStyle(document.getElementById('gate')).display === 'none');
  assert(gateHidden, 'форма входа осталась видимой после входа');

  const card = page.locator('.card', { hasText: marker });
  await card.waitFor({ timeout: 10000 });
  await card.locator('[data-act="publish"]').click();
  await page.waitForTimeout(1200);

  const afterModeration = await page.evaluate(async (args) => {
    const response = await fetch(`${args.base}/api/v1/widget?site=${args.key}&_=${Date.now()}`);
    const data = await response.json();
    return data.reviews.some((review) => review.text.includes(args.marker));
  }, { base: BASE, key: KEY, marker });
  assert(afterModeration, 'опубликованный отзыв не появился в данных виджета');
});

await check('без опубликованных отзывов виджет ничего не оставляет', async (page) => {
  // Ключ есть, отзывов нет: пустой блок на странице клиента недопустим.
  const empty = process.env.WIDGET_EMPTY_KEY;
  if (!empty) {
    throw new Error('не задан WIDGET_EMPTY_KEY — нечем проверить пустой сайт');
  }
  await page.goto(`${BASE}/preview/${empty}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  const leftovers = await page.evaluate(() => ({
    roots: document.querySelectorAll('[data-reviews-widget-root]').length,
    holders: document.querySelectorAll('[data-rw-auto]').length,
    text: document.body.innerText.trim(),
  }));
  assert(leftovers.roots === 0 && leftovers.holders === 0,
    `виджет оставил разметку: ${JSON.stringify(leftovers)}`);
  assert(leftovers.text === '', `виджет оставил текст: "${leftovers.text}"`);
});

await browser.close();

console.log(results.join('\n'));
console.log(failed ? `\nпровалено проверок: ${failed}` : '\nвсе браузерные проверки прошли');
process.exit(failed ? 1 : 0);
