// Isolated responsive release acceptance; synthetic data and injected provider only.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { launch, sleep } from './cdp.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
mkdirSync(join(root, '.local'), { recursive: true });
const runtime = mkdtempSync(join(root, '.local', 'release-responsive-'));
const port = Number(process.env.CQA_RELEASE_CHECK_PORT || 8057);
const base = `http://127.0.0.1:${port}`;
const source = `
import sys
from pathlib import Path
from server.app import Application, make_server
from server.storage import Store
from web.dev.fixture_server import fixture_provider
app=Application(Store(Path(sys.argv[1])/'fixture.sqlite3'),provider=fixture_provider)
make_server(app,port=int(sys.argv[2])).serve_forever()
`;
const fixture = spawn(process.env.CQA_PYTHON || 'python', ['-c', source, runtime, String(port)], { cwd: root, stdio: 'ignore' });
let page;
async function api(path, body) {
  const response = await fetch(base + '/api/' + path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const result = await response.json();
  assert.equal(response.status, 200, JSON.stringify(result));
  return result;
}
try {
  let ready = false;
  for (let i = 0; i < 50; i++) {
    try { await api('health'); ready = true; break; } catch { await sleep(100); }
  }
  assert(ready, 'isolated fixture did not start');
  await api('imports', { source: 'json', group_id: 'SYNTHETIC_RELEASE', payload: [{
    conversation_id: 'release-voice', language: 'RU', channel: 'voice call', audio_url: null,
    label: 'Synthetic responsive release fixture', transcript: [
      { timestamp: '2026-11-03T15:49:13.2744711+00:00', speaker: 'system', text: 'Synthetic system start.' },
      { timestamp: '2026-11-03T15:49:22.4171535+00:00', speaker: 'bot', text: 'Synthetic routing announcement.' },
      { timestamp: '2026-11-03T15:49:30.3241399+00:00', speaker: 'client', text: '<img src=x onerror="window.__releaseXss=1">' + 'long-unbroken-fragment-'.repeat(12) },
      { timestamp: '2026-11-03T15:49:36.5491731+00:00', speaker: 'operator', text: 'Здравствуйте! Это синтетический пример проверки адаптивного интерфейса.' },
    ],
  }] });
  const evaluation = await api('evaluations', { conversation_id: 'release-voice', checklist_id: 'official-cqa-v1' });
  const appeal = await api('appeals', { evaluation_id: evaluation.id, criterion_id: '01', comment: 'Synthetic responsive appeal.' });
  page = await launch({ profileDir: join(runtime, 'browser'), port: Number(process.env.CQA_RELEASE_CHECK_CDP || 9367) });
  const routes = [
    ['/conversations', "document.querySelector('h1')?.textContent.includes('Диалоги')"],
    ['/conversations/release-voice', "document.querySelector('.eval-panel .crit')"],
    ['/import', "document.querySelector('h1')?.textContent.includes('Импорт')"],
    ['/checklists', "document.querySelector('h1')?.textContent.includes('Чек-листы')"],
    ['/checklists/official-cqa-v1', "document.querySelector('.cqa-crit-list')"],
    ['/appeals', "document.querySelector('.appeals-body')"],
    ['/appeals/' + appeal.id, "document.querySelector('.review-form')"],
  ];
  const failures = [];
  for (const width of [320, 375, 768, 1440]) {
    await page.send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: width < 600 });
    for (const [route, readySelector] of routes) {
      await page.goto(base + '/#' + route);
      await page.waitFor(readySelector);
      const metrics = await page.eval(`({width:innerWidth,scroll:document.documentElement.scrollWidth,
        xss:Boolean(window.__releaseXss),
        offenders:[...document.querySelectorAll('#view *')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&(r.right>innerWidth+1||r.left< -1)}).slice(0,5).map(e=>e.className)})`);
      assert.equal(metrics.xss, false, 'transcript HTML must remain inert text');
      if (metrics.scroll > width + 1) failures.push({ width, route, ...metrics });
      if (width === 375 && route === '/conversations/release-voice') await page.screenshot(join(runtime, 'voice-mobile.png'));
    }
  }
  assert.equal(page.errors.length, 0, page.errors.join('\n'));
  console.log(JSON.stringify({ routes: routes.length, widths: 4, checks: 28, overflowFailures: failures, screenshots: runtime }));
  assert.deepEqual(failures, [], 'responsive routes must fit viewport without horizontal page overflow');
  console.log('PASS 28 responsive route/viewport checks; transcript HTML inert, 0 browser errors. TEST PROVIDER ONLY.');
} finally {
  if (page) await page.close();
  fixture.kill();
}
