// Route #/appeals — all appeals with status filter tabs.
import { api } from '../app/api.js';
import { h, mount, errorBox, loading, fmtDate } from '../app/dom.js';
import { href, setQuery } from '../app/router.js';
import { answerPill, appealStatusChip, decisionLabel, criterionIndex } from '../results/answer-format.js';

const TABS = [['pending', 'На рассмотрении'], ['reviewed', 'Рассмотренные'], ['all', 'Все']];

export async function render(container, { query }) {
  let alive = true;
  let filter = TABS.some(([k]) => k === query.status) ? query.status : 'pending';
  mount(container, h('h1', 'Апелляции'), loading());

  let appeals;
  try {
    appeals = await api.appeals();
  } catch (e) {
    mount(container, h('h1', 'Апелляции'), errorBox(e, 'Не удалось загрузить апелляции'));
    return;
  }
  // Without an explicit tab, don't open on an empty "pending" view when reviewed appeals exist.
  if (!query.status && filter === 'pending' && appeals.length && !appeals.some((a) => a.status === 'pending')) filter = 'all';
  appeals.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));

  // Evaluation -> conversation_id / checklist (cached; evaluations fetched once each).
  const evalCache = new Map();
  const clCache = new Map();
  const evalIds = [...new Set(appeals.map((a) => a.evaluation_id))];
  await Promise.all(evalIds.map(async (id) => {
    try { evalCache.set(id, await api.evaluation(id)); } catch (e) { evalCache.set(id, e); }
  }));
  const clIds = [...new Set([...evalCache.values()].filter((e) => !(e instanceof Error)).map((e) => e.checklist_id))];
  await Promise.all(clIds.map(async (id) => {
    try { clCache.set(id, criterionIndex(await api.checklist(id))); } catch { clCache.set(id, new Map()); }
  }));
  if (!alive) return;

  const tabs = h('div.tabs', { role: 'tablist', 'aria-label': 'Фильтр по статусу' });
  const body = h('div.appeals-body');

  function counts(k) {
    return k === 'all' ? appeals.length : appeals.filter((a) => a.status === k).length;
  }

  function draw() {
    mount(tabs, TABS.map(([k, label]) => h('button.tab' + (k === filter ? '.active' : ''), {
      type: 'button', role: 'tab', 'aria-selected': String(k === filter),
      on: { click: () => { filter = k; setQuery({ status: k }); draw(); } },
    }, `${label} (${counts(k)})`)));
    const rows = appeals.filter((a) => filter === 'all' || a.status === filter);
    if (!rows.length) {
      mount(body, h('div.empty-state', appeals.length ? 'Нет апелляций с таким статусом.' : 'Апелляций пока нет. Подать апелляцию можно в карточке диалога, у конкретного критерия.'));
      return;
    }
    mount(body, h('div.appeals-table-wrap', h('table.table.appeals-table',
      h('thead', h('tr',
        h('th', 'Подана'), h('th', 'Диалог и критерий'), h('th', 'Ответ: исходный → итог'),
        h('th', 'Комментарий'), h('th', 'Статус'), h('th', h('span.visually-hidden', 'Действие')))),
      h('tbody', rows.map((a) => {
        const ev = evalCache.get(a.evaluation_id);
        const evOk = ev && !(ev instanceof Error);
        const crit = evOk ? (clCache.get(ev.checklist_id) || new Map()).get(a.criterion_id) : undefined;
        const cid = evOk ? ev.conversation_id : a.conversation_id || null;
        const changedAnswer = a.review && a.review.decision === 'change' && a.review.answer
          && a.review.answer.answer !== (a.disputed_answer && a.disputed_answer.answer);
        const explanationOnly = a.review && a.review.decision === 'change' && a.review.answer && !changedAnswer;
        return h('tr' + (a.status === 'pending' ? '.is-pending' : ''),
          h('td.appeal-date', { 'data-label': 'Подана' }, fmtDate(a.created_at)),
          h('td', { 'data-label': 'Диалог и критерий' },
            h('div.appeal-crit', h('span.crit-num', a.criterion_id), ' ', crit ? crit.title : h('span.muted', 'критерий ' + a.criterion_id)),
            cid
              ? h('a.small.appeal-conv', { href: href('/conversations/' + encodeURIComponent(cid), { eval: a.evaluation_id }) }, cid)
              : h('span.muted.small', ev instanceof Error ? 'оценка не загружена: ' + ev.message : '—')),
          h('td', { 'data-label': 'Ответ' },
            h('div.appeal-answer-change',
              changedAnswer ? h('span.pill-was', answerPill(a.disputed_answer, crit)) : answerPill(a.disputed_answer, crit),
              changedAnswer ? [h('span.muted', '→'), answerPill(a.review.answer, crit)] : null,
              explanationOnly ? h('span.muted.small', 'значение то же, изменено обоснование') : null)),
          h('td.appeal-comment', { 'data-label': 'Комментарий' }, a.comment),
          h('td', { 'data-label': 'Статус' },
            h('div.appeal-status-cell', appealStatusChip(a),
              a.review ? h('strong.appeal-decision', decisionLabel(a.review.decision)) : null,
              a.review ? h('span.muted.small', fmtDate(a.review.reviewed_at)) : null)),
          h('td.appeal-action', h('a.btn.btn-small' + (a.status === 'pending' ? '.btn-primary' : ''), { href: href('/appeals/' + encodeURIComponent(a.id)) }, a.status === 'pending' ? 'Рассмотреть' : 'Открыть')));
      })))));
  }

  mount(container,
    h('div.page-head', h('h1', 'Апелляции'),
      h('p.page-sub', 'Подача не меняет балл; исходная оценка модели сохраняется.')),
    tabs, body);
  draw();
  return () => { alive = false; };
}
