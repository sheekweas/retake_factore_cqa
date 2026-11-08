// Browser regression for the official results export UI (#/import, card 3).
// Isolated fixture: an in-process Node mock HTTP server serves web/ statically and mocks /api/*.
// No app DB, no backend, no model/GPU, no private data. All conversation IDs are synthetic test values;
// the mock documents are shape fixtures, not grading ground truth.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join, normalize, extname, sep } from 'node:path';
import { launch } from './cdp.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const webRoot = join(root, 'web');
const port = Number(process.env.CQA_EXPORT_CHECK_PORT || 8053);
const base = `http://127.0.0.1:${port}`;
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };
const CRITERIA = ['contact', 'discovery', 'identification', 'consultation', 'empathy', 'dialogue', 'closure', 'critical'];

function syntheticDoc(dataset, team, n, withError) {
  const results = [];
  for (let i = 1; i <= n; i++) {
    const id = `synthetic-${dataset}-${String(i).padStart(3, '0')}`;
    if (withError && i === n) {
      results.push({ conversation_id: id, status: 'error', criteria: [], score: null, error: 'TEST ONLY: synthetic model failure' });
      continue;
    }
    results.push({
      conversation_id: id, status: 'ok',
      criteria: CRITERIA.map((c) => ({ criterion_id: c, value: c === 'critical' ? false : null, rationale: 'TEST ONLY', evidence_turns: [1] })),
      score: { final_score: 50, critical_flag: false },
    });
  }
  return { schema_version: '1.0', team_name: team, model: { mode: 'local', name: 'synthetic-test-model' }, results };
}

const exportRequests = [];
function sendJson(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}
const server = createServer(async (req, res) => {
  const url = new URL(req.url, base);
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname === '/api/health') return sendJson(res, 200, { status: 'ok', model_configured: false });
    if (url.pathname === '/api/checklists' || url.pathname === '/api/conversations') return sendJson(res, 200, { items: [] });
    if (url.pathname === '/api/results/export') {
      const p = Object.fromEntries(url.searchParams);
      exportRequests.push(p);
      if (!['dev', 'prod'].includes(p.dataset)) return sendJson(res, 422, { error: { code: 'validation_error', message: 'TEST ONLY: dataset must be dev or prod' } });
      if (!p.group_id) return sendJson(res, 422, { error: { code: 'validation_error', message: 'TEST ONLY: group_id is required' } });
      if (p.group_id === 'MISSING-EVALS') return sendJson(res, 422, { error: { code: 'validation_error', message: 'TEST ONLY: 3 conversations have no saved evaluation' } });
      if (p.group_id === 'BAD-SHAPE') return sendJson(res, 200, { results: 'not-an-array' });
      if (p.dataset === 'prod') return sendJson(res, 200, syntheticDoc('prod', p.team_name, p.group_id === 'SHORT' ? 9 : 10, true));
      return sendJson(res, 200, syntheticDoc('dev', p.team_name, 65, true));
    }
    return sendJson(res, 404, { error: { code: 'not_found', message: 'TEST ONLY: not mocked' } });
  }
  const rel = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname).replace(/^\/+/, '');
  const file = normalize(join(webRoot, rel));
  if (!file.startsWith(webRoot + sep)) { res.writeHead(403); return res.end(); }
  try {
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': TYPES[extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise((r) => server.listen(port, '127.0.0.1', r));

let page;
let passed = 0;
const pass = (msg) => { passed++; console.log('PASS ' + msg); };
try {
  page = await launch({ profileDir: join(root, '.local', 'results-export-profile'), port: 9353 });
  await page.send('Network.enable');
  await page.send('Network.setCacheDisabled', { cacheDisabled: true });
  await page.goto(base + '/#/import');
  await page.waitFor(`document.querySelector('#imp-exp-download')`);

  // Capture downloads instead of writing files: record the Blob and the anchor's download attribute.
  await page.eval(`(() => {
    window.__dl = [];
    window.__blobs = new Map();
    const realCreate = URL.createObjectURL.bind(URL);
    URL.createObjectURL = (b) => { const u = realCreate(b); window.__blobs.set(u, b); return u; };
    HTMLAnchorElement.prototype.click = function () { window.__dl.push({ href: this.href, download: this.getAttribute('download') }); };
    window.__set = (sel, v) => { const el = document.querySelector(sel); el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); };
    window.__out = () => document.querySelector('#imp-exp-out').textContent;
    return true;
  })()`);

  const defaults = await page.eval(`({ dataset: document.querySelector('#imp-exp-dataset').value,
    group: document.querySelector('#imp-exp-group').value, team: document.querySelector('#imp-exp-team').value,
    button: document.querySelector('#imp-exp-download').textContent.trim(),
    live: document.querySelector('#imp-exp-out').getAttribute('aria-live'),
    title: document.querySelector('#imp-exp-h')?.textContent || '' })`);
  assert.equal(defaults.dataset, 'dev');
  assert.equal(defaults.group, 'DEV');
  assert.equal(defaults.team, 'retake factory');
  assert.match(defaults.button, /results\.json/);
  assert.equal(defaults.live, 'polite');
  pass('defaults: dataset dev, group_id DEV, team_name "retake factory", download button, live region');

  // prod requires an explicit custom group_id: switching clears the DEV default; empty submit is blocked client-side.
  await page.eval(`window.__set('#imp-exp-dataset', 'prod')`);
  assert.equal(await page.eval(`document.querySelector('#imp-exp-group').value`), '');
  await page.eval(`document.querySelector('#imp-exp-download').click()`);
  await page.waitFor(`document.querySelector('#imp-exp-out .alert, #imp-exp-out [role="alert"]')`);
  assert.match(await page.eval(`window.__out()`), /group_id/);
  assert.equal(exportRequests.length, 0, 'no request without group_id');
  assert.equal(await page.eval(`window.__dl.length`), 0);
  pass('prod: DEV default cleared, missing group_id shows error, no request, no download');

  await page.eval(`window.__set('#imp-exp-group', 'CTRL-1'); window.__set('#imp-exp-team', '   ')`);
  await page.eval(`document.querySelector('#imp-exp-download').click()`);
  await page.waitFor(`/команд/i.test(window.__out())`);
  assert.equal(exportRequests.length, 0, 'no request without team_name');
  pass('empty team_name shows error, no request');

  // Server 422 is shown verbatim; nothing is downloaded or substituted.
  await page.eval(`window.__set('#imp-exp-team', 'retake factory'); window.__set('#imp-exp-group', 'MISSING-EVALS')`);
  await page.eval(`document.querySelector('#imp-exp-download').click()`);
  await page.waitFor(`/no saved evaluation/.test(window.__out())`);
  assert.equal(exportRequests.length, 1);
  assert.deepEqual(exportRequests[0], { dataset: 'prod', group_id: 'MISSING-EVALS', team_name: 'retake factory', reviewed: '0' });
  assert.equal(await page.eval(`window.__dl.length`), 0);
  assert.equal(await page.eval(`document.querySelector('#imp-exp-download').disabled`), false);
  pass('422 validation: server message shown, exact query sent, no download, button re-enabled');

  await page.eval(`window.__set('#imp-exp-group', 'BAD-SHAPE')`);
  await page.eval(`document.querySelector('#imp-exp-download').click()`);
  await page.waitFor(`/JSON 1\\.0/.test(window.__out())`);
  assert.equal(await page.eval(`window.__dl.length`), 0);
  pass('non-1.0 document rejected, no download');

  // Valid prod (10 records incl. one error) downloads byte-identical server JSON.
  await page.eval(`window.__set('#imp-exp-group', 'CTRL-1')`);
  await page.eval(`document.querySelector('#imp-exp-download').click()`);
  await page.waitFor(`window.__dl.length === 1`);
  const prod = await page.eval(`(async () => { const d = window.__dl[0]; const b = window.__blobs.get(d.href);
    return { download: d.download, type: b.type, text: await b.text(), out: window.__out(),
      warn: !!document.querySelector('#imp-exp-out .alert-warn') }; })()`);
  assert.equal(prod.download, 'results.json');
  assert.match(prod.type, /application\/json/);
  const prodDoc = JSON.parse(prod.text);
  assert.deepEqual(prodDoc, syntheticDoc('prod', 'retake factory', 10, true));
  assert.equal(prodDoc.results.filter((r) => r.status === 'error').length, 1);
  assert.match(prod.out, /results\/prod\/results\.json/);
  assert.match(prod.out, /10/);
  assert.equal(prod.warn, false);
  pass('prod download: results.json, JSON unchanged from server (error record kept, not zeroed), summary, no warning at 10');

  await page.eval(`window.__set('#imp-exp-group', 'SHORT')`);
  await page.eval(`document.querySelector('#imp-exp-download').click()`);
  await page.waitFor(`/ровно 10/.test(window.__out())`);
  assert.equal(await page.eval(`window.__dl.length`), 1, 'malformed count must not be downloaded');
  pass('prod with 9 records is rejected without downloading an invalid submission');

  // Back to dev: DEV default restored; 65-record document downloads with exact query.
  await page.eval(`window.__set('#imp-exp-group', ''); window.__set('#imp-exp-dataset', 'dev')`);
  assert.equal(await page.eval(`document.querySelector('#imp-exp-group').value`), 'DEV');
  await page.eval(`document.querySelector('#imp-exp-download').click()`);
  await page.waitFor(`window.__dl.length === 2`);
  assert.deepEqual(exportRequests.at(-1), { dataset: 'dev', group_id: 'DEV', team_name: 'retake factory', reviewed: '0' });
  const dev = await page.eval(`(async () => { const d = window.__dl[1]; return { text: await window.__blobs.get(d.href).text(), out: window.__out() }; })()`);
  const devDoc = JSON.parse(dev.text);
  assert.equal(devDoc.results.length, 65);
  assert.equal(devDoc.team_name, 'retake factory');
  assert.match(dev.out, /results\/dev\/results\.json/);
  assert.match(dev.out, /65/);
  pass('dev download: DEV restored, exact query, 65 records, results/dev path in summary');

  await page.send('Emulation.setDeviceMetricsOverride', { width: 360, height: 800, deviceScaleFactor: 1, mobile: true });
  await page.goto(base + '/#/import');
  await page.waitFor(`document.querySelector('#imp-exp-download')`);
  const overflow = await page.eval(`document.documentElement.scrollWidth - document.documentElement.clientWidth`);
  assert.ok(overflow <= 1, 'no horizontal overflow at 360px, got ' + overflow);
  pass('360px: no horizontal overflow');

  // 422 responses are logged by Chrome as failed resources; those are expected. Anything else fails.
  const errors = page.errors.filter((e) => !/Failed to load resource: the server responded with a status of 422/.test(e));
  assert.deepEqual(errors, []);
  pass('no unexpected console errors');
  console.log(`results_export_check: ${passed} PASS`);
} finally {
  if (page) await page.close();
  server.close();
}
