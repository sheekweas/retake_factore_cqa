// Browser check of the recording-transcription ("По записи") view of the transcript panel.
// Synthetic labelled data only: a fixture server (ТЕСТОВЫЙ ПРОВАЙДЕР, no model/GPU) with its own DB under
// .local/voice-fixture; the recording transcription and a tiny generated WAV are injected in the browser
// through the CDP Fetch domain, so the server itself is not modified and makes no outbound requests.
//
//   node web/dev/voice_view_check.mjs     (Node >= 22, Chrome or Edge; CQA_CHROME / CQA_PYTHON overrides)
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { launch, sleep } from './cdp.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const PORT = Number(process.env.CQA_VOICE_CHECK_PORT || 8053);
const CDP_PORT = Number(process.env.CQA_VOICE_CHECK_CDP || 9363);
const base = `http://127.0.0.1:${PORT}`;
const GROUP = 'VOICE_VIEW_CHECK';
const LABEL = 'демонстрационный пример — синтетические данные проверки голосового вида, не реальные диалоги';
const VOICE = 'voice-demo-ru';
const PLAIN = 'voice-demo-plain';
const passed = [];
const ok = (msg) => { passed.push(msg); console.log('  ok  ' + msg); };

const DATASET = {
  dataset_group_id: GROUP, note: LABEL,
  records: [
    { conversation_id: VOICE, channel: 'voice call', language: 'RU', audio_url: 'https://recordings.invalid/voice-demo-ru', label: LABEL, transcript: [
      { timestamp: '2026-11-07T10:00:00Z', speaker: 'system', text: 'Синтетический звонок начат.' },
      { timestamp: '2026-11-07T10:00:03Z', speaker: 'operator', text: 'Здравствуйте, меня зовут Ирина. Чем могу помочь?' },
      { timestamp: '2026-11-07T10:00:07Z', speaker: 'client', text: 'Хочу сменить тариф.' },
      { timestamp: '2026-11-07T10:00:11Z', speaker: 'operator', text: 'Откройте Настройки, затем Тарифы. Хорошего дня!' },
    ] },
    { conversation_id: PLAIN, channel: 'voice call', language: 'RU', audio_url: null, label: LABEL, transcript: [
      { timestamp: '2026-11-07T11:00:00Z', speaker: 'client', text: 'Синтетический звонок без расшифровки записи.' },
      { timestamp: '2026-11-07T11:00:05Z', speaker: 'operator', text: 'Здравствуйте, слушаю вас.' },
    ] },
  ],
};

const RECORDING = {
  status: 'ready', model: 'faster-whisper/large-v3-turbo', language: 'ru', created_at: '2026-11-07T12:30:00Z',
  reviewed: false, source: 'recording', conversation_id: VOICE, audio_duration_seconds: 12,
  segments: [
    { id: 'asr-0', start: 0.0, end: 2.1, text: 'Здравствуйте, меня зовут Ирина.' },
    { id: 'asr-1', start: 4.5, end: 6.9, text: 'Хочу сменить тариф <b>x</b>' },
    { id: 'asr-2', start: 8.2, end: 11.5, text: 'Откройте настройки, затем тарифы.' },
    { id: 'bad', start: 'NaN', end: 1, text: 'invalid segment must be ignored' },
    { id: 'bad2', start: 1, end: 2, text: null },
  ],
};

/** 12 s, 8 kHz, 16-bit mono WAV with a quiet tone (synthetic, generated here). */
function makeWav(seconds = 12, rate = 8000) {
  const n = seconds * rate, buf = Buffer.alloc(44 + n * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write('WAVE', 8); buf.write('fmt ', 12);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(800 * Math.sin(2 * Math.PI * 440 * i / rate)), 44 + i * 2);
  return buf;
}
const WAV = makeWav();
const WAV_URL = base + '/__voice_test/tone.wav';

// Own runtime dir so concurrent ui_check runs (which use .local/ui-fixture with --fresh) are unaffected.
const PY_SERVER = `
import json, shutil, sys
from pathlib import Path
root = Path(sys.argv[1]); port = int(sys.argv[2])
sys.path.insert(0, str(root)); sys.path.insert(0, str(root / 'web' / 'dev'))
from fixture_server import fixture_provider
from server.app import Application, make_server
from server.storage import Store
runtime = root / '.local' / 'voice-fixture'
shutil.rmtree(runtime, ignore_errors=True); runtime.mkdir(parents=True, exist_ok=True)
app = Application(Store(runtime / 'fixture.sqlite3'), provider=fixture_provider)
server = make_server(app, port=port)
print('VOICE FIXTURE (ТЕСТОВЫЙ ПРОВАЙДЕР)', port, flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
`;

async function startFixture() {
  const proc = spawn(process.env.CQA_PYTHON || 'python', ['-c', PY_SERVER, ROOT, String(PORT)], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
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

/** Second CDP client on the same page: handles Fetch.requestPaused (cdp.mjs exposes no event hook). */
async function interceptor() {
  const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
  const ws = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map(); const stats = { conversation: 0, audio: 0, wav: 0 };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject }); ws.send(JSON.stringify({ id: mid, method, params }));
  });
  const json = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64');
  const jsonHeaders = [{ name: 'Content-Type', value: 'application/json; charset=utf-8' }];
  ws.onmessage = async (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const p = pending.get(msg.id); pending.delete(msg.id);
      msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result);
      return;
    }
    if (msg.method !== 'Fetch.requestPaused') return;
    const { requestId, request, responseStatusCode } = msg.params;
    const path = new URL(request.url).pathname;
    try {
      if (path.endsWith('/audio')) {
        stats.audio++;
        const res = path.includes(VOICE)
          ? { status: 'playable', media_url: WAV_URL, media_type: 'audio/wav', page_url: 'https://recordings.invalid/voice-demo-ru', message: 'тест' }
          : { status: 'page_only', media_url: null, media_type: null, page_url: null, message: 'тест' };
        await send('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: jsonHeaders, body: json(res) });
      } else if (path.startsWith('/__voice_test/')) {
        stats.wav++;
        const range = /bytes=(\d+)-(\d*)/.exec(request.headers.Range || request.headers.range || '');
        const start = range ? Number(range[1]) : 0;
        const end = range && range[2] ? Math.min(Number(range[2]), WAV.length - 1) : WAV.length - 1;
        const slice = WAV.subarray(start, end + 1);
        const headers = [{ name: 'Content-Type', value: 'audio/wav' }, { name: 'Accept-Ranges', value: 'bytes' },
          { name: 'Content-Length', value: String(slice.length) }];
        if (range) headers.push({ name: 'Content-Range', value: `bytes ${start}-${end}/${WAV.length}` });
        await send('Fetch.fulfillRequest', { requestId, responseCode: range ? 206 : 200, responseHeaders: headers, body: slice.toString('base64') });
      } else if (path === '/api/conversations/' + VOICE && responseStatusCode === 200) {
        stats.conversation++;
        const { body, base64Encoded } = await send('Fetch.getResponseBody', { requestId });
        const data = JSON.parse(base64Encoded ? Buffer.from(body, 'base64').toString('utf8') : body);
        data.recording_transcription = RECORDING;
        await send('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: jsonHeaders, body: json(data) });
      } else {
        await send('Fetch.continueRequest', { requestId });
      }
    } catch (e) { console.error('interceptor: ' + e.message); try { await send('Fetch.continueRequest', { requestId }); } catch { /* gone */ } }
  };
  await send('Fetch.enable', { patterns: [
    { urlPattern: '*/api/conversations/*/audio', requestStage: 'Request' },
    { urlPattern: '*/__voice_test/*', requestStage: 'Request' },
    { urlPattern: `*/api/conversations/${VOICE}`, requestStage: 'Response' },
  ] });
  return { stats, close: () => ws.close() };
}

const H = `const $ = (s, r = document) => r.querySelector(s); const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const activeTab = () => $('.transcript-views [role=tab][aria-selected=true]')?.textContent;`;

let server, page, icpt;
try {
  server = await startFixture();
  page = await launch({ profileDir: join(ROOT, '.local', 'voice-check-browser'), port: CDP_PORT });
  icpt = await interceptor();
  const E = (js) => page.eval(`(() => { ${H}\n${js} })()`);
  const W = (js, t) => page.waitFor(`(() => { ${H}\nreturn ${js}; })()`, t);

  const imp = await api('imports', { source: 'json', payload: DATASET, group_id: GROUP });
  assert(JSON.stringify(imp).includes(VOICE), 'import result: ' + JSON.stringify(imp));
  const evaluation = await api('evaluations', { conversation_id: VOICE, checklist_id: 'official-cqa-v1' });
  const evalId = evaluation.id;
  assert(evalId, 'evaluation id');
  ok('synthetic voice conversations imported; fixture evaluation (ТЕСТОВЫЙ ПРОВАЙДЕР) created');

  // ---- default view: По записи ----
  await page.goto(base + '/#/conversations/' + VOICE + '?eval=' + encodeURIComponent(evalId));
  await W(`$('.transcript-views') && $$('.crit').length === 8 && $('.recording-panel audio')`, 15000);
  assert.equal(icpt.stats.conversation >= 1, true, 'conversation response intercepted');
  const v = await E(`return {
    role: $('.transcript-views').getAttribute('role'),
    tabs: $$('.transcript-views [role=tab]').map((b) => [b.textContent, b.getAttribute('aria-selected'), b.classList.contains('is-active')]),
    asrVisible: !$('.asr-list').closest('[role=tabpanel]').hidden, origHidden: $('.transcript-list').closest('[role=tabpanel]').hidden,
    badge: $('.badge.badge-asr')?.textContent, meta: $('.asr-meta')?.textContent, metaTitles: $$('.asr-meta [title]').map((e) => e.title),
    segs: $$('.asr-list li.asr-seg').map((li) => ({ id: li.id, seg: li.dataset.segId, time: $('.asr-seek', li).textContent, text: $('.asr-text', li).textContent })),
    htmlNodes: $$('.asr-list b, .asr-list script, .asr-list img').length,
    roles: $$('.asr-list .role, .asr-list [class*="role"], .asr-list .msg').length,
    roleWords: /Оператор|Клиент|Бот|Система/.test($('.asr-list').textContent),
    disclaimerHidden: $('.offset-disclaimer') ? $('.offset-disclaimer').hidden : null,
  }`);
  assert.equal(v.role, 'tablist');
  assert.deepEqual(v.tabs, [['По записи', 'true', true], ['Исходные реплики', 'false', false]]);
  assert.equal(v.asrVisible, true); assert.equal(v.origHidden, true);
  ok('toggle role=tablist with tabs "По записи" (default, aria-selected) and "Исходные реплики"');
  assert.equal(v.badge, 'Автоматическая расшифровка');
  assert.match(v.meta, /faster-whisper\/large-v3-turbo/); assert.match(v.meta, /не проверена человеком/);
  assert(v.metaTitles.includes('2026-11-07T12:30:00Z') && v.metaTitles.includes('faster-whisper/large-v3-turbo'), 'raw values in title: ' + v.metaTitles);
  ok('badge "Автоматическая расшифровка", meta: model, "не проверена человеком", created_at (raw in title): ' + v.meta);
  assert.deepEqual(v.segs, [
    { id: 'asr-asr-0', seg: 'asr-0', time: '00:00', text: 'Здравствуйте, меня зовут Ирина.' },
    { id: 'asr-asr-1', seg: 'asr-1', time: '00:04', text: 'Хочу сменить тариф <b>x</b>' },
    { id: 'asr-asr-2', seg: 'asr-2', time: '00:08', text: 'Откройте настройки, затем тарифы.' },
  ]);
  assert.equal(v.htmlNodes, 0); assert.equal(v.roles, 0); assert.equal(v.roleWords, false);
  assert.equal(v.disclaimerHidden, true, 'computed-offset disclaimer hidden in recording view');
  ok('3 valid segments rendered in order, 2 invalid ignored; "<b>x</b>" literal text, no HTML nodes; no speaker roles');

  // ---- seek on a segment: actual offset, no computed-offset disclaimer, is-playing ----
  await E(`$('.recording-panel audio').muted = true`);
  await W(`$('.recording-panel audio').readyState >= 1`, 10000);
  // Real input event (user gesture) so the browser allows play().
  await E(`$('#asr-asr-1 .asr-seek').scrollIntoView({ block: 'center' })`);
  await sleep(300);
  const pt = await E(`const r = $('#asr-asr-1 .asr-seek').getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }`);
  for (const type of ['mousePressed', 'mouseReleased']) await page.send('Input.dispatchMouseEvent', { type, x: pt.x, y: pt.y, button: 'left', clickCount: 1 });
  try { await W(`!$('.recording-panel audio').paused && $('.recording-panel audio').currentTime >= 4.4`, 10000); } catch (e) {
    console.error(await E(`const a = $('.recording-panel audio'); return JSON.stringify({ p: a.paused, t: a.currentTime, d: a.duration, rs: a.readyState, err: a.error && a.error.message, note: $('.audio-offset-note').textContent })`));
    throw e;
  }
  await W(`$('#asr-asr-1').classList.contains('is-playing')`, 5000);
  const seekState = await E(`const a = $('.recording-panel audio'); return { t: a.currentTime, note: $('.audio-offset-note').hidden ? '' : $('.audio-offset-note').textContent, playing: $$('.is-playing').map((e) => e.id) }`);
  assert(seekState.t >= 4.4 && seekState.t < 7, 'currentTime ' + seekState.t);
  assert.doesNotMatch(seekState.note, /не сверено/);
  assert.deepEqual(seekState.playing, ['asr-asr-1']);
  await E(`$('.recording-panel audio').pause()`);
  ok(`segment seek moved playback to ${seekState.t.toFixed(2)} s (segment start 4.5), current segment .is-playing, no "не сверено" note`);

  // ---- original view ----
  await E(`$$('.transcript-views [role=tab]')[1].click()`);
  const o = await E(`return { tab: activeTab(), msgs: $$('.transcript-list .msg').length, asrHidden: $('.asr-list').closest('[role=tabpanel]').hidden,
    legend: Boolean($('.transcript-legend')) && !$('.transcript-legend').closest('[role=tabpanel]').hidden,
    ts: $$('.transcript-list .msg-ts').map((e) => ({ text: e.textContent, title: e.title, src: e.dataset.sourceTs })),
    play: $$('.transcript-list .msg-play').length, disclaimer: $('.offset-disclaimer') && !$('.offset-disclaimer').hidden }`);
  assert.equal(o.tab, 'Исходные реплики'); assert.equal(o.msgs, 4); assert.equal(o.asrHidden, true); assert.equal(o.legend, true);
  assert.equal(o.play, 4); assert.equal(o.disclaimer, true);
  o.ts.forEach((t, i) => {
    const raw = DATASET.records[0].transcript[i].timestamp;
    assert.match(t.text, /^\d{2}:\d{2}:\d{2}$/); assert(!t.text.includes('T'));
    assert.equal(t.src, raw); assert(t.title.includes(raw), t.title);
  });
  ok('original tab shows 4 .msg with legend and ▶ offsets; .msg-ts compact (' + o.ts.map((t) => t.text).join(', ') + '), raw ISO in title and data-source-ts');

  // ---- evidence link from the ASR view switches back to the original view ----
  await E(`$$('.transcript-views [role=tab]')[0].click()`);
  assert.equal(await E(`return activeTab()`), 'По записи');
  const evId = await E(`return $('#crit-01 .chip-evidence').title.replace('Показать сообщение ', '')`);
  await E(`$('#crit-01 .chip-evidence').click()`);
  await W(`$('.msg.is-highlighted')`);
  const evState = await E(`return { tab: activeTab(), hl: $$('.msg.is-highlighted').map((m) => m.dataset.msgId), active: document.activeElement.dataset.msgId,
    visible: !$('.transcript-list').closest('[role=tabpanel]').hidden, hash: location.hash }`);
  assert.equal(evState.tab, 'Исходные реплики'); assert.equal(evState.visible, true);
  assert.deepEqual(evState.hl, [evId]); assert.equal(evState.active, evId);
  assert.equal(new URLSearchParams(evState.hash.split('?')[1]).get('msg'), evId);
  // checked-span link (criterion 03 is "missed" in the fixture provider) also switches view.
  await E(`$$('.transcript-views [role=tab]')[0].click()`);
  await E(`$('#crit-03 .ans-missed button').click()`);
  await W(`$$('.msg.is-highlighted').length === 4`);
  assert.equal(await E(`return activeTab()`), 'Исходные реплики');
  ok('evidence chip and checked-span link from "По записи" switch to original view, highlight + focus ' + evId + ', write ?msg=');

  // ---- ?msg= deep link lands on the original view ----
  await page.goto('about:blank');
  await page.goto(base + '/#/conversations/' + VOICE + '?eval=' + encodeURIComponent(evalId) + '&msg=' + encodeURIComponent(evId));
  await W(`$$('.crit').length === 8 && $('.msg.is-highlighted')`, 15000);
  await sleep(300);
  const deep = await E(`return { tab: activeTab(), hl: $$('.msg.is-highlighted').map((m) => m.dataset.msgId) }`);
  assert.equal(deep.tab, 'Исходные реплики'); assert.deepEqual(deep.hl, [evId]);
  ok('?msg= deep link opens original view with the message highlighted');

  // ---- screenshots of the voice view ----
  const shots = join(ROOT, '.local', 'shots'); mkdirSync(shots, { recursive: true });
  await page.goto('about:blank');
  for (const width of [390, 1440]) {
    await page.send('Emulation.setDeviceMetricsOverride', { width, height: width < 600 ? 1600 : 1000, deviceScaleFactor: 1, mobile: width < 600 });
    await page.goto(base + '/#/conversations/' + VOICE + '?eval=' + encodeURIComponent(evalId));
    await W(`$('.asr-list') && $$('.crit').length === 8`, 15000);
    await E(`$('.transcript-panel').scrollIntoView()`);
    await sleep(300);
    await page.screenshot(join(shots, `voice-view-${width}.png`));
  }
  ok('screenshots .local/shots/voice-view-390.png and voice-view-1440.png');

  // ---- 360 px: no horizontal overflow in either view ----
  await page.send('Emulation.setDeviceMetricsOverride', { width: 360, height: 800, deviceScaleFactor: 1, mobile: true });
  await page.goto(base + '/#/conversations/' + VOICE + '?eval=' + encodeURIComponent(evalId));
  await W(`$('.asr-list') && $$('.crit').length === 8`, 15000);
  await sleep(200);
  const ov1 = await E(`return [document.documentElement.scrollWidth, innerWidth]`);
  await E(`$$('.transcript-views [role=tab]')[1].click()`);
  await sleep(100);
  const ov2 = await E(`return [document.documentElement.scrollWidth, innerWidth]`);
  assert(ov1[0] <= ov1[1], 'recording view overflow ' + ov1); assert(ov2[0] <= ov2[1], 'original view overflow ' + ov2);
  await page.send('Emulation.clearDeviceMetricsOverride');
  ok(`360px: no horizontal overflow (recording ${ov1[0]}/${ov1[1]}, original ${ov2[0]}/${ov2[1]})`);

  // ---- conversation without recording transcription: unchanged panel ----
  await page.goto(base + '/#/conversations/' + PLAIN);
  await W(`$$('.transcript-list .msg').length === 2`, 15000);
  const plain = await E(`return { toggle: Boolean($('.transcript-views')), asr: Boolean($('.asr-list, .badge-asr')), legend: Boolean($('.transcript-legend')),
    ts: $('.msg-ts').textContent, src: $('.msg-ts').dataset.sourceTs }`);
  assert.deepEqual(plain, { toggle: false, asr: false, legend: true, ts: '11:00:00', src: '2026-11-07T11:00:00Z' });
  ok('conversation without recording_transcription: no toggle, no ASR view, original list + legend as before');

  await sleep(300);
  assert.equal(page.errors.length, 0, 'browser errors:\n' + page.errors.join('\n'));
  ok(`0 console errors/exceptions (intercepted: conversation ${icpt.stats.conversation}, audio ${icpt.stats.audio}, wav ${icpt.stats.wav})`);
  console.log(`\nPASS: ${passed.length} checks. Synthetic data only, separate fixture DB, no model.`);
} catch (e) {
  try { if (page) await page.screenshot(join(ROOT, '.local', 'voice-check-failure.png')); } catch { /* ignore */ }
  console.error('\nFAIL after ' + passed.length + ' checks: ' + (e.stack || e));
  if (page && page.errors.length) console.error('browser errors:\n' + page.errors.join('\n'));
  process.exitCode = 1;
} finally {
  try { icpt?.close(); } catch { /* ignore */ }
  if (page) await page.close();
  if (server) server.kill();
}
