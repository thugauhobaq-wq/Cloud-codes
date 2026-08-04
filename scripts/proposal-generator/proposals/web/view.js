/* Страница предложения по ссылке из письма.
   Токен берём из адреса — он же и единственный ключ доступа, поэтому никуда
   больше его не пишем и в заголовок страницы не выводим. */

'use strict';

const token = location.pathname.split('/').filter(Boolean)[1] || '';
const $ = (id) => document.getElementById(id);

let decided = false;

function show(data) {
  document.documentElement.style.setProperty('--accent', data.accent || '#1f5eff');

  $('from').textContent = data.sender ? `${data.sender} — для ${data.client}` : data.client;
  $('title').textContent = data.subject || `Коммерческое предложение № ${data.number}`;

  const meta = [`№ ${data.number} от ${data.issued_at}`];
  if (data.valid_until) meta.push(`действительно до ${data.valid_until}`);
  $('meta').textContent = meta.join('   ·   ');

  $('total').textContent = data.total;
  $('vat').textContent = data.vat_note || '';

  const items = $('items');
  items.innerHTML = '';
  for (const item of data.items || []) {
    const row = document.createElement('li');
    const title = document.createElement('span');
    const sum = document.createElement('span');
    title.textContent = item.title;
    sum.textContent = item.total;
    row.append(title, sum);
    items.append(row);
  }
  items.hidden = !(data.items || []).length;

  $('pdf').src = `/p/${encodeURIComponent(token)}/pdf`;
  $('download').href = `/p/${encodeURIComponent(token)}/pdf`;

  $('foot').textContent = (data.sender_contacts || []).join('   ·   ');

  applyDecision(data.delivery || {});
  $('loading').hidden = true;
  $('page').hidden = false;
  document.title = `${data.subject || 'Коммерческое предложение'} — ${data.sender || ''}`.trim();
}

function applyDecision(state) {
  decided = state.status === 'accepted' || state.status === 'declined';
  $('decide').hidden = decided;
  $('done').hidden = !decided;
  if (!decided) return;

  const accepted = state.status === 'accepted';
  $('doneTitle').textContent = accepted
    ? 'Спасибо, предложение принято'
    : 'Ответ отправлен: хотите обсудить';
  $('doneText').textContent = accepted
    ? 'Отправитель уже видит ваш ответ и свяжется с вами.'
    : 'Отправитель получил ваш комментарий и свяжется с вами.';
  if (state.comment) $('comment').value = state.comment;
}

async function decide(accept, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/p/${encodeURIComponent(token)}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ accept, comment: $('comment').value }),
    });
    if (!response.ok) throw new Error('не получилось отправить ответ');
    applyDecision(await response.json());
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
  } catch (error) {
    alert(`${error.message}. Попробуйте ещё раз или напишите отправителю письмом.`);
  } finally {
    button.disabled = false;
  }
}

$('accept').addEventListener('click', (event) => decide(true, event.target));
$('discuss').addEventListener('click', (event) => decide(false, event.target));
$('change').addEventListener('click', () => {
  $('done').hidden = true;
  $('decide').hidden = false;
});

fetch(`/p/${encodeURIComponent(token)}/data`)
  .then((response) => {
    if (!response.ok) throw new Error('ссылка не действует');
    return response.json();
  })
  .then(show)
  .catch((error) => {
    $('loading').textContent = `${error.message}. Напишите отправителю — он пришлёт новую.`;
  });
