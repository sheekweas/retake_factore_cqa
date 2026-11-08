// Routes #/checklists/new and #/checklists/:id — immutable versions; edits are saved as a new version.
import { api } from '../app/api.js';
import { h, mount, clear, errorBox, loading, toast, provenanceBadge, fmtDate } from '../app/dom.js';
import { href, navigate } from '../app/router.js';

const SLUG = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
const SERVER_FIELDS = ['id', 'version', 'created_at', 'family_id'];
let keySeq = 0;

function parseNum(s) {
  if (typeof s === 'number') return s;
  const t = String(s ?? '').trim().replace(',', '.');
  if (t === '') return NaN;
  return Number(t);
}

function toRow(c) {
  return {
    key: 'c' + (++keySeq),
    orig: c || null,
    id: c ? String(c.id ?? '') : '',
    title: c ? String(c.title ?? '') : '',
    type: c && c.type === 'numeric' ? 'numeric' : 'boolean',
    min: c && c.min !== undefined ? String(c.min) : '1',
    max: c && c.max !== undefined ? String(c.max) : '5',
    weight: c && c.weight !== undefined ? String(c.weight) : '1',
    allow_na: Boolean(c && c.allow_na),
    section: c && typeof c.section === 'string' ? c.section : '',
    gross_error: Boolean(c && c.gross_error),
    description: c?.description || '',
    na_condition: c?.na_condition || '',
    allowed_values: c?.allowed_values?.join('; ') || '',
  };
}

/** Build the criterion object for the API, preserving unknown fields of the original. */
function buildCriterion(r) {
  const c = { ...(r.orig || {}) };
  c.id = r.id.trim();
  c.title = r.title.trim();
  c.type = r.type;
  c.weight = parseNum(r.weight);
  if (r.type === 'numeric') { c.min = parseNum(r.min); c.max = parseNum(r.max); }
  else { delete c.min; delete c.max; delete c.allowed_values; delete c.scale_labels; }
  if (r.type === 'numeric') {
    const values = r.allowed_values.trim();
    if (values) c.allowed_values = values.split(';').map(parseNum); else delete c.allowed_values;
    if (r.orig && JSON.stringify(c.allowed_values) !== JSON.stringify(r.orig.allowed_values)) delete c.scale_labels;
    delete c.gross_error_when;
  }
  if (r.description.trim()) c.description = r.description.trim(); else delete c.description;
  if (r.na_condition.trim()) c.na_condition = r.na_condition.trim(); else delete c.na_condition;
  if (!r.gross_error) delete c.gross_error_when;
  c.allow_na = r.allow_na;
  c.gross_error = r.gross_error;
  const sec = r.section.trim();
  if (sec) c.section = sec; else delete c.section;
  return c;
}

function validate(d) {
  const errs = { top: {}, rows: new Map(), global: [] };
  if (!d.name.trim()) errs.top.name = 'Название обязательно';
  const dec = String(d.decimals).trim();
  if (!/^\d+$/.test(dec) || Number(dec) > 6) errs.top.decimals = 'Целое число от 0 до 6';
  if (!d.criteria.length) errs.global.push('Нужен хотя бы один критерий');
  const seen = new Map();
  for (const r of d.criteria) seen.set(r.id.trim(), (seen.get(r.id.trim()) || 0) + 1);
  let positive = false;
  for (const r of d.criteria) {
    const e = {};
    const id = r.id.trim();
    if (!id) e.id = 'ID обязателен';
    else if (seen.get(id) > 1) e.id = 'ID должен быть уникальным';
    else if ((!r.orig || r.orig.id !== id) && !SLUG.test(id)) e.id = 'Латиница, цифры, «-», «_», «.», без пробелов';
    if (!r.title.trim()) e.title = 'Формулировка обязательна';
    const w = parseNum(r.weight);
    if (!Number.isFinite(w)) e.weight = 'Конечное число';
    else if (w < 0) e.weight = 'Не меньше 0';
    else if (w > 0) positive = true;
    if (r.type === 'numeric') {
      const lo = parseNum(r.min), hi = parseNum(r.max);
      if (r.allowed_values.trim()) {
        const values = r.allowed_values.split(';').map(parseNum);
        if (values.length < 2 || values.some(v => !Number.isFinite(v) || v < lo || v > hi) || new Set(values).size !== values.length || values.some((v, i) => i && v <= values[i-1]) || values[0] !== lo || values.at(-1) !== hi) e.allowed_values = 'Ступени через ; по возрастанию, включая минимум и максимум';
      }
      if (!Number.isFinite(lo)) e.min = 'Конечное число';
      if (!Number.isFinite(hi)) e.max = 'Конечное число';
      if (Number.isFinite(lo) && Number.isFinite(hi) && !(lo < hi)) e.max = 'Максимум должен быть больше минимума';
      else if (Number.isFinite(lo) && Number.isFinite(hi) && !Number.isFinite(hi - lo)) e.max = 'Слишком большой диапазон';
    }
    if (Object.keys(e).length) errs.rows.set(r.key, e);
  }
  if (d.criteria.length && !positive) errs.global.push('Хотя бы один критерий должен иметь положительный вес');
  const count = Object.keys(errs.top).length + [...errs.rows.values()].reduce((n, e) => n + Object.keys(e).length, 0) + errs.global.length;
  return { ...errs, count };
}

function stable(v) {
  if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
  if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
  return JSON.stringify(v);
}

function normCrit(c) {
  const x = { ...c, allow_na: Boolean(c.allow_na), gross_error: Boolean(c.gross_error) };
  if (x.type !== 'numeric') { delete x.min; delete x.max; }
  if (!x.section) delete x.section;
  return x;
}

function diff(loaded, d) {
  const before = loaded ? loaded.criteria || [] : [];
  const after = d.criteria.map(buildCriterion);
  const bMap = new Map(before.map((c) => [c.id, c]));
  const aMap = new Map(after.map((c) => [c.id, c]));
  const added = after.filter((c) => c.id && !bMap.has(c.id)).map((c) => c.id);
  const removed = before.filter((c) => !aMap.has(c.id)).map((c) => c.id);
  const changed = after.filter((c) => bMap.has(c.id) && stable(normCrit(bMap.get(c.id))) !== stable(normCrit(c))).map((c) => c.id);
  const common = after.map((c) => c.id).filter((id) => bMap.has(id));
  const reordered = stable(common) !== stable(before.map((c) => c.id).filter((id) => aMap.has(id)));
  const meta = [];
  if (loaded) {
    if (loaded.name !== d.name.trim()) meta.push('название');
    if (loaded.provenance !== d.provenance) meta.push('происхождение');
    const rules = loaded.rules || {};
    if (String(rules.decimals ?? 2) !== String(d.decimals).trim()) meta.push('округление');
    if ((rules.gross_error_policy || 'flag') !== d.policy) meta.push('политика грубой ошибки');
    if (reordered) meta.push('порядок критериев');
  }
  return { added, removed, changed, meta, any: !loaded || added.length + removed.length + changed.length + meta.length > 0 };
}

export async function render(container, { params }) {
  const id = params && params.id;
  const isNew = !id;
  let alive = true;
  mount(container, h('h1', isNew ? 'Новый чек-лист' : 'Чек-лист'), loading());

  let loaded = null, all = [];
  try {
    [loaded, all] = await Promise.all([isNew ? null : api.checklist(id), api.checklists().catch(() => [])]);
  } catch (e) {
    mount(container, h('h1', 'Чек-лист'), errorBox(e, 'Не удалось загрузить версию'),
      h('a', { href: href('/checklists') }, '← К списку чек-листов'));
    return;
  }
  if (!alive) return () => {};

  const family = loaded ? all.filter((c) => c.family_id === loaded.family_id).sort((a, b) => b.version - a.version) : [];
  const latestVersion = family.length ? family[0].version : (loaded ? loaded.version : 0);

  const d = loaded
    ? {
        name: loaded.name || '', provenance: loaded.provenance || 'custom',
        decimals: String(loaded.rules && loaded.rules.decimals !== undefined ? loaded.rules.decimals : 2),
        policy: (loaded.rules && loaded.rules.gross_error_policy) || 'flag',
        criteria: (loaded.criteria || []).map(toRow),
      }
    : { name: '', provenance: 'custom', decimals: '2', policy: 'flag', criteria: [toRow(null)] };

  let showErrors = false;
  let saving = false;
  const topErr = { name: h('span.field-error', { id: 'err-name' }), decimals: h('span.field-error', { id: 'err-decimals' }) };
  const globalErr = h('div', { 'aria-live': 'polite' });
  const serverErr = h('div', { 'aria-live': 'assertive' });
  const rowsHost = h('div.cqa-crit-list');
  const summaryHost = h('div');
  const weightsHost = h('div');
  const officialWarn = h('div.alert.alert-info.cl-official-note', { hidden: d.provenance !== 'official' },
    'Официальный чек-лист из PDF организаторов. Изменённая копия сохранится как пользовательская версия, оригинал останется доступен.');
  const sectionsList = h('datalist#cqa-sections');
  let rowErr = new Map();
  let rowHeads = new Map();
  let rowAdv = new Map();
  let saveBtn;
  const saveStatus = h('span.cl-save-status', { 'aria-live': 'polite' });

  function refresh() {
    const v = validate(d);
    for (const k of Object.keys(topErr)) topErr[k].textContent = showErrors ? (v.top[k] || '') : '';
    for (const r of d.criteria) {
      const e = v.rows.get(r.key) || {};
      const spans = rowErr.get(r.key) || {};
      for (const [f, span] of Object.entries(spans)) {
        span.textContent = showErrors ? (e[f] || '') : '';
        const input = span.previousElementSibling;
        if (input) input.setAttribute('aria-invalid', showErrors && e[f] ? 'true' : 'false');
        // Never hide an error inside a collapsed "advanced" block.
        const adv = rowAdv.get(r.key);
        if (showErrors && e[f] && adv && adv.contains(span) && !adv.open) { adv.open = true; r.open = true; }
      }
    }
    mount(globalErr, showErrors && v.count
      ? h('div.alert.alert-error', { role: 'alert' }, `Исправьте ошибки (${v.count}) перед сохранением.`,
          v.global.length ? h('ul', v.global.map((g) => h('li', g))) : null)
      : null);
    saveStatus.textContent = showErrors && v.count ? `Ошибок: ${v.count}` : '';
    critCount.textContent = String(d.criteria.length);
    drawRowHeads();
    drawSummary();
    drawWeights();
    const secs = [...new Set(d.criteria.map((r) => r.section.trim()).filter(Boolean))].sort();
    mount(sectionsList, secs.map((s) => h('option', { value: s })));
    return v;
  }

  function drawSummary() {
    const df = diff(loaded, d);
    const list = (label, ids, cls) => ids.length ? h('li', h('span.badge.' + cls, label), ' ', ids.map((x, i) => [i ? ', ' : '', h('code', x)])) : null;
    mount(summaryHost, h('section.panel.cl-panel', { 'aria-labelledby': 'cl-sum-h' },
      h('h2#cl-sum-h', loaded ? `Изменения относительно v${loaded.version}` : 'Содержимое новой версии'),
      !df.any ? h('p.muted.cl-tight', 'Изменений нет. Сохранение создаст идентичную копию.')
        : h('ul.cqa-diff',
            list('добавлены', df.added, 'badge-ok'),
            list('удалены', df.removed, 'badge-error'),
            list('изменены', df.changed, 'badge-warn'),
            df.meta.length ? h('li', h('span.badge.badge-neutral', 'настройки'), ' ', df.meta.join(', ')) : null)));
  }

  function drawWeights() {
    const ws = d.criteria.map((r) => parseNum(r.weight));
    const valid = ws.every((w) => Number.isFinite(w) && w >= 0);
    const sum = valid ? ws.reduce((a, b) => a + b, 0) : NaN;
    const rounded = Number.isFinite(sum) ? Math.round(sum * 1000) / 1000 : NaN;
    const status = !Number.isFinite(rounded) ? h('span.cl-pill.cl-pill-err', 'есть некорректные веса')
      : rounded === 100 ? h('span.cl-pill.cl-pill-ok', 'сумма = 100')
      : h('span.cl-pill.cl-pill-warn', `сумма ${rounded}, не 100`);
    mount(weightsHost, h('section.panel.cl-panel', { 'aria-labelledby': 'cl-w-h' },
      h('div.cl-panel-head', h('h2#cl-w-h', 'Веса'), status),
      h('p.muted.cl-note', 'Доля = вес / сумма весов. При оценке критерии с N/A исключаются, и доли пересчитываются для каждого диалога.'),
      h('div.cl-table-wrap', h('table.table.cqa-weights',
        h('thead', h('tr', h('th', { scope: 'col' }, 'Критерий'), h('th.cl-num', { scope: 'col' }, 'Вес'), h('th.cl-num', { scope: 'col' }, 'Доля'), h('th.cl-bar-col', { scope: 'col' }, h('span.cl-sr', 'Диаграмма доли')))),
        h('tbody', d.criteria.map((r, i) => {
          const share = valid && sum > 0 ? ws[i] / sum : null;
          return h('tr',
            h('td.cl-wname', h('code', r.id || '(без ID)'), ' ', h('span.muted', r.title)),
            h('td.cl-num', Number.isFinite(ws[i]) ? String(ws[i]) : '—'),
            h('td.cl-num', share === null ? '—' : (Math.round(share * 1000) / 10) + '%'),
            h('td.cl-bar-col', h('div.cqa-bar', { 'aria-hidden': 'true' }, h('div.cqa-bar-fill', { style: { width: share === null ? '0%' : (share * 100) + '%' } }))));
        })),
        h('tfoot', h('tr', h('th', 'Сумма'), h('th.cl-num', Number.isFinite(sum) ? String(rounded) : '—'), h('th.cl-num', sum > 0 ? '100%' : '—'), h('th', '')))))));
  }

  function fieldInput(r, f, label, attrs = {}, hint = null) {
    const inputId = `${r.key}-${f}`;
    const err = h('span.field-error', { id: inputId + '-err' });
    const input = h(['description', 'na_condition'].includes(f) ? 'textarea' : 'input', {
      id: inputId, value: r[f], 'aria-describedby': inputId + '-err', ...attrs,
      on: { input: (e) => { r[f] = e.target.value; refresh(); } },
    });
    (rowErr.get(r.key) || rowErr.set(r.key, {}).get(r.key))[f] = err;
    return h('div.field.cqa-f-' + f, h('label', { for: inputId }, h('span', label)), input, err,
      hint ? h('span.cl-hint', hint) : null);
  }

  function checkbox(r, f, label) {
    const inputId = `${r.key}-${f}`;
    return h('label.cqa-check', { for: inputId },
      h('input', { type: 'checkbox', id: inputId, checked: r[f], on: { change: (e) => { r[f] = e.target.checked; refresh(); } } }), ' ', label);
  }

  /** Compact pills summarising a criterion in its header (kept live by refresh()). */
  function rowPills(r) {
    const w = parseNum(r.weight);
    const pills = [h('span.cl-pill.cl-pill-strong', 'Вес ' + (Number.isFinite(w) ? String(w) : '—'))];
    if (r.type === 'numeric') {
      const steps = r.allowed_values.trim();
      pills.push(h('span.cl-pill', steps ? 'Шкала ' + steps.split(';').map((s) => s.trim()).join('/') : `Шкала ${r.min.trim() || '?'}–${r.max.trim() || '?'}`));
    } else pills.push(h('span.cl-pill', 'Да/Нет'));
    if (r.allow_na) pills.push(h('span.cl-pill', 'N/A допускается'));
    if (r.gross_error) pills.push(h('span.cl-pill.cl-pill-err', 'Грубая ошибка'));
    if (r.section.trim()) pills.push(h('span.cl-pill.cl-pill-muted', r.section.trim()));
    return pills;
  }

  function drawRowHeads() {
    for (const r of d.criteria) {
      const parts = rowHeads.get(r.key);
      if (!parts) continue;
      parts.title.textContent = r.title.trim() || 'Без формулировки';
      parts.title.classList.toggle('muted', !r.title.trim());
      parts.code.textContent = r.id.trim() || 'без ID';
      mount(parts.pills, rowPills(r));
      parts.desc.textContent = r.description.trim();
      parts.desc.hidden = !r.description.trim();
    }
  }

  function drawRows(focusKey, focusField = 'id') {
    rowErr = new Map();
    rowHeads = new Map();
    rowAdv = new Map();
    clear(rowsHost);
    d.criteria.forEach((r, i) => {
      const typeId = `${r.key}-type`;
      const isNewRow = !r.orig;
      const move = (delta) => {
        const j = i + delta;
        if (j < 0 || j >= d.criteria.length) return;
        [d.criteria[i], d.criteria[j]] = [d.criteria[j], d.criteria[i]];
        drawRows(r.key, delta < 0 ? 'up' : 'down');
      };
      const head = { title: h('span.cl-crit-title'), code: h('code.cl-crit-code'), pills: h('span.cl-pills'), desc: h('p.cl-crit-desc') };
      rowHeads.set(r.key, head);
      const scaleLabels = r.type === 'numeric' && r.orig?.scale_labels
        ? h('div.cl-scale', h('span.cl-label', 'Подписи шкалы'),
            h('ul.muted.cl-scale-list', Object.entries(r.orig.scale_labels).map(([value, label]) => h('li', `${value}: ${label}`))))
        : null;
      const adv = h('details.disclosure.cl-adv', {
        open: Boolean(r.open),
        on: { toggle: (e) => { r.open = e.currentTarget.open; } },
      },
        h('summary', 'Правило оценки, условие N/A и раздел'),
        h('div.cl-adv-body',
          fieldInput(r, 'description', 'Правило оценки', { rows: 3 }),
          fieldInput(r, 'na_condition', 'Условие N/A', { rows: 2 }, r.allow_na ? null : 'Используется, только если отмечено «Допускается N/A».'),
          h('div.cl-adv-row',
            fieldInput(r, 'section', 'Раздел', { list: 'cqa-sections', autocomplete: 'off' }),
            isNewRow ? null : fieldInput(r, 'id', 'ID критерия', { required: true, autocomplete: 'off', spellcheck: 'false' })),
          scaleLabels));
      rowAdv.set(r.key, adv);
      rowsHost.append(h('fieldset.cqa-crit', { dataset: { key: r.key } },
        h('legend.cl-sr', `Критерий ${i + 1}`),
        h('div.cl-crit-top',
          h('span.cl-crit-num', { 'aria-hidden': 'true' }, String(i + 1)),
          h('div.cl-crit-headtext',
            h('div.cl-crit-line', head.title, head.code, isNewRow ? h('span.cl-pill.cl-pill-ok', 'новый') : null),
            head.pills),
          h('div.cl-crit-tools',
            h('button.btn.btn-small', { type: 'button', dataset: { act: 'up' }, disabled: i === 0, title: 'Выше', 'aria-label': `Переместить критерий ${i + 1} выше`, on: { click: () => move(-1) } }, '↑'),
            h('button.btn.btn-small', { type: 'button', dataset: { act: 'down' }, disabled: i === d.criteria.length - 1, title: 'Ниже', 'aria-label': `Переместить критерий ${i + 1} ниже`, on: { click: () => move(1) } }, '↓'),
            h('button.btn.btn-small.btn-danger', {
              type: 'button', 'aria-label': `Удалить критерий ${i + 1}`,
              on: { click: () => { d.criteria.splice(i, 1); drawRows(); toast('Критерий удалён из черновика'); } },
            }, 'Удалить'))),
        head.desc,
        h('div.cqa-crit-grid',
          isNewRow ? fieldInput(r, 'id', 'ID (латиница, без пробелов)', { required: true, autocomplete: 'off', spellcheck: 'false' }) : null,
          fieldInput(r, 'title', 'Формулировка', { required: true }),
          h('div.field.cqa-f-type', h('label', { for: typeId }, h('span', 'Тип ответа')),
            h('select', { id: typeId, on: { change: (e) => { r.type = e.target.value; drawRows(r.key, 'type'); } } },
              h('option', { value: 'boolean', selected: r.type === 'boolean' }, 'Да/Нет'),
              h('option', { value: 'numeric', selected: r.type === 'numeric' }, 'Числовая шкала'))),
          fieldInput(r, 'weight', 'Вес', { inputmode: 'decimal' }),
          r.type === 'numeric' ? h('div.cl-scale-fields',
            fieldInput(r, 'min', 'Минимум', { inputmode: 'decimal' }),
            fieldInput(r, 'max', 'Максимум', { inputmode: 'decimal' }),
            fieldInput(r, 'allowed_values', 'Допустимые ступени через ;', { placeholder: 'пусто — любое значение в диапазоне' })) : null),
        h('div.cqa-crit-foot',
          checkbox(r, 'allow_na', 'Допускается N/A'),
          checkbox(r, 'gross_error', 'Может быть грубой ошибкой')),
        adv));
    });
    refresh();
    if (focusKey) {
      const fs = rowsHost.querySelector(`[data-key="${focusKey}"]`);
      const target = fs && (fs.querySelector(`[data-act="${focusField}"]:not(:disabled)`) || fs.querySelector(`#${focusKey}-${focusField}`) || fs.querySelector('input'));
      if (target) target.focus();
    }
  }

  async function save() {
    if (saving) return;
    showErrors = true;
    const v = refresh();
    mount(serverErr, null);
    if (v.count) {
      const bad = container.querySelector('[aria-invalid="true"]') || (v.top.name ? container.querySelector('#cl-name') : v.top.decimals ? container.querySelector('#cl-decimals') : null);
      if (bad) bad.focus();
      return;
    }
    const base = {};
    if (loaded) for (const [k, val] of Object.entries(loaded)) if (!SERVER_FIELDS.includes(k)) base[k] = val;
    const payload = {
      ...base,
      name: d.name.trim(),
      provenance: loaded?.provenance === 'official' && diff(loaded, d).any ? 'custom' : d.provenance,
      criteria: d.criteria.map(buildCriterion),
      rules: { ...((loaded && loaded.rules) || {}), decimals: Number(String(d.decimals).trim()), gross_error_policy: d.policy },
    };
    if (loaded && loaded.family_id) payload.family_id = loaded.family_id;
    saving = true;
    saveBtn.disabled = true;
    saveBtn.textContent = 'Сохранение…';
    try {
      const saved = await api.saveChecklist(payload);
      toast(`Сохранена версия v${saved.version}`, 'ok');
      navigate('/checklists/' + encodeURIComponent(saved.id));
    } catch (e) {
      mount(serverErr, errorBox(e, 'Сервер отклонил чек-лист'));
      serverErr.scrollIntoView({ block: 'center' });
    } finally {
      saving = false;
      if (saveBtn.isConnected) { saveBtn.disabled = false; saveBtn.textContent = 'Сохранить как новую версию'; }
    }
  }

  const nameInput = h('input#cl-name', { value: d.name, required: true, 'aria-describedby': 'err-name', on: { input: (e) => { d.name = e.target.value; refresh(); } } });
  const decInput = h('input#cl-decimals', { type: 'number', min: 0, max: 6, step: 1, value: d.decimals, 'aria-describedby': 'err-decimals',
    on: { input: (e) => { d.decimals = e.target.value; refresh(); } } });
  saveBtn = h('button.btn.btn-primary', { type: 'button', on: { click: save } }, 'Сохранить как новую версию');

  const history = h('aside.cl-side',
    h('section.panel.cl-panel.cqa-history', { 'aria-labelledby': 'cl-hist-h' },
      h('h2#cl-hist-h', 'История версий'),
      loaded
        ? h('ul.cqa-versions', family.map((c) => h('li', { class: c.id === loaded.id ? 'current' : '' },
            h('span.cl-ver-main',
              c.id === loaded.id
                ? h('strong', { 'aria-current': 'page' }, `v${c.version}`)
                : h('a', { href: href('/checklists/' + encodeURIComponent(c.id)) }, `v${c.version}`),
              ' ', h('span.muted.cl-ver-date', fmtDate(c.created_at))),
            provenanceBadge(c.provenance))))
        : h('p.muted.cl-tight', 'Будет создан новый чек-лист с версией v1.'),
      loaded ? h('details.disclosure.cl-tech',
        h('summary', 'Технические сведения'),
        h('dl.kv.cl-tech-kv',
          h('dt', 'ID версии'), h('dd', h('code', loaded.id)),
          h('dt', 'Семейство'), h('dd', h('code', loaded.family_id || loaded.id)))) : null));

  const critCount = h('span.cl-count');
  const addBtn = h('button.btn', {
    type: 'button',
    on: { click: () => { const r = toRow(null); r.weight = '1'; d.criteria.push(r); drawRows(r.key, 'id'); } },
  }, '+ Добавить критерий');

  mount(container,
    h('p.cl-back', h('a', { href: href('/checklists') }, '← Все чек-листы')),
    h('div.page-head.cqa-head.cl-head',
      h('div.cl-head-text',
        h('div.cl-title-row',
          h('h1', loaded ? loaded.name : 'Новый чек-лист'),
          loaded ? provenanceBadge(loaded.provenance) : null,
          loaded ? h('span.cl-pill.cl-pill-strong', `v${loaded.version}`) : null),
        h('p.page-sub.cl-sub', loaded
          ? `Черновик на основе v${loaded.version}. Сохранение создаст v${latestVersion + 1}; сохранённые версии не меняются.`
          : 'Заполните критерии и сохраните — будет создана версия v1.'))),
    loaded && loaded.version < latestVersion
      ? h('div.alert.alert-warn', `Это не последняя версия. Последняя — v${latestVersion}.`)
      : null,
    h('div.cqa-editor-layout',
      h('div.cqa-editor-main',
        h('section.panel.cl-panel', { 'aria-labelledby': 'cl-meta-h' },
          h('h2#cl-meta-h', 'Общие сведения'),
          h('div.cl-meta-grid',
            h('div.field.cl-meta-name', h('label', { for: 'cl-name' }, h('span', 'Название')), nameInput, topErr.name),
            h('div.field', h('label', { for: 'cl-prov' }, h('span', 'Происхождение')),
              h('select#cl-prov', { on: { change: (e) => { d.provenance = e.target.value; officialWarn.hidden = d.provenance !== 'official'; refresh(); } } },
                h('option', { value: 'demonstration', selected: d.provenance === 'demonstration' }, 'Демонстрационный пример'),
                h('option', { value: 'custom', selected: d.provenance === 'custom' }, 'Пользовательский'),
                h('option', { value: 'official', selected: d.provenance === 'official' }, 'Официальный (исходный preset)'))),
            h('div.field', h('label', { for: 'cl-decimals' }, h('span', 'Знаков после запятой')), decInput, topErr.decimals),
            h('div.field', h('label', { for: 'cl-policy' }, h('span', 'При грубой ошибке')),
              h('select#cl-policy', { on: { change: (e) => { d.policy = e.target.value; refresh(); } } },
                h('option', { value: 'flag', selected: d.policy === 'flag' }, 'Только отметка (итог не меняется)'),
                h('option', { value: 'zero_total', selected: d.policy === 'zero_total' }, 'Общий итог 0 (разделы сохраняются)')))),
          officialWarn),
        h('section.panel.cl-panel', { 'aria-labelledby': 'cl-crit-h' },
          h('div.cl-panel-head', h('h2#cl-crit-h', 'Критерии ', critCount)),
          sectionsList,
          rowsHost,
          h('div.cl-add', addBtn)),
        weightsHost,
        summaryHost,
        globalErr,
        serverErr,
        h('div.cqa-save-bar', saveBtn,
          h('span.muted.cl-save-note', loaded ? `Будет создана v${latestVersion + 1}. Текущая версия не изменится.` : 'Будет создан новый чек-лист v1.'),
          saveStatus)),
      history));

  drawRows();
  return () => { alive = false; };
}
