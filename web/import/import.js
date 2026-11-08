// Route #/import — DEV import via server, JSON import, sequential batch evaluation.
import { api } from '../app/api.js';
import { h, mount, errorBox, loading, toast, fmtScore } from '../app/dom.js';
import { href } from '../app/router.js';

const PROVENANCE_TEXT = {
  official: 'официальный (декларация пользователя)',
  demonstration: 'демонстрационный пример',
  custom: 'пользовательский',
};
const CHANNELS = [['agent chat', 'Чат с оператором'], ['voice call', 'Голосовой звонок']];
const MODEL_MISSING = 'модель не настроена';

function importResult(res) {
  const ids = (res && res.conversation_ids) || [];
  return h('div.alert.alert-info', { role: 'status' },
    h('strong', `Импортировано диалогов: ${res && res.imported != null ? res.imported : ids.length}`),
    ids.length ? h('details.cqa-ids', h('summary', 'ID диалогов'),
      h('ul', ids.slice(0, 200).map((id) => h('li', h('a', { href: '#/conversations/' + encodeURIComponent(id) }, id)))),
      ids.length > 200 ? h('p.muted', `… и ещё ${ids.length - 200}`) : null) : null);
}

function devSection(onImported) {
  const out = h('div', { 'aria-live': 'polite' });
  const group = h('input#imp-dev-group', { type: 'text', value: 'DEV', 'aria-label': 'group_id набора организаторов', autocomplete: 'off', spellcheck: 'false' });
  const btn = h('button.btn.btn-primary', { type: 'button', on: { click: run } }, 'Импорт набора через сервер');
  async function run() {
    const gid = group.value.trim();
    if (!gid) { mount(out, errorBox({ message: 'Укажите group_id (например, DEV или значение контрольного набора)' }, 'Нет group_id')); return; }
    btn.disabled = true;
    mount(out, loading(`Импорт набора ${gid}… (может занять до 2 минут)`));
    try {
      const res = await api.importDev(gid);
      mount(out, importResult(res));
      toast(`Набор ${gid} импортирован`, 'ok');
      onImported();
    } catch (e) {
      mount(out, errorBox(e, `Импорт набора ${gid} не выполнен`));
    } finally {
      btn.disabled = false;
    }
  }
  return h('section.cqa-source', { 'aria-labelledby': 'imp-dev-h' },
    h('h2#imp-dev-h.visually-hidden', 'Набор организаторов'),
    h('p.muted.cqa-lead', 'Сервер загрузит набор по group_id: DEV — тренировочный, для контрольного укажите значение от организаторов.'),
    h('div.cqa-inline-form',
      h('label.field.cqa-group-field', { for: 'imp-dev-group' }, h('span', 'group_id'), group), btn),
    out,
    h('details.disclosure', h('summary', 'Справка'),
      h('p', 'Адрес API настраивается только на сервере; интерфейс его не задаёт и не показывает. Сервер подставляет указанный group_id в запрос. Повторный импорт тех же диалогов безопасен. Загрузка может занять до 2 минут.')));
}

function jsonSection(onImported) {
  const out = h('div', { 'aria-live': 'polite' });
  const area = h('textarea.cqa-json', { rows: 8, spellcheck: 'false', placeholder: '{"records": [ … ]} или массив диалогов', 'aria-label': 'JSON набора диалогов' });
  const group = h('input', { type: 'text', value: 'JSON', 'aria-label': 'Группа данных' });
  const file = h('input', {
    type: 'file', accept: '.json,application/json', 'aria-label': 'JSON-файл',
    on: {
      change: async () => {
        const f = file.files && file.files[0];
        if (!f) return;
        try { area.value = await f.text(); mount(out, h('p.muted', `Файл загружен: ${f.name} (${f.size} байт)`)); }
        catch (e) { mount(out, errorBox(e, 'Не удалось прочитать файл')); }
      },
    },
  });
  const btn = h('button.btn.btn-primary', { type: 'button', on: { click: run } }, 'Импортировать JSON');
  async function run() {
    const text = area.value.trim();
    if (!text) { mount(out, errorBox({ message: 'Вставьте JSON или выберите файл' }, 'Нет данных')); return; }
    let payload;
    try { payload = JSON.parse(text); }
    catch (e) { mount(out, errorBox({ message: e.message }, 'Некорректный JSON')); return; }
    btn.disabled = true;
    mount(out, loading('Импорт…'));
    try {
      const res = await api.importJson(payload, group.value.trim() || 'JSON');
      mount(out, importResult(res));
      toast('JSON импортирован', 'ok');
      onImported();
    } catch (e) {
      mount(out, errorBox(e, 'Импорт отклонён сервером'));
    } finally {
      btn.disabled = false;
    }
  }
  return h('section.cqa-source', { 'aria-labelledby': 'imp-json-h' },
    h('h2#imp-json-h.visually-hidden', 'Файл или JSON'),
    h('p.muted.cqa-lead', 'Выберите .json-файл или вставьте содержимое ниже.'),
    h('div.cqa-inline-form',
      h('label.field.cqa-file-field', h('span', 'Файл .json'), file),
      h('label.field.cqa-group-field', h('span', 'Группа данных (group_id)'), group)),
    h('label.field.cqa-mt', h('span', 'Или вставьте JSON'), area),
    h('div.row.cqa-mt', btn), out,
    h('details.disclosure', h('summary', 'Справка: поддерживаемые форматы'),
      h('ul',
        h('li', 'официальный ответ API: {isSuccess, data: {records}};'),
        h('li', '{dataset_group_id?, records};'),
        h('li', 'массив диалогов.')),
      h('p', 'Каждый диалог: conversation_id, language (RU, KZ, EN), channel («agent chat» или «voice call»), transcript с timestamp, speaker и text. Повторный импорт тех же данных безопасен.')));
}

function batchSection() {
  const root = h('section.card.cqa-batch', { 'aria-labelledby': 'imp-batch-h' });
  const state = { checklists: [], conversations: [], selected: new Set(), language: '', channel: '', group: '', running: false, stop: false };
  const live = h('p.cqa-live', { role: 'status', 'aria-live': 'polite' });
  const progressHost = h('div');
  let checklistSel, listHost, runBtn, stopBtn, countEl;

  async function load() {
    mount(root, h('h2#imp-batch-h.cqa-step-title', h('span.cqa-step', { 'aria-hidden': 'true' }, '2'), 'Пакетная оценка'), loading('Загрузка чек-листов и диалогов…'));
    try {
      const [cls, convs] = await Promise.all([api.checklists(), api.conversations()]);
      // Official preset first, then by name and newest version.
      state.checklists = cls.slice().sort((a, b) => (b.provenance === 'official') - (a.provenance === 'official') || String(a.name).localeCompare(String(b.name)) || b.version - a.version);
      state.conversations = convs;
      const known = new Set(convs.map((c) => c.conversation_id));
      for (const id of [...state.selected]) if (!known.has(id)) state.selected.delete(id);
      draw();
    } catch (e) {
      mount(root, h('h2#imp-batch-h.cqa-step-title', h('span.cqa-step', { 'aria-hidden': 'true' }, '2'), 'Пакетная оценка'), errorBox(e, 'Не удалось загрузить данные'),
        h('button.btn', { type: 'button', on: { click: load } }, 'Повторить'));
    }
  }

  const filtered = () => state.conversations.filter((c) =>
    (!state.language || c.language === state.language) &&
    (!state.channel || c.channel === state.channel) &&
    (!state.group || c.dataset_group_id === state.group));

  function filterSelect(label, key, options) {
    return h('label.field.cqa-filter', h('span', label), h('select.chip-filter', {
      class: state[key] ? 'is-active' : '',
      on: { change: (e) => { state[key] = e.target.value; e.target.classList.toggle('is-active', Boolean(e.target.value)); drawList(); } },
    }, h('option', { value: '' }, 'Все'), options.map(([v, t]) => h('option', { value: v, selected: state[key] === v }, t))));
  }

  function updateCount() {
    if (countEl) countEl.textContent = `Выбрано: ${state.selected.size}`;
    if (runBtn) runBtn.disabled = state.running || !state.selected.size || !checklistSel || !checklistSel.value;
  }

  function drawList() {
    const rows = filtered();
    const allSel = rows.length > 0 && rows.every((c) => state.selected.has(c.conversation_id));
    mount(listHost,
      h('div.row.cqa-mt',
        h('button.btn.btn-small', { type: 'button', disabled: state.running || !rows.length, on: { click: () => { rows.forEach((c) => state.selected.add(c.conversation_id)); drawList(); } } }, 'Выбрать все отфильтрованные'),
        h('button.btn.btn-small', { type: 'button', disabled: state.running || !state.selected.size, on: { click: () => { state.selected.clear(); drawList(); } } }, 'Снять выбор'),
        h('span.muted', `Отфильтровано: ${rows.length}`)),
      rows.length ? h('div.cqa-pick-list', h('table.table',
        h('thead', h('tr',
          h('th', { scope: 'col' }, h('input', {
            type: 'checkbox', checked: allSel, disabled: state.running, 'aria-label': 'Выбрать все отфильтрованные',
            on: { change: (e) => { rows.forEach((c) => e.target.checked ? state.selected.add(c.conversation_id) : state.selected.delete(c.conversation_id)); drawList(); } },
          })),
          h('th', { scope: 'col' }, 'ID'), h('th', { scope: 'col' }, 'Язык'), h('th', { scope: 'col' }, 'Канал'), h('th', { scope: 'col' }, 'Группа'))),
        h('tbody', rows.map((c) => {
          const id = 'pick-' + encodeURIComponent(c.conversation_id).replace(/%/g, '_');
          return h('tr',
            h('td', h('input', {
              type: 'checkbox', id, checked: state.selected.has(c.conversation_id), disabled: state.running,
              on: { change: (e) => { e.target.checked ? state.selected.add(c.conversation_id) : state.selected.delete(c.conversation_id); updateCount(); } },
            })),
            h('td', h('label', { for: id }, c.conversation_id)), h('td', c.language || ''), h('td', c.channel || ''), h('td', c.dataset_group_id || ''));
        }))))
        : h('p.muted', state.conversations.length ? 'Нет диалогов под фильтры.' : 'Диалогов нет — сначала выполните импорт.'));
    updateCount();
  }

  function draw() {
    const groups = [...new Set(state.conversations.map((c) => c.dataset_group_id).filter(Boolean))].sort();
    const prev = checklistSel && checklistSel.value;
    const official = state.checklists.find((c) => c.id === 'official-cqa-v1') || state.checklists.find((c) => c.provenance === 'official');
    const preferred = state.checklists.some((c) => c.id === prev) ? prev : (official || state.checklists[0])?.id;
    checklistSel = h('select', { 'aria-label': 'Версия чек-листа', on: { change: updateCount } },
      state.checklists.map((c) => h('option', { value: c.id, selected: c.id === preferred },
        `${c.name} · v${c.version} · ${PROVENANCE_TEXT[c.provenance] || c.provenance || 'не указано'}`)));
    listHost = h('div');
    countEl = h('span.muted');
    runBtn = h('button.btn.btn-primary', { type: 'button', on: { click: runBatch } }, 'Оценить выбранные');
    stopBtn = h('button.btn.btn-danger', { type: 'button', disabled: !state.running, on: { click: requestStop } }, 'Остановить');
    mount(root,
      h('h2#imp-batch-h.cqa-step-title', h('span.cqa-step', { 'aria-hidden': 'true' }, '2'), 'Пакетная оценка'),
      h('p.muted.cqa-lead.cqa-step-lead', 'Выберите версию чек-листа и диалоги. Оценка идёт по одному диалогу, результат каждого виден сразу.'),
      state.checklists.length
        ? h('label.field.cqa-checklist-field', h('span', 'Версия чек-листа'), checklistSel)
        : h('div.alert.alert-warn', 'Нет сохранённых чек-листов. ', h('a', { href: href('/checklists/new') }, 'Создать чек-лист')),
      h('div.cqa-filters.cqa-mt',
        filterSelect('Язык', 'language', [['RU', 'RU'], ['KZ', 'KZ'], ['EN', 'EN']]),
        filterSelect('Канал', 'channel', CHANNELS),
        filterSelect('Группа данных', 'group', groups.map((g) => [g, g]))),
      listHost,
      h('div.cqa-run-bar', runBtn, stopBtn, countEl),
      h('p.muted.cqa-note', 'При уходе со страницы оценка остановится после текущего диалога.'),
      live,
      progressHost,
      h('details.disclosure', h('summary', 'Справка'),
        h('p', 'Диалоги оцениваются по очереди, по одному запросу на диалог (пакетного API нет). Каждый вызов модели синхронный и может занять до 2 минут. Ошибки показываются по каждому диалогу; оценки никогда не подставляются.')));
    drawList();
  }

  function requestStop() {
    state.stop = true;
    stopBtn.disabled = true;
    live.textContent = 'Остановка после текущего диалога…';
  }

  async function runBatch() {
    const checklistId = checklistSel && checklistSel.value;
    const ids = state.conversations.map((c) => c.conversation_id).filter((id) => state.selected.has(id));
    if (!checklistId || !ids.length) return;
    state.running = true;
    state.stop = false;
    drawList();
    runBtn.disabled = true;
    stopBtn.disabled = false;

    const rows = new Map();
    const hintHost = h('div');
    const statusCell = (text, cls) => h('span.badge.' + cls, text);
    const bar = h('progress.cqa-bar', { max: String(ids.length), value: '0', 'aria-label': 'Ход пакетной оценки' });
    const barText = h('span.cqa-bar-text', `0 из ${ids.length}`);
    const tick = (n) => { bar.value = n; barText.textContent = `${n} из ${ids.length}`; };
    mount(progressHost, h('div.cqa-progress-head', h('h3', 'Ход оценки'), barText), bar, hintHost, h('ol.cqa-progress', ids.map((id) => {
      const st = h('span.cqa-st', statusCell('ожидание', 'badge-neutral'));
      const detail = h('span.cqa-detail');
      rows.set(id, { st, detail });
      return h('li', h('span.cqa-pid', id), ' ', st, ' ', detail);
    })));

    let done = 0, failed = 0, skipped = 0;
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      const r = rows.get(id);
      if (state.stop) { skipped = ids.length - i; for (const rest of ids.slice(i)) mount(rows.get(rest).st, statusCell('не запущено', 'badge-neutral')); break; }
      mount(r.st, statusCell('выполняется', 'badge-warn'));
      live.textContent = `Оценивается ${i + 1} из ${ids.length}: ${id}`;
      try {
        const ev = await api.evaluate(id, checklistId);
        done++;
        const res = ev.final && ev.final.result;
        mount(r.st, statusCell('готово', 'badge-ok'));
        mount(r.detail, h('a', { href: href('/conversations/' + encodeURIComponent(id), { eval: ev.id }) },
          'итог: ' + fmtScore(res ? res.total : null, 2)),
          res && res.gross_error ? [' ', h('span.badge.badge-gross', 'грубая ошибка')] : null);
        tick(i + 1);
      } catch (e) {
        failed++;
        mount(r.st, statusCell('ошибка', 'badge-error'));
        mount(r.detail, h('span.cqa-err', e.message || String(e)));
        tick(i + 1);
        if (String(e.message || '').toLowerCase().includes(MODEL_MISSING)) {
          state.stop = true;
          mount(hintHost, h('div.alert.alert-warn.cqa-model-hint', { role: 'alert' },
            'Пакет остановлен: модель не настроена. Настройте CQA_MODEL и CQA_OLLAMA_URL на сервере, выполните python -m server.model_check и запустите пакет повторно.'));
        }
      }
    }
    state.running = false;
    live.textContent = `Завершено. Готово: ${done}, ошибок: ${failed}` + (skipped ? `, не запущено: ${skipped}` : '') + '.';
    if (root.isConnected) { drawList(); stopBtn.disabled = true; }
  }

  load();
  return { el: root, reload: load, stop: () => { state.stop = true; }, isRunning: () => state.running };
}

function exportSummary(doc, dataset) {
  const n = doc.results.length;
  const ok = doc.results.filter((r) => r && r.status === 'ok').length;
  const err = doc.results.filter((r) => r && r.status === 'error').length;
  return [
    h('div.alert.alert-info', { role: 'status' },
      h('strong', `Записей: ${n}`), h('br'),
      `ok: ${ok}, error: ${err}`, h('br'),
      'Положите файл в ', h('code', `results/${dataset}/results.json`)),
    dataset === 'prod' && n !== 10
      ? h('div.alert.alert-warn', `В контрольном наборе должно быть ровно 10 записей; получено ${n}.`) : null,
  ];
}

function downloadJson(doc) {
  const blob = new Blob([JSON.stringify(doc, null, 2) + '\n'], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: 'results.json', hidden: true });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function exportSection() {
  const out = h('div#imp-exp-out', { 'aria-live': 'polite' });
  const dataset = h('select#imp-exp-dataset', { on: { change: onDataset } },
    h('option', { value: 'dev', selected: true }, 'DEV — results/dev/results.json'),
    h('option', { value: 'prod' }, 'Контрольный (prod) — results/prod/results.json'));
  const group = h('input#imp-exp-group', { type: 'text', value: 'DEV', autocomplete: 'off', spellcheck: 'false' });
  const team = h('input#imp-exp-team', { type: 'text', value: 'retake factory', autocomplete: 'off', spellcheck: 'false' });
  const btn = h('button#imp-exp-download.btn.btn-primary', { type: 'button', on: { click: run } }, 'Скачать results.json');

  function onDataset() {
    const v = group.value.trim();
    if (dataset.value === 'prod' && v === 'DEV') group.value = '';
    else if (dataset.value === 'dev' && !v) group.value = 'DEV';
  }

  async function run() {
    const ds = dataset.value === 'prod' ? 'prod' : 'dev';
    const gid = group.value.trim();
    const teamName = team.value.trim();
    if (!teamName) { mount(out, errorBox({ message: 'Укажите название команды' }, 'Выгрузка не выполнена')); return; }
    if (!gid) {
      mount(out, errorBox({ message: ds === 'prod' ? 'Для контрольного набора укажите group_id от организаторов' : 'Укажите group_id' }, 'Выгрузка не выполнена'));
      return;
    }
    btn.disabled = true;
    mount(out, loading('Формирование results.json…'));
    try {
      const doc = await api.exportResults({ dataset: ds, group_id: gid, team_name: teamName, reviewed: 0 });
      if (!doc || typeof doc !== 'object' || doc.schema_version !== '1.0' || !Array.isArray(doc.results)) {
        mount(out, errorBox({ message: 'Сервер вернул документ не в формате JSON 1.0' }, 'Выгрузка не выполнена'));
        return;
      }
      const expectedCount = ds === 'prod' ? 10 : 65;
      if (doc.results.length !== expectedCount) {
        throw new Error(`Для ${ds.toUpperCase()} требуется ровно ${expectedCount} записей; получено ${doc.results.length}. Файл не скачан.`);
      }
      downloadJson(doc);
      mount(out, exportSummary(doc, ds));
      toast('results.json сформирован', 'ok');
    } catch (e) {
      mount(out, errorBox(e, 'Выгрузка не выполнена'));
    } finally {
      btn.disabled = false;
    }
  }

  return h('section.card.cqa-export', { 'aria-labelledby': 'imp-exp-h' },
    h('h2#imp-exp-h.cqa-step-title', h('span.cqa-step', { 'aria-hidden': 'true' }, '3'), 'Выгрузка результатов'),
    h('p.muted.cqa-lead.cqa-step-lead', 'Сервер соберёт официальный results.json (JSON 1.0) из сохранённых оценок выбранного набора.'),
    h('div.cqa-inline-form',
      h('label.field', { for: 'imp-exp-dataset' }, h('span', 'Набор'), dataset),
      h('label.field.cqa-group-field', { for: 'imp-exp-group' }, h('span', 'group_id'), group),
      h('label.field.cqa-group-field', { for: 'imp-exp-team' }, h('span', 'Название команды'), team),
      btn),
    out,
    h('details.disclosure', h('summary', 'Справка'),
      h('p', 'Файл формирует сервер из сохранённых оценок; интерфейс ничего не дополняет и не изменяет. Значения N/A (null) не учитываются в оценке согласно приложению. Диалоги, оценка которых не удалась, выгружаются со статусом "error", а не с нулевой оценкой. Файл не коммитится в Git автоматически — скопируйте его в results/dev или results/prod и отправьте (push) вручную.')));
}

function sourcePicker(dev, json) {
  const panels = [
    ['dev', 'Набор организаторов', dev],
    ['json', 'Файл или JSON', json],
  ];
  const tabs = panels.map(([key, label], i) => h('button', {
    type: 'button', role: 'tab', id: 'imp-tab-' + key, 'aria-controls': 'imp-panel-' + key,
    'aria-selected': i === 0 ? 'true' : 'false', tabindex: i === 0 ? '0' : '-1',
    on: { click: () => select(i), keydown: onKey },
  }, label));
  const bodies = panels.map(([key, , el], i) => h('div.cqa-source-panel', {
    role: 'tabpanel', id: 'imp-panel-' + key, 'aria-labelledby': 'imp-tab-' + key, hidden: i !== 0,
  }, el));
  function select(i, focus) {
    tabs.forEach((t, j) => { t.setAttribute('aria-selected', String(i === j)); t.tabIndex = i === j ? 0 : -1; });
    bodies.forEach((b, j) => { b.hidden = i !== j; });
    if (focus) tabs[i].focus();
  }
  function onKey(e) {
    const cur = tabs.indexOf(e.currentTarget);
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();
      select((cur + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length, true);
    }
  }
  return h('section.card.cqa-sources', { 'aria-labelledby': 'imp-src-h' },
    h('h2#imp-src-h.cqa-step-title', h('span.cqa-step', { 'aria-hidden': 'true' }, '1'), 'Импорт диалогов'),
    h('p.muted.cqa-lead.cqa-step-lead', 'Выберите источник. Уже загруженные диалоги можно сразу оценить ниже.'),
    h('div.tabs.cqa-source-tabs', { role: 'tablist', 'aria-label': 'Источник данных' }, tabs),
    bodies);
}

export async function render(container) {
  const batch = batchSection();
  const refresh = () => { if (!batch.isRunning()) batch.reload(); };
  mount(container,
    h('div.page-head',
      h('div', h('h1', 'Импорт и оценка'),
        h('p.page-sub', 'Загрузите диалоги и оцените их по выбранной версии чек-листа.'))),
    sourcePicker(devSection(refresh), jsonSection(refresh)),
    batch.el,
    exportSection());
  return () => batch.stop();
}
