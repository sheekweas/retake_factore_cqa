// Route #/conversations — list of imported conversations with filters and latest evaluation total.
import { api } from '../app/api.js';
import { h, mount, errorBox, loading, fmtScore, fmtDate } from '../app/dom.js';
import { href, setQuery } from '../app/router.js';

const LANGUAGES = ['RU', 'KZ', 'EN'];
const CHANNELS = [['agent chat', 'Чат с оператором'], ['voice call', 'Голосовой звонок']];
const CHANNEL_LABEL = Object.fromEntries(CHANNELS);
const STATUSES = [['evaluated', 'Оценён'], ['unevaluated', 'Не оценён'], ['gross', 'Грубая ошибка'], ['appealed', 'Изменён апелляцией']];

/** Newest evaluation per conversation by created_at (ISO strings compare lexicographically only when same format, so use Date). */
export function latestByConversation(evaluations) {
  const map = new Map();
  for (const ev of evaluations || []) {
    const cur = map.get(ev.conversation_id);
    if (!cur || new Date(ev.created_at) > new Date(cur.created_at)) map.set(ev.conversation_id, ev);
  }
  return map;
}

const finalResult = (ev) => (ev && ev.final && ev.final.result) || null;
const isGross = (ev) => Boolean(finalResult(ev) && finalResult(ev).gross_error);
const isChanged = (ev) => {
  const fin = finalResult(ev);
  const orig = ev && ev.original && ev.original.result;
  return Boolean(fin && orig && fin.total !== orig.total);
};
const STATUS_TEST = {
  evaluated: (ev) => Boolean(ev),
  unevaluated: (ev) => !ev,
  gross: isGross,
  appealed: isChanged,
};

function selectField(label, value, options, onChange) {
  return h('label.field.cqa-filter',
    h('span', label),
    h('select.chip-filter', { class: value ? 'is-active' : '', on: { change: (e) => onChange(e.target.value) } },
      h('option', { value: '', selected: !value }, 'Все'),
      options.map(([v, text]) => h('option', { value: v, selected: v === value }, text))));
}

function scoreCell(ev) {
  if (!ev) return h('td', { 'data-label': 'Оценка' }, h('span.muted', 'не оценён'));
  const fin = finalResult(ev);
  const orig = ev.original && ev.original.result;
  const total = fin ? fin.total : null;
  return h('td.cqa-score-cell', { 'data-label': 'Оценка' },
    h('a.cqa-score', { href: href('/conversations/' + encodeURIComponent(ev.conversation_id), { eval: ev.id }), title: 'Оценка от ' + fmtDate(ev.created_at) },
      h('strong', fmtScore(total, 2))),
    total === null ? h('span.muted.cqa-note', ' нет применимых критериев') : null,
    fin && fin.gross_error ? [' ', h('span.badge.badge-gross', 'грубая ошибка')] : null,
    isChanged(ev) ? [' ', h('span.badge.badge-warn', { title: 'Исходный итог: ' + fmtScore(orig.total, 2) }, 'изменено апелляцией')] : null);
}

function stat(value, label) {
  return h('div.stat', h('span.stat-value', String(value)), h('span.stat-label', label));
}

export async function render(container, { query }) {
  const state = {
    language: query.language || '',
    channel: query.channel || '',
    dataset_group_id: query.dataset_group_id || '',
    status: STATUS_TEST[query.status] ? query.status : '',
    q: query.q || '',
  };
  let alive = true;
  const head = () => h('div.page-head',
    h('div', h('h1', 'Диалоги'), h('p.page-sub', 'Импортированные диалоги и итог последней оценки.')),
    h('div.page-actions', h('a.btn.btn-primary', { href: href('/import') }, 'Импорт и оценка')));
  mount(container, head(), loading('Загрузка диалогов…'));

  let all, evals;
  try {
    [all, evals] = await Promise.all([api.conversations(), api.evaluations()]);
  } catch (e) {
    mount(container, head(), errorBox(e, 'Не удалось загрузить диалоги'),
      h('button.btn', { type: 'button', on: { click: () => render(container, { query }) } }, 'Повторить'));
    return;
  }
  if (!alive) return;
  const latest = latestByConversation(evals);
  const groups = [...new Set(all.map((c) => c.dataset_group_id).filter(Boolean))].sort();
  if (state.dataset_group_id && !groups.includes(state.dataset_group_id)) groups.push(state.dataset_group_id);

  const tableHost = h('div');
  const status = h('p.muted.cqa-count', { role: 'status', 'aria-live': 'polite' });
  const filtersHost = h('div.cqa-filters');
  let items = all;
  let reqSeq = 0;

  const anyFilter = () => Boolean(state.language || state.channel || state.dataset_group_id || state.status || state.q.trim());

  function syncUrl() {
    setQuery({ language: state.language || undefined, channel: state.channel || undefined, dataset_group_id: state.dataset_group_id || undefined, status: state.status || undefined, q: state.q || undefined });
  }

  async function reload() {
    syncUrl();
    drawReset();
    const seq = ++reqSeq;
    const filters = { language: state.language, channel: state.channel, dataset_group_id: state.dataset_group_id };
    if (!filters.language && !filters.channel && !filters.dataset_group_id) { items = all; drawTable(); return; }
    mount(tableHost, loading());
    try {
      const res = await api.conversations(filters);
      if (!alive || seq !== reqSeq) return;
      items = res;
      drawTable();
    } catch (e) {
      if (alive && seq === reqSeq) mount(tableHost, errorBox(e, 'Не удалось применить фильтр'));
    }
  }

  const resetHost = h('span.cqa-reset');
  function drawReset() {
    mount(resetHost, anyFilter() ? h('button.btn.btn-ghost.btn-small', { type: 'button', on: { click: resetFilters } }, 'Сбросить') : null);
  }

  function resetFilters() {
    Object.assign(state, { language: '', channel: '', dataset_group_id: '', status: '', q: '' });
    search.value = '';
    drawFilters();
    search.focus();
    reload();
  }

  // Selects are rebuilt only on reset, so keyboard focus stays on the control being changed.
  function drawFilters() {
    const onPick = (key, after) => (v) => {
      state[key] = v;
      for (const sel of filtersHost.querySelectorAll('select')) sel.classList.toggle('is-active', Boolean(sel.value));
      after();
    };
    mount(filtersHost,
      selectField('Язык', state.language, LANGUAGES.map((l) => [l, l]), onPick('language', reload)),
      selectField('Канал', state.channel, CHANNELS, onPick('channel', reload)),
      selectField('Статус', state.status, STATUSES, onPick('status', () => { syncUrl(); drawTable(); })),
      groups.length > 1 || state.dataset_group_id
        ? selectField('Группа данных', state.dataset_group_id, groups.map((g) => [g, g]), onPick('dataset_group_id', reload))
        : null,
      resetHost);
    drawReset();
  }

  function drawTable() {
    const needle = state.q.trim().toLowerCase();
    const statusTest = STATUS_TEST[state.status];
    const rows = items.filter((c) =>
      (!needle || String(c.conversation_id).toLowerCase().includes(needle)) &&
      (!statusTest || statusTest(latest.get(c.conversation_id))));
    status.textContent = `Показано: ${rows.length} из ${all.length}`;
    if (!all.length) {
      mount(tableHost, h('div.empty-state.cqa-empty',
        h('h2', 'Диалогов пока нет'),
        h('p', 'Импортируйте набор организаторов или JSON-файл, затем запустите оценку.'),
        h('a.btn.btn-primary', { href: href('/import') }, 'Перейти к импорту')));
      return;
    }
    drawReset();
    if (!rows.length) {
      mount(tableHost, h('div.empty-state.cqa-empty', h('p', 'Нет диалогов, подходящих под фильтры.'),
        h('button.btn', { type: 'button', on: { click: resetFilters } }, 'Сбросить фильтры')));
      return;
    }
    mount(tableHost, h('div.card.cqa-table-card', h('div.cqa-table-wrap', h('table.table.cqa-conv-table',
      h('thead', h('tr',
        h('th', { scope: 'col' }, 'Диалог'), h('th', { scope: 'col' }, 'Язык'), h('th', { scope: 'col' }, 'Канал'),
        h('th.cqa-num', { scope: 'col' }, 'Сообщений'), h('th', { scope: 'col' }, 'Группа'), h('th', { scope: 'col' }, 'Запись'),
        h('th', { scope: 'col' }, 'Последняя оценка'))),
      h('tbody', rows.map((c) => h('tr',
        h('td.cqa-id-cell', h('a.cqa-conv-link', { href: '#/conversations/' + encodeURIComponent(c.conversation_id) }, c.conversation_id)),
        h('td', { 'data-label': 'Язык' }, c.language ? h('span.cqa-lang', c.language) : ''),
        h('td.cqa-channel', { 'data-label': 'Канал', title: CHANNEL_LABEL[c.channel] || '' }, c.channel || ''),
        h('td.cqa-num', { 'data-label': 'Сообщений' }, c.message_count ?? ''),
        h('td.cqa-group', { 'data-label': 'Группа' }, c.dataset_group_id || ''),
        h('td', { 'data-label': 'Запись' }, c.audio_url ? h('span.badge.badge-neutral', { title: 'Есть ссылка на исходную запись' }, 'запись') : h('span.muted', '—')),
        scoreCell(latest.get(c.conversation_id)))))))));
  }

  const search = h('input.cqa-search-input', {
    type: 'search', value: state.q, placeholder: 'Поиск по ID диалога', 'aria-label': 'Поиск по ID диалога',
    on: { input: (e) => { state.q = e.target.value; syncUrl(); drawReset(); drawTable(); } },
  });

  const evaluated = all.filter((c) => latest.has(c.conversation_id));
  const gross = evaluated.filter((c) => isGross(latest.get(c.conversation_id))).length;
  const changed = evaluated.filter((c) => isChanged(latest.get(c.conversation_id))).length;

  mount(container,
    head(),
    all.length ? h('div.stats', { 'aria-label': 'Сводка по всем диалогам' },
      stat(all.length, 'всего диалогов'),
      stat(evaluated.length, 'оценено'),
      stat(all.length - evaluated.length, 'без оценки'),
      stat(gross, 'с грубой ошибкой'),
      changed ? stat(changed, 'изменено апелляцией') : null) : null,
    h('div.cqa-toolbar',
      h('label.field.cqa-search', h('span.visually-hidden', 'Поиск'), search),
      filtersHost),
    status,
    tableHost);

  drawFilters();
  await reload();
  return () => { alive = false; };
}
