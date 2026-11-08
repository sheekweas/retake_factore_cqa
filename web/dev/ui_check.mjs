// Full scripted browser acceptance of the demo flow. ONLY runs against the labelled synthetic
// fixture server (ТЕСТОВЫЙ ПРОВАЙДЕР, separate DB under .local/ui-fixture) which this script starts
// with --fresh and stops at the end. No real data, no model, no network beyond 127.0.0.1.
//
//   node web/dev/ui_check.mjs            (Node >= 22, Chrome or Edge; CQA_CHROME / CQA_PYTHON overrides)
//
// Steps: a) JSON import via UI form, b) list filters/search, c) batch evaluation with official-cqa-v1,
// d) evidence link + ?msg= deep link, e) appeal + discrete review, f) new checklist version with a new
// criterion through the editor + evaluation, g) browser errors. Also compares the official preset as
// shown by the editor/result UI with tests/fixtures/checklist.txt.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { launch, sleep } from './cdp.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const PORT = Number(process.env.CQA_UI_CHECK_PORT || 8037);
const base = `http://127.0.0.1:${PORT}`;
const GROUP = 'UI_CHECK_DEMO';
const LABEL = 'демонстрационный пример — синтетические данные UI-проверки, не реальные диалоги';
const findings = [];   // mismatches outside this script's write scope: reported, not fatal
const passed = [];
const ok = (msg) => { passed.push(msg); console.log('  ok  ' + msg); };

// ---------- synthetic demonstration dataset (explicitly labelled in payload) ----------
const DATASET = {
  dataset_group_id: GROUP,
  note: LABEL,
  records: [
    { conversation_id: 'ui-demo-ru', channel: 'agent chat', language: 'RU', audio_url: null, label: LABEL, transcript: [
      { timestamp: '2026-11-07T09:00:00Z', speaker: 'system', text: 'Демонстрационный синтетический чат открыт.' },
      { timestamp: '2026-11-07T09:00:02Z', speaker: 'bot', text: 'Здравствуйте! Соединяю с оператором.' },
      { timestamp: '2026-11-07T09:00:10Z', speaker: 'client', text: 'Не приходит код подтверждения.' },
      { timestamp: '2026-11-07T09:00:40Z', speaker: 'operator', text: 'Здравствуйте, меня зовут Анна. Номер телефона в профиле совпадает с текущим?' },
      { timestamp: '2026-11-07T09:01:05Z', speaker: 'client', text: 'Да, совпадает.' },
      { timestamp: '2026-11-07T09:01:30Z', speaker: 'operator', text: 'Запросите код повторно через 60 секунд. Если не придёт — напишите. Хорошего дня!' },
    ] },
    { conversation_id: 'ui-demo-kz', channel: 'agent chat', language: 'KZ', audio_url: null, label: LABEL, transcript: [
      { timestamp: '00:00', speaker: 'bot', text: 'Сәлеметсіз бе! Операторға қосып жатырмын.' },
      { timestamp: '00:07', speaker: 'client', text: 'Шот үзіндісін қалай алуға болады?' },
      { timestamp: '00:31', speaker: 'operator', text: 'Сәлеметсіз бе, менің атым Айгүл. Қосымшадағы Құжаттар бөлімін ашыңыз.' },
      { timestamp: '01:02', speaker: 'system', text: 'Чат жабылды.' },
    ] },
    { conversation_id: 'ui-demo-en', channel: 'voice call', language: 'EN', audio_url: null, label: LABEL, transcript: [
      { timestamp: '0:00', speaker: 'system', text: 'Synthetic demonstration call transcript.' },
      { timestamp: '0:04', speaker: 'client', text: 'Hello, how do I change my tariff?' },
      { timestamp: '0:09', speaker: 'operator', text: 'Hello, this is Mark. Open Settings, then Tariffs, and pick a new plan.' },
      { timestamp: '0:21', speaker: 'client', text: 'Thanks.' },
      { timestamp: '0:25', speaker: 'operator', text: 'Anything else I can help with? Goodbye!' },
    ] },
  ],
};

// ---------- expected official preset, parsed independently from the PDF text extraction ----------
function parsePdfChecklist() {
  const txt = readFileSync(join(ROOT, 'tests', 'fixtures', 'checklist.txt'), 'utf8').replace(/\r/g, '');
  const [p1, p2] = txt.split('--- PAGE 2 ---');
  const crits = [];
  const re = /^(0[1-8])\n(.+)\n(.+?)\s+·\s+(?:ВЕС (\d+)|БЕЗ БАЛЛОВ)\n([\s\S]*?)(?=\n0[1-8]\n|\n[А-ЯЁ ]{8,}\n|\nВес показывает)/gm;
  for (const m of p1.matchAll(re)) {
    const body = m[5].replace(/\n/g, ' ').replace(/\s+/g, ' ').trim();
    const naIdx = body.indexOf('N/A —');
    crits.push({
      id: m[1], title: m[2].trim(), weight: m[4] === undefined ? 0 : Number(m[4]),
      allow_na: /N\/A/.test(m[3]), numeric: /\d–\d/.test(m[3]),
      range: (m[3].match(/(\d+)–(\d+)/) || []).slice(1).map(Number),
      yes: body.slice(0, naIdx >= 0 ? naIdx : undefined).trim(),
      na: naIdx >= 0 ? body.slice(naIdx + 'N/A —'.length).trim() : '',
    });
  }
  const scale = (id, next) => {
    const start = p2.indexOf(id + '   ');
    const end = next ? p2.indexOf(next + '   ') : p2.indexOf('ПОРЯДОК РАСЧЁТА');
    const lines = p2.slice(start, end).split('\n').slice(1).map((s) => s.trim()).filter(Boolean);
    const out = {};
    for (let i = 0; i < lines.length; i += 2) out[lines[i]] = lines[i + 1];
    return out;
  };
  return { crits, scales: { '04': scale('04', '06'), '06': scale('06', null) } };
}

const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase().replace(/\.$/, '');

// ---------- fixture server ----------
async function startFixture() {
  const py = process.env.CQA_PYTHON || 'python';
  const proc = spawn(py, [join(ROOT, 'web', 'dev', 'fixture_server.py'), '--port', String(PORT), '--fresh'], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
  let log = '';
  proc.stdout.on('data', (d) => { log += d; });
  proc.stderr.on('data', (d) => { log += d; });
  for (let i = 0; i < 100; i++) {
    try { if ((await fetch(base + '/api/health')).ok) return proc; } catch { /* starting */ }
    if (proc.exitCode !== null) break;
    await sleep(150);
  }
  proc.kill();
  throw new Error('fixture server did not start:\n' + log);
}

const api = async (path, body, expect = 200) => {
  const r = await fetch(base + '/api/' + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  const data = await r.json();
  assert.equal(r.status, expect, path + ' ' + JSON.stringify(data));
  return data;
};

// Helpers injected into every page evaluation.
const H = `const $ = (s, r = document) => r.querySelector(s); const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const byText = (sel, t, r = document) => $$(sel, r).find((e) => e.textContent.trim() === t);
const fieldByLabel = (t, r = document) => { const l = $$('label.field', r).find((x) => x.querySelector('span')?.textContent.trim() === t); return l && l.querySelector('select,input,textarea'); };
const setVal = (el, v, ev = 'input') => { el.value = v; el.dispatchEvent(new Event(ev, { bubbles: true })); return el.value; };`;

const server = await startFixture();
const page = await launch({ profileDir: join(ROOT, '.local', 'ui-check-browser'), port: Number(process.env.CQA_UI_CHECK_CDP || 9353) });
const E = (js) => page.eval(`(() => { ${H}\n${js} })()`);
const W = (js, t) => page.waitFor(`(() => { ${H}\nreturn ${js}; })()`, t);
const expectedErrors = [];

try {
  // ================= a. JSON import through the UI form =================
  console.log('a. import');
  await page.goto(base + '/#/import');
  await W(`$('textarea[aria-label="JSON набора диалогов"]') && $('select[aria-label="Версия чек-листа"]')`);
  assert.equal(await E(`return $('#imp-dev-group').value`), 'DEV');
  await E(`setVal($('#imp-dev-group'), ''); byText('button', 'Импорт набора через сервер').click()`);
  await W(`$('#imp-dev-h').parentElement.querySelector('.alert-error')?.textContent.includes('Нет group_id')`);
  await E(`setVal($('#imp-dev-group'), 'CONTROL_UI_TEST')`);
  assert.equal(await E(`return $('#imp-dev-group').value`), 'CONTROL_UI_TEST');
  await E(`setVal($('#imp-dev-group'), 'DEV')`);
  ok('organizer group_id defaults to DEV, is editable for control data, and empty value is rejected before requesting data');
  const totals = await E(`return import('/results/answer-format.js').then(({ formatTotal }) => [50, 0, 50.12, null, undefined].map(formatTotal))`);
  assert.deepEqual(totals, ['50.00', '0.00', '50.12', '— (все критерии N/A)', '— (все критерии N/A)']);
  ok('total formatting preserves two decimal places and null/undefined labels');
  // Render the real list module with explicit in-memory fixtures, including whole-number totals.
  const listFormat = await E(`return Promise.all([import('/app/api.js'), import('/import/conversations.js')]).then(async ([{ api }, { render }]) => {
    const originalConversations = api.conversations, originalEvaluations = api.evaluations;
    try {
      api.conversations = async () => [{ conversation_id: 'format-total-50' }, { conversation_id: 'format-total-null' }];
      api.evaluations = async () => [
        { id: 'format-50', conversation_id: 'format-total-50', created_at: '2026-11-07T09:00:00Z', final: { result: { total: 50 } }, original: { result: { total: 40 } } },
        { id: 'format-null', conversation_id: 'format-total-null', created_at: '2026-11-07T09:00:00Z', final: { result: { total: null } }, original: { result: { total: null } } }
      ];
      const host = document.createElement('div');
      const cleanup = await render(host, { query: {} });
      const result = { totals: $$('.cqa-score-cell strong', host).map((el) => el.textContent), tooltip: $('.badge-warn', host).title, nullLabel: $$('.cqa-score-cell', host)[1].textContent };
      cleanup(); return result;
    } finally { api.conversations = originalConversations; api.evaluations = originalEvaluations; }
  })`);
  assert.deepEqual(listFormat.totals, ['50.00', '—']);
  assert.equal(listFormat.tooltip, 'Исходный итог: 40.00');
  assert.match(listFormat.nullLabel, /нет применимых критериев/);
  ok('conversation list renders 50 as 50.00, original tooltip 40.00, and null as an applicability label');
  // Russian presentation of a server validation error (language outside RU/KZ/EN).
  const bad = { dataset_group_id: GROUP, note: LABEL, records: [{ ...DATASET.records[0], conversation_id: 'ui-demo-bad', language: 'DE' }] };
  const errStart = page.errors.length;
  await E(`setVal($('textarea[aria-label="JSON набора диалогов"]'), ${JSON.stringify(JSON.stringify(bad))}); byText('button', 'Импортировать JSON').click();`);
  await W(`$('#imp-json-h').parentElement.querySelector('.alert-error')`);
  const badText = await E(`return $('#imp-json-h').parentElement.querySelector('.alert-error').textContent`);
  assert.match(badText, /Импорт отклонён сервером/);
  assert.match(badText, /язык/i, 'server validation error should be shown in Russian: ' + badText);
  assert.doesNotMatch(badText, /Unsupported conversation language/);
  await sleep(200);
  // Chrome reports the 422 response itself as a resource error: this one is expected.
  expectedErrors.push(...page.errors.splice(errStart).filter((e) => /status of 422/.test(e)));
  ok('server validation error (422) shown in Russian: ' + badText.replace(/\s+/g, ' ').trim());

  await E(`setVal(fieldByLabel('Группа данных (group_id)'), ${JSON.stringify(GROUP)});
    setVal($('textarea[aria-label="JSON набора диалогов"]'), ${JSON.stringify(JSON.stringify(DATASET, null, 2))});
    byText('button', 'Импортировать JSON').click();`);
  await W(`$('#imp-json-h').parentElement.querySelector('.alert-info')?.textContent.includes('Импортировано диалогов: 3')`);
  assert.deepEqual(await E(`return $$('.cqa-ids li a', $('#imp-json-h').parentElement).map((a) => a.textContent)`), ['ui-demo-ru', 'ui-demo-kz', 'ui-demo-en']);
  ok('JSON import form imported 3 labelled demonstration conversations');

  await page.goto(base + '/#/conversations');
  await W(`$$('tbody tr').length >= 6`);
  const rows = await E(`return Object.fromEntries($$('tbody tr').map((tr) => { const td = $$('td', tr).map((x) => x.textContent.trim()); return [td[0], td]; }))`);
  for (const r of DATASET.records) {
    const row = rows[r.conversation_id];
    assert(row, 'list lacks ' + r.conversation_id);
    assert.equal(row[1], r.language); assert.equal(row[2], r.channel); assert.equal(row[3], String(r.transcript.length)); assert.equal(row[4], GROUP);
    assert.equal(row[6], 'не оценён');
  }
  ok('conversation list shows the 3 imported conversations with language/channel/count/group');

  for (const r of DATASET.records) {
    await page.goto(base + '/#/conversations/' + r.conversation_id);
    await W(`$$('.transcript-list .msg').length === ${r.transcript.length}`);
    const msgs = await E(`return $$('.transcript-list .msg').map((li) => ({ role: li.className.match(/msg-(operator|client|bot|system)\\b/)[1], ts: $('.msg-ts', li).dataset.sourceTs ?? $('.msg-ts', li).textContent, shown: $('.msg-ts', li).textContent, text: $('.msg-text', li).textContent, lang: $('.msg-text', li).getAttribute('lang') }))`);
    assert.deepEqual(msgs.map((m) => [m.role, m.ts, m.text]), r.transcript.map((m) => [m.speaker, m.timestamp, m.text]));
    assert(msgs.every((m) => !/\d{4}-\d{2}-\d{2}T/.test(m.shown) && m.shown.length <= 8), 'visible message time must be compact, source ISO kept in data-source-ts/title');
    assert(msgs.every((m) => m.lang === { RU: 'ru', KZ: 'kk', EN: 'en' }[r.language]));
    assert(await E(`return document.body.innerText.includes(${JSON.stringify(LABEL)})`), 'demonstration label badge missing');
    assert.equal(await E(`return $('.transcript-head dl').textContent.includes(${JSON.stringify(r.language)})`), true);
  }
  ok('transcripts: roles operator/client/bot/system, original timestamps verbatim in data-source-ts, compact visible time (ISO, MM:SS, M:SS), lang attr, label badge');

  // ================= b. filters and search =================
  console.log('b. filters');
  await page.goto(base + '/#/conversations');
  await W(`$$('tbody tr').length >= 6`);
  const ids = () => E(`return $$('tbody tr td:first-child').map((t) => t.textContent.trim()).sort()`);
  await E(`setVal(fieldByLabel('Язык'), 'KZ', 'change')`);
  await W(`$$('tbody tr').length && $$('tbody tr td:nth-child(2)').every((t) => t.textContent === 'KZ')`);
  assert((await ids()).includes('ui-demo-kz')); assert(!(await ids()).includes('ui-demo-ru'));
  assert.match(await E(`return location.hash`), /language=KZ/);
  await E(`setVal(fieldByLabel('Язык'), '', 'change'); setVal(fieldByLabel('Канал'), 'voice call', 'change')`);
  await W(`$$('tbody tr').length === 1`);
  assert.deepEqual(await ids(), ['ui-demo-en']);
  await E(`setVal(fieldByLabel('Канал'), '', 'change'); setVal(fieldByLabel('Группа данных'), ${JSON.stringify(GROUP)}, 'change')`);
  await W(`$$('tbody tr').length === 3`);
  assert.deepEqual(await ids(), ['ui-demo-en', 'ui-demo-kz', 'ui-demo-ru']);
  await E(`setVal(fieldByLabel('Поиск'), 'DEMO-K')`);
  await W(`$$('tbody tr').length === 1`);
  assert.deepEqual(await ids(), ['ui-demo-kz']);
  assert.match(await E(`return $('p.muted[role=status]').textContent`), /Показано: 1 из \d+/);
  await E(`setVal(fieldByLabel('Поиск'), 'nothing-matches')`);
  await W(`document.body.innerText.includes('Нет диалогов, подходящих под фильтры')`);
  // Filters survive a reload through the URL.
  await page.goto('about:blank');
  await page.goto(base + `/#/conversations?dataset_group_id=${GROUP}&q=ru`);
  await W(`$$('tbody tr').length === 1`);
  assert.deepEqual(await ids(), ['ui-demo-ru']);
  ok('filters language/channel/group + case-insensitive ID search + empty state + URL-restored filters');

  // ================= c. batch evaluation with official-cqa-v1 =================
  console.log('c. batch evaluation');
  await page.goto(base + '/#/import');
  await W(`$('select[aria-label="Версия чек-листа"]') && $$('.cqa-pick-list tbody tr').length >= 6`);
  await E(`setVal($('select[aria-label="Версия чек-листа"]'), 'official-cqa-v1', 'change');
    setVal(fieldByLabel('Группа данных', $('#imp-batch-h').parentElement), ${JSON.stringify(GROUP)}, 'change');`);
  await W(`$$('.cqa-pick-list tbody tr').length === 3`);
  await E(`byText('button', 'Выбрать все отфильтрованные').click()`);
  await W(`byText('button', 'Оценить выбранные') && !byText('button', 'Оценить выбранные').disabled`);
  // Explicitly mocked missing-model response: no actual inference request is sent.
  await E(`return import('/app/api.js').then(({ api }) => {
    window.__evaluate = api.evaluate; window.__missingCalls = 0;
    api.evaluate = async () => { window.__missingCalls++; throw Object.assign(new Error('модель не настроена'), { status: 422 }); };
  })`);
  await E(`byText('button', 'Оценить выбранные').click()`);
  await W(`$('.cqa-live')?.textContent === 'Завершено. Готово: 0, ошибок: 1, не запущено: 2.'`);
  assert.equal(await E(`return window.__missingCalls`), 1);
  assert.deepEqual(await E(`return $$('.cqa-progress .cqa-st').map((e) => e.textContent)`), ['ошибка', 'не запущено', 'не запущено']);
  assert.equal(await E(`return $$('.cqa-model-hint').length`), 1);
  assert.match(await E(`return $('.cqa-model-hint').textContent`), /CQA_MODEL.*CQA_OLLAMA_URL.*server.model_check/);
  assert.equal(await E(`return Boolean(byText('button', 'Продолжить всё равно'))`), false);
  await E(`return import('/app/api.js').then(({ api }) => { api.evaluate = window.__evaluate; delete window.__evaluate; delete window.__missingCalls; })`);
  ok('mock missing-model error stops the batch after one attempt, leaves remaining rows unstarted, and displays one configuration hint');
  await E(`byText('button', 'Оценить выбранные').click()`);
  await W(`$('.cqa-live')?.textContent.startsWith('Завершено')`, 30000);
  assert.equal(await E(`return $('.cqa-live').textContent`), 'Завершено. Готово: 3, ошибок: 0.');
  const batch = await E(`return $$('.cqa-progress li').map((li) => ({ id: $('.cqa-pid', li).textContent, st: $('.cqa-st', li).textContent, href: $('.cqa-detail a', li)?.getAttribute('href'), gross: Boolean($('.badge-gross', li)) }))`);
  assert.deepEqual(batch.map((b) => [b.id, b.st]), [['ui-demo-ru', 'готово'], ['ui-demo-kz', 'готово'], ['ui-demo-en', 'готово']]);
  assert(batch.every((b) => /eval=/.test(b.href)));
  ok('batch evaluation of 3 conversations with official-cqa-v1 via UI, per-row links');

  const ruLink = batch[0].href;
  await page.goto(base + '/#/conversations');
  await W(`$$('.cqa-score-cell strong').length === 3`);
  const listTotals = await E(`return $$('.cqa-score-cell strong').map((el) => el.textContent)`);
  for (const total of listTotals) assert.match(total, /^\d{1,3}\.\d{2}$/, 'conversation list total: ' + total);
  const evalId = new URLSearchParams(ruLink.split('?')[1]).get('eval');
  const evApi = await api('evaluations/' + encodeURIComponent(evalId));
  assert.equal(evApi.checklist_id, 'official-cqa-v1');
  await page.goto(base + '/' + ruLink);
  await W(`$$('.crit').length === 8`);
  const card = await E(`return {
    ids: $$('.crit').map((c) => c.id.replace('crit-', '')),
    titles: $$('.crit .crit-title').map((t) => t.textContent),
    weights: $$('.crit').map((c) => { const dd = $$('dt', c).find((d) => d.textContent.startsWith('Вес'))?.nextElementSibling; return dd ? dd.textContent : ''; }),
    scores: Object.fromEntries($$('.crit').map((c) => [c.id.replace('crit-', ''), $$('dt', c).find((d) => d.textContent === 'Балл')?.nextElementSibling.textContent])),
    total: $('.total-final .score-big').textContent,
    gross: Boolean($('.gross-banner')), grossText: $('.gross-banner')?.textContent || '',
    na02: $('#crit-02 .ans-na-reason')?.textContent || '', na02class: $('#crit-02').classList.contains('crit-na'),
    ev01: $$('#crit-01 .chip-evidence').map((b) => b.title),
    missed03: Boolean($('#crit-03 .ans-missed button')),
    tag08: $('#crit-08 .crit-title').textContent.includes('Грубые ошибки') && $('#crit-08').textContent.includes('без баллов'),
    gross08: $('#crit-08').classList.contains('crit-gross'),
    provenance: $('.eval-meta .badge')?.textContent,
    testProvider: document.body.innerText.includes('ТЕСТОВЫЙ ПРОВАЙДЕР'),
    providerMeta: $('.eval-meta .eval-provider')?.textContent || '',
  }`);
  assert.deepEqual(card.ids, ['01', '02', '03', '04', '05', '06', '07', '08']);
  const t = evApi.final.result.total;
  if (t === null) assert.match(card.total, /все критерии N\/A/);
  else { assert.match(card.total, /^\d{1,3}\.\d{2}$/, 'total must have exactly 2 decimals: ' + card.total); assert.equal(Number(card.total), t); }
  const sectionTotals = await E(`return $$('.sections-table tbody tr').map((tr) => $$('td', tr)[1].textContent)`);
  for (const total of sectionTotals) assert.match(total, /^(?:\d{1,3}\.\d{2}|— \(все критерии N\/A\))$/, 'section total: ' + total);
  assert.equal(card.gross, true); assert.match(card.grossText, /ГРУБАЯ ОШИБКА/); assert.match(card.grossText, /flag/);
  assert.equal(card.tag08, true); assert.equal(card.gross08, true);
  assert.equal(card.na02class, true); assert.match(card.na02, /Причина N\/A: ТЕСТОВЫЙ ПРОВАЙДЕР/);
  assert.equal(card.scores['02'], '— (N/A, не учитывается)');
  assert(card.ev01.length > 0 && card.ev01.every((x) => x.startsWith('Показать сообщение ui-demo-ru:')));
  assert.equal(card.missed03, true);
  assert.equal(card.provenance, 'официальный чек-лист');
  assert.equal(card.testProvider, true);
  assert.deepEqual(evApi.provider, { type: 'injected' });
  assert.match(card.providerMeta, /ТЕСТОВЫЙ ПРОВАЙДЕР \(injected\)/);
  if (card.scores['08'] !== undefined && card.scores['08'] !== '—' && !/без балл/i.test(card.scores['08'])) {
    findings.push(`result card shows "Балл ${card.scores['08']}" for 08 «Грубые ошибки», which the PDF defines as a flag "БЕЗ БАЛЛОВ" (weight 0, contribution 0); web/results scope`);
  }
  ok(`result card: 8 criteria 01..08, total ${card.total}, gross banner + 08 flag, N/A reason row on 02, evidence chips, missed-span link, provider metadata ${card.providerMeta}`);

  // The supporting quote must be the source message, with its original timestamp and role,
  // not just an opaque message ID or an invented quotation from the model's explanation.
  const quoted = await E(`return $$('#crit-01 .evidence-fragment').map((el) => ({
    id: el.dataset.msgId, time: $('.evidence-timestamp', el).dataset.sourceTs ?? $('.evidence-timestamp', el).textContent,
    speaker: $('.evidence-role', el).dataset.speaker, text: $('.evidence-text', el).textContent,
  }))`);
  const expectedEvidenceIds = evApi.final.answers.find((a) => a.criterion_id === '01').evidence_message_ids;
  assert.deepEqual(quoted.map((q) => q.id), expectedEvidenceIds);
  for (const q of quoted) {
    const source = DATASET.records[0].transcript[Number(q.id.split(':').at(-1))];
    assert.equal(q.time, source.timestamp);
    assert.equal(q.speaker, source.speaker);
    assert.equal(q.text, source.text);
  }
  assert.equal(await E(`return $$('#crit-03 .checked-span-fragments .evidence-fragment').length`), 2);
  assert.match(card.scores['08'], /без баллов/, '08 is a flag, not a 100-point score');
  // Exercise untrusted excerpt text without changing the dataset or executing a real model.
  const safeQuote = await E(`return import('/results/answer-format.js').then(({answerDetails}) => {
    const malicious = '<img src=x onerror="window.__quoteInjection=1">' + 'x'.repeat(500);
    const block = answerDetails({answer:true, evidence_message_ids:['safe:0']}, {
      conversation:{transcript:[{id:'safe:0', timestamp:'00:09', speaker:'operator', text:malicious}]}
    });
    document.body.append(block);
    const out = {htmlNodes:block.querySelectorAll('img,script').length,
      text:block.querySelector('.evidence-text').textContent, injected:Boolean(window.__quoteInjection)};
    block.remove(); return out; });`);
  assert.equal(safeQuote.htmlNodes, 0);
  assert.equal(safeQuote.injected, false);
  assert(safeQuote.text.startsWith('<img src=x onerror='));
  assert(safeQuote.text.length < 300 && safeQuote.text.endsWith('…'));
  ok('inline source excerpts preserve original timestamp/role/text; missed-span endpoints shown; long HTML-like quotes remain safe text; 08 is flag-only');

  // Official preset displayed by the result UI vs PDF.
  const pdf = parsePdfChecklist();
  assert.equal(pdf.crits.length, 8, 'PDF parse');
  assert.deepEqual(pdf.crits.map((c) => c.weight), [10, 15, 20, 20, 15, 15, 5, 0]);
  pdf.crits.forEach((c, i) => {
    if (card.titles[i] !== c.title) findings.push(`result title ${c.id}: UI "${card.titles[i]}" vs PDF "${c.title}"`);
    if (!card.weights[i].startsWith(String(c.weight) + ' →')) findings.push(`result weight ${c.id}: UI "${card.weights[i]}" vs PDF ${c.weight}`);
  });

  // ================= d. evidence link -> highlighted message, ?msg= deep link =================
  console.log('d. evidence');
  const evId = card.ev01[0].replace('Показать сообщение ', '');
  await E(`$('#crit-01 .chip-evidence').click()`);
  await W(`$('.msg.is-highlighted')`);
  assert.deepEqual(await E(`return $$('.msg.is-highlighted').map((m) => m.dataset.msgId)`), [evId]);
  assert.equal(await E(`return document.activeElement.dataset.msgId`), evId);
  assert.equal(new URLSearchParams((await E(`return location.hash`)).split('?')[1]).get('msg'), evId);
  await E(`$('#crit-03 .ans-missed button').click()`);
  await W(`$$('.msg.is-highlighted').length === ${DATASET.records[0].transcript.length}`);
  const deep = base + '/#/conversations/ui-demo-ru?eval=' + encodeURIComponent(evalId) + '&msg=' + encodeURIComponent(evId);
  await page.goto('about:blank');
  await page.goto(deep);
  await W(`$$('.crit').length === 8 && $('.msg.is-highlighted')`);
  assert.deepEqual(await E(`return $$('.msg.is-highlighted').map((m) => m.dataset.msgId)`), [evId]);
  ok('evidence chip highlights + focuses message and writes ?msg=; missed span highlights range; deep link reload highlights');

  // ================= e. appeal + reviewer =================
  console.log('e. appeal');
  const totalBefore = evApi.final.result.total;
  await E(`byText('button', 'Апелляция', $('#crit-04')).click()`);
  await W(`$('#crit-04 textarea[aria-label="Комментарий к апелляции"]')`);
  await E(`byText('button', 'Подать апелляцию', $('#crit-04')).click()`);
  await W(`$('#crit-04 .field-error')?.textContent === 'Комментарий обязателен'`);
  await E(`setVal($('#crit-04 textarea'), 'ТЕСТ UI: консультация была частично полной, прошу пересмотреть'); byText('button', 'Подать апелляцию', $('#crit-04')).click()`);
  await W(`$('#crit-04 .appeal-pending a')`);
  assert.equal(await E(`return $('.total-final .score-big').textContent`), card.total);
  const appeals = await api('appeals?evaluation_id=' + encodeURIComponent(evalId));
  assert.equal(appeals.items.length, 1); assert.equal(appeals.items[0].status, 'pending'); assert.equal(appeals.items[0].criterion_id, '04');
  assert.equal((await api('evaluations/' + encodeURIComponent(evalId))).final.result.total, totalBefore);
  ok('appeal filed from card with required comment; score unchanged, pending badge shown');

  await E(`$('#crit-04 .appeal-pending a').click()`);
  await W(`$('.review-form')`);
  assert(await E(`return document.body.innerText.includes('ТЕСТ UI: консультация была частично полной')`));
  assert.equal(await E(`return $$('.appeal-compare .evidence-fragment').length`), 3,
    'disputed, original and final appeal answers each include the original supporting quote');
  await E(`$('input[value="change"]').click(); $('input[value="change"]').dispatchEvent(new Event('change', { bubbles: true }))`);
  await W(`!$('.change-editor').hidden`);
  const sel = 'select[aria-label="Значение"]';
  assert.deepEqual(await E(`return $$('${sel} option').map((o) => Number(o.value))`), [0, 5, 10, 15, 20]);
  assert.equal(await E(`return $('${sel}').tagName`), 'SELECT', 'discrete scale must not be a free number input');
  // Intermediate value cannot be chosen: the select rejects it, and submitting blocks client-side.
  assert.equal(await E(`const s = $('${sel}'); s.value = '12'; return s.value`), '');
  await E(`setVal($('.review-form > label.field textarea'), 'ТЕСТ UI: попытка промежуточного значения'); $('.review-form button[type=submit]').click()`);
  await W(`$('.review-form .alert-error')`);
  assert.match(await E(`return $('.review-form .alert-error').textContent`), /числовое значение|допустимую ступень/);
  assert.equal((await api('appeals/' + encodeURIComponent(appeals.items[0].id))).status, 'pending');
  // Reason is required.
  await E(`setVal($('.review-form > label.field textarea'), '   '); $('${sel}').value = '15'; $('.review-form button[type=submit]').click()`);
  await W(`$('.review-form .alert-error')?.textContent.includes('Причина решения обязательна')`);
  await E(`setVal($('.review-form > label.field textarea'), 'ТЕСТ UI: основной вопрос решён, пропущена деталь'); $('${sel}').value = '15'; $('.review-form button[type=submit]').click()`);
  await W(`document.body.innerText.includes('Решение сохранено')`);
  assert(await E(`return !$('.review-form') && document.body.innerText.includes('Повторное решение по этой апелляции невозможно')`));
  const after = await api('evaluations/' + encodeURIComponent(evalId));
  assert.deepEqual(after.original, evApi.original, 'original must be immutable');
  assert.equal(after.final.answers.find((a) => a.criterion_id === '04').answer, 15);
  assert.notEqual(after.final.result.total, evApi.original.result.total);
  const finalTxt = await E(`return $('.panel-final .total-line strong').textContent`);
  const origTxt = await E(`return $('.panel-original .total-line strong').textContent`);
  assert.match(finalTxt, /^\d{1,3}\.\d{2}$/); assert.match(origTxt, /^\d{1,3}\.\d{2}$/);
  assert.equal(Number(finalTxt), after.final.result.total); assert.equal(Number(origTxt), evApi.original.result.total);
  // Repeat review is refused by the server (UI no longer offers a form).
  const again = await api('appeals/' + encodeURIComponent(appeals.items[0].id) + '/review', { decision: 'confirm', reason: 'повтор' }, 409);
  assert.equal(again.error.code, 'already_reviewed');
  await page.goto(base + '/#/conversations/ui-demo-ru?eval=' + encodeURIComponent(evalId));
  await W(`$$('.crit').length === 8 && $('.total-original')`);
  assert.equal(await E(`return $('.total-final .score-big').textContent`), finalTxt);
  assert.equal(await E(`return $('.total-original .score-big').textContent`), origTxt);
  assert(await E(`return $('#crit-04').textContent.includes('изменено по апелляции') && $('#crit-04 .appeal-history') !== null`));
  await page.goto(base + '/#/appeals?status=reviewed');
  await W(`$$('.appeals-table tbody tr').length === 1`);
  assert(await E(`return $('.appeals-table tbody tr').textContent.includes('изменено')`));
  ok(`review: only steps 0/5/10/15/20, intermediate value blocked, reason required, 04→15; original ${origTxt} kept, final ${finalTxt}; repeat review 409`);

  // ================= official preset in editor vs PDF =================
  console.log('preset vs PDF');
  await page.goto(base + '/#/checklists/official-cqa-v1');
  await W(`$$('.cqa-crit').length === 8`);
  const ed = await E(`return $$('.cqa-crit').map((f) => ({
    id: $('[id$="-id"]', f).value, title: $('[id$="-title"]', f).value, weight: $('[id$="-weight"]', f).value,
    type: $('[id$="-type"]', f).value, min: $('[id$="-min"]', f)?.value, max: $('[id$="-max"]', f)?.value,
    steps: $('[id$="-allowed_values"]', f)?.value ?? null, allow_na: $('[id$="-allow_na"]', f).checked,
    gross: $('[id$="-gross_error"]', f).checked, desc: $('[id$="-description"]', f).value, na: $('[id$="-na_condition"]', f).value,
    labels: $$('ul.muted li', f).map((li) => li.textContent),
  }))`);
  const edMeta = await E(`return { badge: $('.cqa-head .badge').textContent, decimals: $('#cl-decimals').value, policy: $('#cl-policy').value, sum: $('.cqa-weights tfoot').textContent }`);
  assert.equal(edMeta.badge, 'официальный чек-лист');
  const mism = [];
  const check = (cond, msg) => { if (!cond) mism.push(msg); };
  check(edMeta.decimals === '2', 'decimals ' + edMeta.decimals);
  check(edMeta.policy === 'flag', 'gross policy ' + edMeta.policy);
  check(/Сумма\s*100/.test(edMeta.sum), 'weight sum ' + edMeta.sum);
  pdf.crits.forEach((c, i) => {
    const u = ed[i] || {};
    check(u.id === c.id, `${c.id}: id ${u.id}`);
    check(u.title === c.title, `${c.id}: title "${u.title}" vs "${c.title}"`);
    check(Number(u.weight) === c.weight, `${c.id}: weight ${u.weight} vs ${c.weight}`);
    check(u.allow_na === c.allow_na, `${c.id}: allow_na ${u.allow_na} vs ${c.allow_na}`);
    check((u.type === 'numeric') === c.numeric, `${c.id}: type ${u.type}`);
    if (c.numeric) check(Number(u.min) === c.range[0] && Number(u.max) === c.range[1], `${c.id}: range ${u.min}-${u.max} vs ${c.range}`);
    check(norm(u.desc).startsWith(norm(c.yes.replace(/ по шкале на стр\. 2/, '')).slice(0, 40)), `${c.id}: description "${u.desc}" vs PDF "${c.yes}"`);
    if (c.allow_na) check(norm(u.na) === norm(c.na), `${c.id}: N/A condition "${u.na}" vs PDF "${c.na}"`);
    else check(!u.na, `${c.id}: unexpected N/A condition "${u.na}"`);
    check(u.gross === (c.id === '08'), `${c.id}: gross flag ${u.gross}`);
    const sc = pdf.scales[c.id];
    if (sc) {
      const steps = Object.keys(sc).map(Number).sort((a, b) => a - b);
      check(u.steps === steps.join('; '), `${c.id}: steps "${u.steps}" vs ${steps.join('; ')}`);
      for (const [v, label] of Object.entries(sc)) check(u.labels.includes(`${v}: ${label}`), `${c.id}: scale label ${v} "${label}" not shown`);
    }
  });
  check(ed.length === 8, 'criteria count ' + ed.length);
  assert.deepEqual(mism, [], 'official preset UI vs PDF mismatches:\n' + mism.join('\n'));
  ok('editor shows official preset identical to PDF: titles, weights 10/15/20/20/15/15/5/0 (sum 100), N/A only 02/03/04/07 with PDF conditions, 04 steps 0;5;10;15;20 and 06 0;4;7;10 with labels, 08 gross flag, decimals 2, flag policy');

  // ================= f. new checklist version with a new custom criterion =================
  console.log('f. new checklist version');
  await E(`byText('button', '+ Добавить критерий').click()`);
  await W(`$$('.cqa-crit').length === 9`);
  await E(`const row = $$('.cqa-crit').at(-1);
    setVal($('[id$="-id"]', row), 'ui-check-new');
    setVal($('[id$="-title"]', row), 'Оператор назвал срок решения (UI-проверка)');
    setVal($('[id$="-description"]', row), 'Да — оператор назвал ожидаемый срок. ТЕСТОВЫЙ критерий UI-проверки.');
    setVal($('[id$="-weight"]', row), '7');
    setVal($('[id$="-section"]', row), 'Пользовательский раздел');`);
  await W(`$('.cqa-diff')?.textContent.includes('ui-check-new')`);
  await E(`byText('button', 'Сохранить как новую версию').click()`);
  await W(`!location.hash.endsWith('/official-cqa-v1') && $$('.cqa-crit').length === 9`);
  const newId = decodeURIComponent(await E(`return location.hash.split('/').at(-1)`));
  const saved = await api('checklists/' + encodeURIComponent(newId));
  assert.equal(saved.provenance, 'custom'); assert.equal(saved.family_id, 'official-cqa'); assert(saved.version >= 2);
  assert.equal(saved.criteria.at(-1).id, 'ui-check-new');
  assert.equal(await E(`return $('.cqa-head .badge').textContent`), 'пользовательский');
  assert(await E(`return $('.cqa-versions').textContent.includes('v1') && $('.cqa-versions').textContent.includes('v${saved.version}')`));

  await page.goto(base + '/#/conversations/ui-demo-en');
  await W(`$('.evaluate-box select[aria-label="Версия чек-листа"]') && $$('.crit').length === 8`);
  await E(`setVal($('.evaluate-box select'), ${JSON.stringify(newId)}, 'change'); byText('button', 'Оценить', $('.evaluate-box')).click()`);
  await W(`$$('.crit').length === 9 && $('#crit-ui-check-new')`, 30000);
  const custom = await E(`return { title: $('#crit-ui-check-new .crit-title').textContent, group: $('#crit-ui-check-new').closest('.crit-group').querySelector('h4').textContent, badge: $('.eval-meta .badge').textContent, weight: $$('dt', $('#crit-ui-check-new')).find((d) => d.textContent.startsWith('Вес')).nextElementSibling.textContent }`);
  assert.equal(custom.title, 'Оператор назвал срок решения (UI-проверка)');
  assert.equal(custom.group, 'Пользовательский раздел');
  assert.equal(custom.badge, 'пользовательский');
  assert.match(custom.weight, /^7 → /);
  const official = await api('checklists/official-cqa-v1');
  assert.equal(official.version, 1); assert.equal(official.provenance, 'official'); assert.equal(official.criteria.length, 8);
  assert(!official.criteria.some((c) => c.id === 'ui-check-new'));
  await page.goto(base + '/#/checklists/official-cqa-v1');
  await W(`$$('.cqa-crit').length === 8`);
  assert.equal(await E(`return $('.cqa-head .badge').textContent`), 'официальный чек-лист');
  ok(`new version v${saved.version} (custom, family official-cqa) with criterion ui-check-new created in editor, evaluated via card (9 criteria, own section/weight); official-cqa-v1 still v1, 8 criteria, official`);

  // ================= g. browser errors =================
  console.log('g. browser errors');
  await sleep(300);
  assert.equal(page.errors.length, 0, 'unexpected browser errors:\n' + page.errors.join('\n'));
  assert.equal(expectedErrors.length <= 1, true, 'expected-error window captured more than the single 422: ' + expectedErrors.join('\n'));
  ok(`0 unexpected console errors/exceptions (expected and excluded: ${expectedErrors.length} × "status of 422" resource error from the deliberate invalid import)`);

  console.log(`\nPASS: ${passed.length} checks. TEST PROVIDER ONLY (synthetic labelled data, separate fixture DB).`);
  if (findings.length) console.log('\nFINDINGS (outside UI-check write scope, not failing):\n- ' + findings.join('\n- '));
} catch (e) {
  try { await page.screenshot(join(ROOT, '.local', 'ui-check-failure.png')); } catch { /* ignore */ }
  console.error('\nFAIL after ' + passed.length + ' checks: ' + (e.stack || e));
  if (page.errors.length) console.error('browser errors:\n' + page.errors.join('\n'));
  process.exitCode = 1;
} finally {
  await page.close();
  server.kill();
}
