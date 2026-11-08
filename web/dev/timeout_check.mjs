// Browser regression for timeout UI integration. Synthetic in-memory data only;
// static server, no app DB, no inference and no provider calls.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { launch, sleep } from './cdp.mjs';
const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const port = 8049;
const base = `http://127.0.0.1:${port}`;
const server = spawn(process.env.CQA_PYTHON || 'python', ['-m', 'http.server', String(port), '--bind', '127.0.0.1'], { cwd: root, stdio: 'ignore' });
let page;
try {
  let ready = false;
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(base + '/web/dev/')).ok) { ready = true; break; } } catch {}
    await sleep(100);
  }
  assert(ready, 'static test server available');
  page = await launch({ profileDir: join(root, '.local', 'timeout-ui-profile'), port: 9349 });
  await page.goto(base + '/web/dev/');
  const timeout = await page.eval(`(async () => {
    const {api} = await import('/web/app/api.js');
    const realFetch = window.fetch, realTimer = window.setTimeout;
    let duration;
    window.setTimeout = (fn, ms) => { duration = ms; return realTimer(fn, ms); };
    window.fetch = async () => { throw new DOMException('TEST ONLY', 'AbortError'); };
    try { await api.evaluate('synthetic-test', 'synthetic-checklist'); }
    catch (e) { return { duration, code: e.code, message: e.message }; }
    finally { window.fetch = realFetch; window.setTimeout = realTimer; }
  })()`);
  assert.equal(timeout.duration, 660000);
  assert.equal(timeout.code, 'timeout');
  console.log('PASS evaluate allows 660s; aborted request produces visible timeout error');
  const history = await page.eval(`(async () => {
    const {renderEvaluationPanel} = await import('/web/results/evaluation-panel.js');
    const cl = {id:'test', name:'Synthetic test', version:1, provenance:'custom', criteria:[]};
    const result = {total:90, criteria:[], sections:[], applicable_weight:100, rules:{decimals:2,gross_error_policy:'flag'}};
    const legacy = {id:'legacy', checklist_id:'test', created_at:'2026-11-07T12:00:00Z', original:{answers:[],result}, final:{answers:[],result}};
    const current = {...legacy,id:'current',provider:{type:'ollama',model:'synthetic-model',prompt_revision:'test'}};
    const ctx = {conversation:{conversation_id:'synthetic'},evaluations:[current,legacy],selected:legacy,
      checklists:new Map([['test',cl]]),allChecklists:[cl],appeals:[],transcript:{},onSelect(){},onReload(){}};
    window.timeoutCtx=ctx;
    const panel=renderEvaluationPanel(ctx); document.body.append(panel);
    const warning=panel.querySelector('.eval-status .badge-warn')?.textContent;
    const note=panel.querySelector('.eval-note').textContent;
    const selector=[...panel.querySelector('select[aria-label="Оценка"]').options].map(o=>o.textContent);
    const automatic=renderEvaluationPanel({...ctx,selected:current});
    return {warning,note,selector,automaticWarning:!!automatic.querySelector('.eval-status .badge-warn')};
  })()`);
  assert.match(history.warning, /архивная.*модель не подтверждена/);
  assert.match(history.note, /история сохранится/);
  assert.match(history.selector[1], /архивная, модель не подтверждена/);
  assert.equal(history.automaticWarning, false);
  console.log('PASS metadata-less history visibly marked; real metadata not mislabelled; history remains selectable');
  const retry=await page.eval(`(async () => {
    const {api,ApiError}=await import('/web/app/api.js');
    const {renderEvaluationPanel}=await import('/web/results/evaluation-panel.js');
    const real=api.evaluate;
    api.evaluate=async()=>{const e=new ApiError(422,'validation_error','Модель не ответила вовремя. Повторите оценку; предыдущие результаты сохранены.');e.serverMessage=e.message;throw e;};
    try {
      const panel=renderEvaluationPanel(window.timeoutCtx); document.body.append(panel);
      const btn=panel.querySelector('.evaluate-box button'); btn.click();
      await new Promise(r=>setTimeout(r,0));
      return {enabled:!btn.disabled,label:btn.textContent,alert:panel.querySelector('[role="alert"]').textContent,hint:panel.querySelector('.evaluate-status').textContent};
    } finally {api.evaluate=real;}
  })()`);
  assert.equal(retry.enabled,true);
  assert.equal(retry.label,'Повторить оценку');
  assert.match(retry.alert,/не ответила вовремя/);
  assert.match(retry.hint,/Оценка не сохранена/);
  console.log('PASS server timeout visible, chosen checklist retained and retry button enabled');
  // Directory-listing harness has no favicon; keep all application errors fatal.
  const errors=page.errors.filter(e=>!e.includes(base+'/favicon.ico'));
  assert.equal(errors.length,0,errors.join('\n'));
  console.log('PASS timeout browser checks (no inference; synthetic in-memory UI only)');
} finally {
  if(page)await page.close();
  server.kill();
}
