/* Дашборд отзывов: загрузка данных из /api и отрисовка графиков на чистом SVG. */

const SVG_NS = "http://www.w3.org/2000/svg";
const PAGE_SIZE = 20;

const SENTIMENTS = [
  { key: "positive", title: "Позитив", color: "var(--sent-positive)" },
  { key: "neutral", title: "Нейтрально", color: "var(--sent-neutral)" },
  { key: "negative", title: "Негатив", color: "var(--sent-negative)" },
];

const SOURCE_TITLES = {
  yandex: "Яндекс.Карты",
  "2gis": "2ГИС",
  wildberries: "Wildberries",
  ozon: "Ozon",
  file: "Импорт",
  demo: "Демо",
};

const MONTHS = ["янв", "фев", "мар", "апр", "май", "июн",
                "июл", "авг", "сен", "окт", "ноя", "дек"];

const state = {
  summary: null,
  sources: [],
  offset: 0,
  total: 0,
  loading: false,
};

const $ = (selector) => document.querySelector(selector);
const tooltip = $("#tooltip");

/* ------------------------------------------------------------------ утилиты */

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, value);
  }
  return node;
}

const nf = new Intl.NumberFormat("ru-RU");
const fmtInt = (value) => nf.format(Math.round(value || 0));
const fmtPct = (value) => `${Math.round((value || 0) * 100)}%`;
const fmtScore = (value) => (value > 0 ? "+" : "") + (value || 0).toFixed(2);

function monthLabel(month) {
  const [year, index] = month.split("-");
  return `${MONTHS[Number(index) - 1] || month} ${year.slice(2)}`;
}

function sourceTitle(name) {
  return SOURCE_TITLES[name] || name;
}

function stars(rating) {
  if (rating === null || rating === undefined) return "";
  const full = Math.round(rating);
  return "★".repeat(full) + "☆".repeat(Math.max(0, 5 - full));
}

/** Прямоугольник со скруглением только на «конце данных». */
function roundedBar(x, y, width, height, radius, side = "top") {
  const r = Math.max(0, Math.min(radius, width / 2, height));
  if (r === 0 || height <= 0 || width <= 0) return `M${x} ${y}h${width}v${height}h${-width}z`;
  if (side === "top") {
    return `M${x} ${y + height}V${y + r}a${r} ${r} 0 0 1 ${r} ${-r}h${width - 2 * r}` +
           `a${r} ${r} 0 0 1 ${r} ${r}V${y + height}z`;
  }
  if (side === "right") {
    return `M${x} ${y}h${width - r}a${r} ${r} 0 0 1 ${r} ${r}v${height - 2 * r}` +
           `a${r} ${r} 0 0 1 ${-r} ${r}H${x}z`;
  }
  if (side === "left") {
    return `M${x + width} ${y}H${x + r}a${r} ${r} 0 0 0 ${-r} ${r}v${height - 2 * r}` +
           `a${r} ${r} 0 0 0 ${r} ${r}h${width - r}z`;
  }
  return `M${x} ${y}h${width}v${height}h${-width}z`;
}

function showTooltip(event, html) {
  tooltip.innerHTML = html;
  tooltip.hidden = false;
  moveTooltip(event);
}

function moveTooltip(event) {
  const pad = 14;
  const box = tooltip.getBoundingClientRect();
  let left = event.clientX + pad;
  let top = event.clientY + pad;
  if (left + box.width > window.innerWidth - 8) left = event.clientX - box.width - pad;
  if (top + box.height > window.innerHeight - 8) top = event.clientY - box.height - pad;
  tooltip.style.left = `${Math.max(8, left)}px`;
  tooltip.style.top = `${Math.max(8, top)}px`;
}

function hideTooltip() {
  tooltip.hidden = true;
}

/** Навешивает всплывающую подсказку на элемент графика. */
function withTooltip(node, html) {
  node.classList.add("mark");
  node.addEventListener("mouseenter", (event) => showTooltip(event, html));
  node.addEventListener("mousemove", moveTooltip);
  node.addEventListener("mouseleave", hideTooltip);
  return node;
}

function tooltipRow(color, label, value) {
  return `<div class="tooltip__row"><span class="tooltip__swatch" style="background:${color}"></span>` +
         `<span>${label}</span><b style="margin-left:auto">${value}</b></div>`;
}

function legend(container, items) {
  const box = el("div", { class: "legend" });
  for (const item of items) {
    const entry = el("span", { class: "legend__item" });
    entry.append(el("span", { class: "legend__swatch", style: `background:${item.color}` }),
                 el("span", {}, item.title));
    box.append(entry);
  }
  container.append(box);
}

function emptyChart(container, message = "Нет данных за выбранный период") {
  container.replaceChildren(el("p", { class: "card__sub" }, message));
}

/* ------------------------------------------------------------------ графики */

/** Кольцевая диаграмма тональности с подписями долей. */
function renderSentimentDonut(container, summary) {
  container.replaceChildren();
  const total = summary.total;
  if (!total) return emptyChart(container);

  const size = 300, cx = 150, cy = 132, outer = 104, inner = 66;
  const root = svg("svg", { viewBox: `0 0 ${size} 220`, role: "img",
                            "aria-label": "Доли тональности отзывов" });

  let angle = -Math.PI / 2;
  const gap = 0.022; // визуальный зазор между сегментами

  for (const item of SENTIMENTS) {
    const value = summary.sentiment[item.key] || 0;
    if (!value) continue;
    const share = value / total;
    const sweep = share * Math.PI * 2;
    const start = angle + (share < 1 ? gap / 2 : 0);
    const end = angle + sweep - (share < 1 ? gap / 2 : 0);
    angle += sweep;

    const path = svg("path", { d: donutArc(cx, cy, outer, inner, start, end) });
    path.style.fill = item.color;
    withTooltip(path, `<strong>${item.title}</strong>` +
      tooltipRow(item.color, "Отзывов", fmtInt(value)) +
      tooltipRow(item.color, "Доля", fmtPct(share)));
    root.append(path);

    if (share >= 0.07) {
      const mid = (start + end) / 2;
      const radius = (outer + inner) / 2;
      const label = svg("text", {
        x: cx + Math.cos(mid) * radius,
        y: cy + Math.sin(mid) * radius + 4,
        "text-anchor": "middle",
        class: "value-label",
      });
      label.style.fill = "#fff";
      label.textContent = fmtPct(share);
      root.append(label);
    }
  }

  const center = svg("text", { x: cx, y: cy - 4, "text-anchor": "middle", class: "value-label" });
  center.style.fontSize = "26px";
  center.style.fill = "var(--text-primary)";
  center.textContent = fmtInt(total);
  const caption = svg("text", { x: cx, y: cy + 16, "text-anchor": "middle", class: "axis-label" });
  caption.textContent = "отзывов";
  root.append(center, caption);

  container.append(root);
  legend(container, SENTIMENTS.map((item) => ({
    title: `${item.title} — ${fmtInt(summary.sentiment[item.key] || 0)}`,
    color: item.color,
  })));
}

function donutArc(cx, cy, outer, inner, start, end) {
  const large = end - start > Math.PI ? 1 : 0;
  const x1 = cx + Math.cos(start) * outer, y1 = cy + Math.sin(start) * outer;
  const x2 = cx + Math.cos(end) * outer, y2 = cy + Math.sin(end) * outer;
  const x3 = cx + Math.cos(end) * inner, y3 = cy + Math.sin(end) * inner;
  const x4 = cx + Math.cos(start) * inner, y4 = cy + Math.sin(start) * inner;
  return `M${x1} ${y1}A${outer} ${outer} 0 ${large} 1 ${x2} ${y2}` +
         `L${x3} ${y3}A${inner} ${inner} 0 ${large} 0 ${x4} ${y4}Z`;
}

/** Распределение оценок 1–5: порядковая шкала одного тона. */
function renderRatings(container, summary) {
  container.replaceChildren();
  const values = [1, 2, 3, 4, 5].map((star) => summary.ratings[String(star)] || 0);
  const total = values.reduce((sum, value) => sum + value, 0);
  if (!total) return emptyChart(container, "Оценки в звёздах не пришли от источника");

  const width = 320, height = 200, padX = 16, padTop = 26, padBottom = 34;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
                            "aria-label": "Распределение оценок" });
  const max = Math.max(...values);
  const plotHeight = height - padTop - padBottom;
  const step = (width - padX * 2) / 5;
  const barWidth = step - 14;

  root.append(svg("line", { x1: padX, y1: height - padBottom, x2: width - padX,
                            y2: height - padBottom, class: "grid-line" }));

  values.forEach((value, index) => {
    const barHeight = max ? (value / max) * plotHeight : 0;
    const x = padX + step * index + (step - barWidth) / 2;
    const y = height - padBottom - barHeight;
    const bar = svg("path", { d: roundedBar(x, y, barWidth, barHeight, 4, "top") });
    bar.style.fill = `var(--ord-${index + 1})`;
    withTooltip(bar, `<strong>${index + 1} ★</strong>` +
      tooltipRow(`var(--ord-${index + 1})`, "Отзывов", fmtInt(value)) +
      tooltipRow(`var(--ord-${index + 1})`, "Доля", fmtPct(value / total)));
    root.append(bar);

    const label = svg("text", { x: x + barWidth / 2, y: y - 7, "text-anchor": "middle",
                                class: "value-label" });
    label.textContent = value ? fmtInt(value) : "";
    const axis = svg("text", { x: x + barWidth / 2, y: height - padBottom + 18,
                               "text-anchor": "middle", class: "axis-label" });
    axis.textContent = `${index + 1} ★`;
    root.append(label, axis);
  });

  container.append(root);
}

/** Помесячные столбики, разложенные по тональности. */
function renderTimeline(container, summary) {
  container.replaceChildren();
  const points = summary.timeline;
  if (!points.length) return emptyChart(container);

  const width = 720, height = 260, padLeft = 40, padRight = 16, padTop = 18, padBottom = 40;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
                            "aria-label": "Количество отзывов по месяцам" });
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;
  const max = Math.max(...points.map((point) => point.total), 1);
  const step = plotWidth / points.length;
  const barWidth = Math.max(6, Math.min(46, step - 8));
  const ticks = niceTicks(max, 4);

  for (const tick of ticks) {
    const y = padTop + plotHeight - (tick / ticks[ticks.length - 1]) * plotHeight;
    root.append(svg("line", { x1: padLeft, y1: y, x2: width - padRight, y2: y, class: "grid-line" }));
    const label = svg("text", { x: padLeft - 8, y: y + 4, "text-anchor": "end", class: "axis-label" });
    label.textContent = fmtInt(tick);
    root.append(label);
  }

  const scaleMax = ticks[ticks.length - 1];
  const labelStep = Math.ceil(points.length / Math.floor(plotWidth / 54));

  points.forEach((point, index) => {
    const x = padLeft + step * index + (step - barWidth) / 2;
    let cursor = padTop + plotHeight;
    const stackOrder = [...SENTIMENTS].reverse(); // негатив снизу, позитив сверху

    stackOrder.forEach((item, position) => {
      const value = point[item.key] || 0;
      if (!value) return;
      const segmentHeight = (value / scaleMax) * plotHeight;
      const isTop = position === stackOrder.length - 1 ||
        stackOrder.slice(position + 1).every((rest) => !point[rest.key]);
      const y = cursor - segmentHeight;
      const bar = svg("path", {
        d: roundedBar(x, y, barWidth, Math.max(1, segmentHeight - 2), 4, isTop ? "top" : "none"),
      });
      bar.style.fill = item.color;
      withTooltip(bar, `<strong>${monthLabel(point.month)}</strong>` +
        SENTIMENTS.map((s) => tooltipRow(s.color, s.title, fmtInt(point[s.key] || 0))).join("") +
        tooltipRow("transparent", "Всего", fmtInt(point.total)));
      root.append(bar);
      cursor = y;
    });

    if (index % labelStep === 0) {
      const axis = svg("text", { x: x + barWidth / 2, y: height - padBottom + 18,
                                 "text-anchor": "middle", class: "axis-label" });
      axis.textContent = monthLabel(point.month);
      root.append(axis);
    }
  });

  container.append(root);
  legend(container, SENTIMENTS);
}

/** Линия среднего индекса тональности с нулевой базой. */
function renderScoreLine(container, summary) {
  container.replaceChildren();
  const points = summary.timeline;
  if (points.length < 2) return emptyChart(container, "Нужно минимум два месяца данных");

  const width = 720, height = 220, padLeft = 44, padRight = 18, padTop = 18, padBottom = 36;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
                            "aria-label": "Индекс тональности по месяцам" });
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;
  const scaleX = (index) => padLeft + (plotWidth / Math.max(1, points.length - 1)) * index;
  const scaleY = (value) => padTop + plotHeight / 2 - (value * plotHeight) / 2;

  for (const value of [1, 0.5, 0, -0.5, -1]) {
    const y = scaleY(value);
    root.append(svg("line", { x1: padLeft, y1: y, x2: width - padRight, y2: y,
                              class: value === 0 ? "zero-line" : "grid-line" }));
    const label = svg("text", { x: padLeft - 8, y: y + 4, "text-anchor": "end", class: "axis-label" });
    label.textContent = fmtScore(value);
    root.append(label);
  }

  const line = points.map((point, index) => `${index ? "L" : "M"}${scaleX(index)} ${scaleY(point.avg_score)}`).join("");
  const area = svg("path", {
    d: `${line}L${scaleX(points.length - 1)} ${scaleY(0)}L${scaleX(0)} ${scaleY(0)}Z`,
  });
  area.style.fill = "var(--sent-positive)";
  area.style.opacity = ".12";
  const stroke = svg("path", { d: line, fill: "none", "stroke-width": 2,
                               "stroke-linejoin": "round", "stroke-linecap": "round" });
  stroke.style.stroke = "var(--sent-positive)";
  root.append(area, stroke);

  const labelStep = Math.ceil(points.length / Math.floor(plotWidth / 54));
  points.forEach((point, index) => {
    const cx = scaleX(index), cy = scaleY(point.avg_score);
    const dot = svg("circle", { cx, cy, r: 4 });
    dot.style.fill = point.avg_score < 0 ? "var(--sent-negative)" : "var(--sent-positive)";
    dot.style.stroke = "var(--surface-1)";
    dot.style.strokeWidth = 2;
    const hit = svg("circle", { cx, cy, r: 14, fill: "transparent" });
    withTooltip(hit, `<strong>${monthLabel(point.month)}</strong>` +
      tooltipRow("var(--sent-positive)", "Индекс", fmtScore(point.avg_score)) +
      tooltipRow("var(--sent-neutral)", "Отзывов", fmtInt(point.total)) +
      (point.avg_rating ? tooltipRow("var(--ord-3)", "Средняя оценка", point.avg_rating.toFixed(2)) : ""));
    root.append(dot, hit);

    if (index % labelStep === 0) {
      const axis = svg("text", { x: cx, y: height - padBottom + 18, "text-anchor": "middle",
                                 class: "axis-label" });
      axis.textContent = monthLabel(point.month);
      root.append(axis);
    }
  });

  container.append(root);
}

/** Горизонтальные столбики по площадкам, разложенные по тональности. */
function renderSources(container, summary) {
  container.replaceChildren();
  const rows = summary.sources;
  if (!rows.length) return emptyChart(container);

  const width = 372, rowHeight = 46, padLeft = 120, padRight = 46, padTop = 6;
  const height = padTop + rows.length * rowHeight + 8;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
                            "aria-label": "Отзывы по источникам" });
  const plotWidth = width - padLeft - padRight;
  const max = Math.max(...rows.map((row) => row.total), 1);

  rows.forEach((row, index) => {
    const y = padTop + index * rowHeight + 6;
    const barHeight = 18;
    const fullWidth = (row.total / max) * plotWidth;

    const name = svg("text", { x: padLeft - 10, y: y + barHeight - 4, "text-anchor": "end",
                               class: "value-label" });
    name.textContent = sourceTitle(row.source);
    root.append(name);

    let cursor = padLeft;
    SENTIMENTS.forEach((item, position) => {
      const value = row[item.key] || 0;
      if (!value) return;
      const segment = (value / row.total) * fullWidth;
      const isLast = SENTIMENTS.slice(position + 1).every((rest) => !row[rest.key]);
      const side = position === 0 && cursor === padLeft ? (isLast ? "both" : "left") : (isLast ? "right" : "none");
      const bar = svg("path", {
        d: roundedBar(cursor, y, Math.max(1, segment - 2), barHeight, 4,
                      side === "both" ? "right" : side),
      });
      bar.style.fill = item.color;
      withTooltip(bar, `<strong>${sourceTitle(row.source)}</strong>` +
        tooltipRow(item.color, item.title, `${fmtInt(value)} (${fmtPct(value / row.total)})`) +
        tooltipRow("transparent", "Всего", fmtInt(row.total)) +
        (row.avg_rating ? tooltipRow("transparent", "Оценка", row.avg_rating.toFixed(2)) : ""));
      root.append(bar);
      cursor += segment;
    });

    const total = svg("text", { x: padLeft + fullWidth + 8, y: y + barHeight - 4,
                                class: "value-label" });
    total.textContent = fmtInt(row.total);
    const rating = svg("text", { x: padLeft, y: y + barHeight + 14, class: "axis-label" });
    rating.textContent = row.avg_rating ? `средняя оценка ${row.avg_rating.toFixed(2)}` : "без оценок";
    root.append(total, rating);
  });

  container.append(root);
  legend(container, SENTIMENTS);
}

/** Аспекты: диверджентные бары от нуля. */
function renderAspects(container, summary) {
  container.replaceChildren();
  const rows = summary.aspects.slice(0, 8);
  if (!rows.length) return emptyChart(container, "Не нашли устойчивых тем в текстах");

  const width = 320, rowHeight = 30, padLeft = 96, padRight = 20, padTop = 14;
  const height = padTop + rows.length * rowHeight + 22;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img",
                            "aria-label": "Тональность по аспектам" });
  const plotWidth = width - padLeft - padRight;
  const center = padLeft + plotWidth / 2;
  const half = plotWidth / 2;

  root.append(svg("line", { x1: center, y1: padTop - 6, x2: center,
                            y2: padTop + rows.length * rowHeight, class: "zero-line" }));

  rows.forEach((row, index) => {
    const y = padTop + index * rowHeight;
    const barHeight = 14;
    const magnitude = Math.min(1, Math.abs(row.score)) * half;
    const positive = row.score >= 0;
    const x = positive ? center : center - magnitude;
    const bar = svg("path", {
      d: roundedBar(x, y, Math.max(2, magnitude), barHeight, 4, positive ? "right" : "left"),
    });
    bar.style.fill = positive ? "var(--sent-positive)" : "var(--sent-negative)";
    withTooltip(bar, `<strong>${row.aspect}</strong>` +
      tooltipRow(positive ? "var(--sent-positive)" : "var(--sent-negative)", "Тональность", fmtScore(row.score)) +
      tooltipRow("var(--sent-neutral)", "Упоминаний", fmtInt(row.mentions)));
    root.append(bar);

    const name = svg("text", { x: padLeft - 10, y: y + barHeight - 2, "text-anchor": "end",
                               class: "value-label" });
    name.textContent = row.aspect;
    const value = svg("text", {
      x: positive ? Math.min(width - 4, x + magnitude + 6) : Math.max(4, x - 6),
      y: y + barHeight - 2,
      "text-anchor": positive ? "start" : "end",
      class: "axis-label",
    });
    value.textContent = fmtScore(row.score);
    root.append(name, value);
  });

  const caption = svg("text", { x: center, y: height - 4, "text-anchor": "middle", class: "axis-label" });
  caption.textContent = "← негатив · позитив →";
  root.append(caption);
  container.append(root);
}

function renderWords(container, summary) {
  container.replaceChildren();
  const groups = [
    { title: "В позитивных отзывах", items: summary.keywords.positive.slice(0, 12), color: "var(--sent-positive)" },
    { title: "В негативных отзывах", items: summary.keywords.negative.slice(0, 12), color: "var(--sent-negative)" },
  ];

  for (const group of groups) {
    const column = el("div", { class: "words__col" });
    const heading = el("h4");
    heading.append(el("span", { class: "legend__swatch", style: `background:${group.color}` }),
                   document.createTextNode(group.title));
    column.append(heading);

    if (!group.items.length) {
      column.append(el("p", { class: "card__sub" }, "пока пусто"));
    } else {
      const max = group.items[0].count || 1;
      for (const item of group.items) {
        const row = el("div", { class: "word-row" });
        row.append(el("span", {
          class: "word-row__bar",
          style: `width:${(item.count / max) * 100}%;background:${group.color}`,
        }));
        row.append(el("span", {}, item.word));
        row.append(el("span", { class: "word-row__count" }, fmtInt(item.count)));
        column.append(row);
      }
    }
    container.append(column);
  }
}

function niceTicks(max, count) {
  const raw = max / count;
  const magnitude = Math.pow(10, Math.floor(Math.log10(Math.max(1, raw))));
  const step = [1, 2, 2.5, 5, 10].map((multiplier) => multiplier * magnitude)
    .find((candidate) => candidate >= raw) || magnitude * 10;
  const ticks = [];
  for (let value = 0; value <= max + step / 2; value += step) ticks.push(Math.round(value));
  return ticks.length > 1 ? ticks : [0, Math.max(1, max)];
}

/* ------------------------------------------------------- показатели и списки */

function renderKpis(summary) {
  $("#kpiTotal").textContent = fmtInt(summary.total);
  $("#kpiPeriod").textContent = summary.period.from
    ? `${summary.period.from} — ${summary.period.to}`
    : "за всё время";
  $("#kpiRating").textContent = summary.avg_rating ? summary.avg_rating.toFixed(2) : "—";
  $("#kpiRatingStars").innerHTML = summary.avg_rating
    ? `<span class="stars">${stars(summary.avg_rating)}</span>`
    : "источник не отдаёт оценки";
  $("#kpiScore").textContent = summary.total ? fmtScore(summary.avg_score) : "—";
  $("#kpiNegative").textContent = summary.total ? fmtPct(summary.sentiment_share.negative) : "—";
  $("#kpiReply").textContent = summary.total ? fmtPct(summary.reply_rate) : "—";
  $("#kpiReplyHint").textContent = `${fmtInt(summary.with_reply)} ответов компании`;

  const trend = $("#kpiTrend");
  const info = summary.trend;
  trend.className = "badge";
  if (info.status === "негатив растёт") trend.classList.add("badge--bad");
  if (info.status === "негатив снижается") trend.classList.add("badge--good");
  trend.textContent = info.status === "нет данных" || info.status === "недостаточно истории"
    ? info.status
    : `${info.status}: ${fmtPct(info.previous_negative_share)} → ${fmtPct(info.recent_negative_share)}`;

  $("#periodLabel").textContent = summary.total
    ? `${fmtInt(summary.total)} отзывов · ${summary.sources.map((s) => sourceTitle(s.source)).join(" · ")}`
    : "Яндекс.Карты · 2ГИС · Wildberries · Ozon";
}

function renderHighlights(summary) {
  const container = $("#highlights");
  container.replaceChildren();
  const cards = [
    { key: "best", title: "Самый позитивный отзыв", cls: "highlight--best" },
    { key: "worst", title: "Самый негативный отзыв", cls: "highlight--worst" },
    { key: "latest", title: "Последний отзыв", cls: "" },
  ];
  for (const card of cards) {
    const review = summary.highlights[card.key];
    if (!review) continue;
    const node = el("article", { class: `highlight ${card.cls}` });
    node.append(el("p", { class: "highlight__title" }, card.title));
    node.append(el("p", { class: "highlight__text" }, review.text || "(без текста)"));
    const meta = el("div", { class: "highlight__meta" });
    meta.append(el("span", {}, review.author));
    if (review.date) meta.append(el("span", {}, review.date));
    meta.append(el("span", {}, sourceTitle(review.source)));
    if (review.rating) meta.append(el("span", { class: "stars" }, stars(review.rating)));
    node.append(meta);
    container.append(node);
  }
}

function reviewCard(review) {
  const node = el("article", { class: "review" });
  const head = el("div", { class: "review__head" });
  head.append(el("span", { class: "review__author" }, review.author || "Аноним"));

  const item = SENTIMENTS.find((entry) => entry.key === review.sentiment) || SENTIMENTS[1];
  const chip = el("span", { class: "chip" });
  chip.append(el("span", { class: "chip__dot", style: `background:${item.color}` }),
              document.createTextNode(`${item.title} ${fmtScore(review.score)}`));
  head.append(chip);

  if (review.rating) head.append(el("span", { class: "stars" }, stars(review.rating)));
  head.append(el("span", { class: "review__meta" },
                 `${sourceTitle(review.source)}${review.date ? " · " + review.date : ""}`));
  node.append(head);

  if (review.pros) node.append(el("p", { class: "review__extra" }, `Достоинства: ${review.pros}`));
  if (review.cons) node.append(el("p", { class: "review__extra" }, `Недостатки: ${review.cons}`));
  if (review.text) node.append(el("p", { class: "review__text" }, review.text));
  if (review.reply) node.append(el("p", { class: "review__reply" }, `Ответ компании: ${review.reply}`));

  if (review.url) {
    const link = el("a", { href: review.url, target: "_blank", rel: "noopener noreferrer",
                           class: "review__meta" }, "Открыть на площадке →");
    node.append(link);
  }
  return node;
}

function renderTableView(summary) {
  const body = $("#tableViewBody");
  body.replaceChildren();

  const sourcesTable = buildTable(
    "Источники",
    ["Источник", "Всего", "Позитив", "Нейтрально", "Негатив", "Средняя оценка"],
    summary.sources.map((row) => [
      sourceTitle(row.source), fmtInt(row.total), fmtInt(row.positive),
      fmtInt(row.neutral), fmtInt(row.negative),
      row.avg_rating ? row.avg_rating.toFixed(2) : "—",
    ]),
  );

  const timelineTable = buildTable(
    "Динамика по месяцам",
    ["Месяц", "Всего", "Позитив", "Нейтрально", "Негатив", "Индекс"],
    summary.timeline.map((row) => [
      monthLabel(row.month), fmtInt(row.total), fmtInt(row.positive),
      fmtInt(row.neutral), fmtInt(row.negative), fmtScore(row.avg_score),
    ]),
  );

  const aspectsTable = buildTable(
    "Аспекты",
    ["Аспект", "Тональность", "Упоминаний"],
    summary.aspects.map((row) => [row.aspect, fmtScore(row.score), fmtInt(row.mentions)]),
  );

  const ratingsTable = buildTable(
    "Оценки",
    ["Оценка", "Отзывов"],
    [1, 2, 3, 4, 5].map((star) => [`${star} ★`, fmtInt(summary.ratings[String(star)] || 0)]),
  );

  body.append(sourcesTable, timelineTable, aspectsTable, ratingsTable);
}

function buildTable(caption, headers, rows) {
  const table = el("table");
  table.append(el("caption", {}, caption));
  const head = el("tr");
  headers.forEach((title, index) => head.append(el("th", { class: index ? "num" : "" }, title)));
  const thead = el("thead");
  thead.append(head);
  table.append(thead);
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    row.forEach((cell, index) => tr.append(el("td", { class: index ? "num" : "" }, cell)));
    tbody.append(tr);
  }
  table.append(tbody);
  return table;
}

/* -------------------------------------------------------------------- данные */

function currentFilters() {
  const params = new URLSearchParams();
  const company = $("#filterCompany").value;
  const source = $("#filterSource").value;
  const sentiment = $("#filterSentiment").value;
  const period = $("#filterPeriod").value;
  const query = $("#filterQuery").value.trim();

  if (company) params.set("company", company);
  if (source) params.set("source", source);
  if (sentiment) params.set("sentiment", sentiment);
  if (query) params.set("q", query);
  if (period) {
    const from = new Date();
    from.setDate(from.getDate() - Number(period));
    params.set("from", from.toISOString().slice(0, 10));
  }
  return params;
}

async function loadSummary() {
  const params = currentFilters();
  const response = await fetch(`/api/summary?${params}`);
  const summary = await response.json();
  if (summary.error) throw new Error(summary.error);
  state.summary = summary;

  fillCompanies(summary.companies);
  $("#emptyState").hidden = summary.total > 0 || summary.companies.length > 0;

  renderKpis(summary);
  renderSentimentDonut($("#chartSentiment"), summary);
  renderRatings($("#chartRatings"), summary);
  renderTimeline($("#chartTimeline"), summary);
  renderScoreLine($("#chartScoreLine"), summary);
  renderSources($("#chartSources"), summary);
  renderAspects($("#chartAspects"), summary);
  renderWords($("#wordLists"), summary);
  renderHighlights(summary);
  if (!$("#tableView").hidden) renderTableView(summary);
}

async function loadReviews(reset = true) {
  if (state.loading) return;
  state.loading = true;
  if (reset) state.offset = 0;

  const params = currentFilters();
  params.set("limit", PAGE_SIZE);
  params.set("offset", state.offset);

  try {
    const response = await fetch(`/api/reviews?${params}`);
    const data = await response.json();
    if (data.error) throw new Error(data.error);

    state.total = data.total;
    const list = $("#reviewsList");
    if (reset) list.replaceChildren();
    for (const review of data.reviews) list.append(reviewCard(review));

    state.offset += data.reviews.length;
    $("#reviewsCount").textContent = data.total ? `· найдено ${fmtInt(data.total)}` : "";
    $("#loadMore").hidden = state.offset >= data.total;
    if (!data.total && reset) {
      list.append(el("p", { class: "empty" }, "По этим фильтрам ничего не нашлось."));
    }
  } finally {
    state.loading = false;
  }
}

function fillCompanies(companies) {
  const select = $("#filterCompany");
  const current = select.value;
  select.replaceChildren(el("option", { value: "" }, "Все компании"));
  for (const item of companies) {
    select.append(el("option", { value: item.company },
                     `${item.company} (${fmtInt(item.total)})`));
  }
  if (companies.some((item) => item.company === current)) select.value = current;
}

async function refresh() {
  try {
    await Promise.all([loadSummary(), loadReviews(true)]);
  } catch (error) {
    console.error(error);
    $("#emptyState").hidden = false;
    $("#emptyState").textContent = `Не удалось загрузить данные: ${error.message}`;
  }
}

/* -------------------------------------------------------------------- сбор */

async function loadSources() {
  const response = await fetch("/api/sources");
  const data = await response.json();
  state.sources = data.sources || [];
  const select = $("#collectSource");
  select.replaceChildren();
  for (const source of state.sources) {
    select.append(el("option", { value: source.name }, source.title));
  }
  select.value = "demo";
  updateCollectHint();
}

function updateCollectHint() {
  const name = $("#collectSource").value;
  const source = state.sources.find((item) => item.name === name);
  $("#collectHint").textContent = source ? source.query_hint : "";
  $("#apiKeyField").hidden = !(source && source.needs_key);
}

function openModal() {
  $("#collectModal").hidden = false;
  $("#collectStatus").textContent = "";
  $("#collectStatus").className = "modal__status";
  $("#collectQuery").focus();
}

function closeModal() {
  $("#collectModal").hidden = true;
}

async function submitCollect(event) {
  event.preventDefault();
  const status = $("#collectStatus");
  const button = $("#collectSubmit");
  status.className = "modal__status";
  status.textContent = "Собираем… это может занять до минуты.";
  button.disabled = true;

  const payload = {
    source: $("#collectSource").value,
    query: $("#collectQuery").value.trim(),
    company: $("#collectCompany").value.trim(),
    limit: Number($("#collectLimit").value) || 100,
    api_key: $("#collectKey").value.trim(),
    from_file: $("#collectFile").value.trim(),
  };

  try {
    const response = await fetch("/api/collect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "неизвестная ошибка");

    status.className = "modal__status modal__status--ok";
    status.textContent = `Готово: получено ${data.fetched}, новых ${data.added}, ` +
                         `дубликатов ${data.duplicates}. Компания: ${data.company}`;
    await refresh();
    $("#filterCompany").value = data.company;
    await refresh();
  } catch (error) {
    status.className = "modal__status modal__status--error";
    status.textContent = `Ошибка: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

/* -------------------------------------------------------------------- запуск */

function applyStoredTheme() {
  const stored = localStorage.getItem("reviews-theme");
  if (stored) document.documentElement.dataset.theme = stored;
}

function toggleTheme() {
  const root = document.documentElement;
  const isDark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = isDark ? "light" : "dark";
  localStorage.setItem("reviews-theme", root.dataset.theme);
}

function bindEvents() {
  let debounce;
  for (const id of ["#filterCompany", "#filterSource", "#filterSentiment", "#filterPeriod"]) {
    $(id).addEventListener("change", refresh);
  }
  $("#filterQuery").addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(refresh, 350);
  });
  $("#resetFilters").addEventListener("click", () => {
    for (const id of ["#filterCompany", "#filterSource", "#filterSentiment", "#filterPeriod"]) {
      $(id).value = "";
    }
    $("#filterQuery").value = "";
    refresh();
  });

  $("#loadMore").addEventListener("click", () => loadReviews(false));
  $("#openCollect").addEventListener("click", openModal);
  $("#closeCollect").addEventListener("click", closeModal);
  $("#cancelCollect").addEventListener("click", closeModal);
  $("#collectModal").addEventListener("click", (event) => {
    if (event.target === $("#collectModal")) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });
  $("#collectSource").addEventListener("change", updateCollectHint);
  $("#collectForm").addEventListener("submit", submitCollect);
  $("#themeToggle").addEventListener("click", toggleTheme);

  $("#toggleTable").addEventListener("click", () => {
    const view = $("#tableView");
    const pressed = view.hidden;
    view.hidden = !pressed;
    $("#toggleTable").setAttribute("aria-pressed", String(pressed));
    $("#toggleTable").textContent = pressed ? "Скрыть таблицу" : "Показать таблицей";
    if (pressed && state.summary) renderTableView(state.summary);
  });

  window.addEventListener("resize", () => hideTooltip());
}

applyStoredTheme();
bindEvents();
loadSources();
refresh();
