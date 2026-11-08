// Route #/checklists — saved checklist versions grouped by family.
import { api } from '../app/api.js';
import { h, mount, errorBox, loading, provenanceBadge, fmtDate } from '../app/dom.js';
import { href } from '../app/router.js';

const VISIBLE_VERSIONS = 4;

export function groupFamilies(items) {
  const fams = new Map();
  for (const c of items || []) {
    const key = c.family_id || c.id;
    if (!fams.has(key)) fams.set(key, []);
    fams.get(key).push(c);
  }
  const out = [...fams.entries()].map(([family_id, versions]) => {
    versions.sort((a, b) => b.version - a.version);
    return { family_id, versions, latest: versions[0] };
  });
  // Official family first, then most recently changed.
  out.sort((a, b) => (Number(b.versions.some((v) => v.provenance === 'official')) - Number(a.versions.some((v) => v.provenance === 'official')))
    || (new Date(b.latest.created_at) - new Date(a.latest.created_at)));
  return out;
}

function totalWeight(c) {
  const ws = (c.criteria || []).map((x) => Number(x.weight));
  if (!ws.length || ws.some((w) => !Number.isFinite(w))) return null;
  return Math.round(ws.reduce((a, b) => a + b, 0) * 1000) / 1000;
}

function weightCell(c) {
  const w = totalWeight(c);
  if (w === null) return h('span.muted', '—');
  return w === 100 ? String(w) : h('span', { title: 'Сумма весов не равна 100: доли пересчитываются пропорционально' }, String(w), ' ', h('span.cl-pill.cl-pill-warn', '≠ 100'));
}

function stat(label, value) {
  return h('div.cl-stat', h('dt', label), h('dd', value));
}

function versionRow(v, latest) {
  const link = href('/checklists/' + encodeURIComponent(v.id));
  return h('tr',
    h('td', h('a', { href: link, title: 'Открыть версию ' + v.id }, 'v' + v.version),
      v === latest ? [' ', h('span.cl-pill', 'последняя')] : null),
    h('td', fmtDate(v.created_at)),
    h('td.cl-num', String((v.criteria || []).length)),
    h('td.cl-num', weightCell(v)),
    h('td', provenanceBadge(v.provenance)));
}

function versionsTable(rows) {
  return h('table.table.cl-versions-table',
    h('thead', h('tr',
      h('th', { scope: 'col' }, 'Версия'), h('th', { scope: 'col' }, 'Создана'),
      h('th.cl-num', { scope: 'col' }, 'Критериев'), h('th.cl-num', { scope: 'col' }, 'Сумма весов'),
      h('th', { scope: 'col' }, 'Тип'))),
    h('tbody', rows));
}

function familyCard({ versions, latest }) {
  const shown = versions.slice(0, VISIBLE_VERSIONS);
  const rest = versions.slice(VISIBLE_VERSIONS);
  const open = href('/checklists/' + encodeURIComponent(latest.id));
  return h('section.panel.cl-family', { 'aria-label': 'Чек-лист ' + latest.name },
    h('div.cl-family-head',
      h('div.cl-family-title',
        h('h2', h('a', { href: open }, latest.name)),
        provenanceBadge(latest.provenance)),
      h('a.btn.btn-small', { href: open }, 'Открыть v' + latest.version)),
    h('dl.cl-stats',
      stat('Последняя версия', 'v' + latest.version),
      stat('Критериев', String((latest.criteria || []).length)),
      stat('Сумма весов', weightCell(latest)),
      stat('Обновлён', fmtDate(latest.created_at) || '—')),
    versions.length > 1 || rest.length
      ? h('div.cl-table-wrap', versionsTable(shown.map((v) => versionRow(v, latest))))
      : null,
    rest.length
      ? h('details.disclosure.cl-more',
          h('summary', `Более ранние версии (${rest.length})`),
          h('div.cl-table-wrap', versionsTable(rest.map((v) => versionRow(v, latest)))))
      : null);
}

export async function render(container) {
  const head = h('div.page-head.cqa-head.cl-head',
    h('div.cl-head-text',
      h('h1', 'Чек-листы'),
      h('p.page-sub.cl-sub', 'Официальный чек-лист и ваши версии. Каждое сохранение — новая версия.')),
    h('a.btn.btn-primary', { href: href('/checklists/new') }, 'Новый чек-лист'));
  mount(container, head, loading());

  let items;
  try { items = await api.checklists(); }
  catch (e) { mount(container, head, errorBox(e, 'Не удалось загрузить чек-листы')); return; }

  const fams = groupFamilies(items);
  if (!fams.length) {
    mount(container, head, h('div.panel.empty-state.cl-empty',
      h('p', 'Сохранённых чек-листов пока нет.'),
      h('p.muted', 'Создайте чек-лист, чтобы оценивать диалоги по своим критериям.')));
    return;
  }

  mount(container, head, h('div.cl-families', fams.map(familyCard)));
}
