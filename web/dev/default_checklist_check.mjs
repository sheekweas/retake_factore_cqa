// Isolated defaults regression: synthetic data, separate fixture DB, no model/network credentials.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launch, sleep } from './cdp.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
mkdirSync(join(ROOT, '.local'), { recursive: true });
const runtime = mkdtempSync(join(ROOT, '.local', 'default-checklist-'));
const port = Number(process.env.CQA_DEFAULT_CHECKLIST_PORT || 8051);
const base = `http://127.0.0.1:${port}`;
const code = `
import sys
from pathlib import Path
from server.app import Application, make_server
from server.storage import Store
from server.presets import official_checklist
from web.dev.fixture_server import fixture_provider
app = Application(Store(Path(sys.argv[1]) / 'fixture.sqlite3'), provider=fixture_provider)
app.dispatch('POST', '/api/imports', {'source':'json','payload':[{
 'conversation_id':'synthetic-ungraded-voice','language':'RU','channel':'voice call',
 'audio_url':None,'label':'Synthetic defaults regression only',
 'transcript':[{'timestamp':'00:00','speaker':'client','text':'Synthetic request.'},
 {'timestamp':'00:01','speaker':'operator','text':'Synthetic answer.'}]}]})
custom=official_checklist()
custom.update(name='Synthetic newer custom rubric',provenance='custom')
app.dispatch('POST','/api/checklists',custom)
make_server(app,port=int(sys.argv[2])).serve_forever()
`;
const fixture = spawn(process.env.CQA_PYTHON || 'python', ['-c', code, runtime, String(port)], { cwd: ROOT, stdio: 'ignore' });
const api = async (route, body) => {
  const response = await fetch(base + '/api/' + route, body ? { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) } : {});
  const data = await response.json();
  assert.equal(response.status, 200, 'Synthetic fixture API request');
  return data;
};
let page;
try {
  let ready = false;
  for (let i=0; i<80; i++) {
    try { if ((await fetch(base+'/api/health')).ok) { ready=true; break; } } catch {}
    await sleep(100);
  }
  assert.ok(ready, 'Synthetic fixture server ready');
  const checklists = (await api('checklists')).items;
  const custom = checklists.find(c=>c.provenance==='custom');
  assert.ok(custom, 'Newer custom checklist exists alongside official');
  page = await launch({profileDir:join(runtime,'browser'), port:9351});
  await page.goto(base+'/#/conversations/synthetic-ungraded-voice');
  await page.waitFor("document.querySelector('.evaluate-box select')");
  assert.equal(await page.eval("document.querySelector('.evaluate-box select').value"), 'official-cqa-v1');
  console.log('PASS ungraded voice card defaults to official despite newer custom rubric');

  await page.goto(base+'/#/import');
  await page.waitFor("document.querySelector('.cqa-batch select[aria-label=\"Версия чек-листа\"]')");
  assert.equal(await page.eval("document.querySelector('.cqa-batch select[aria-label=\"Версия чек-листа\"]').value"), 'official-cqa-v1');
  console.log('PASS batch defaults to official rubric');
  await page.eval(`(() => {
    const selector=document.querySelector('.cqa-batch select[aria-label="Версия чек-листа"]');
    selector.value=${JSON.stringify(custom.id)};
    selector.dispatchEvent(new Event('change',{bubbles:true}));
    document.querySelector('#imp-tab-json').click();
    const area=document.querySelector('textarea[aria-label="JSON набора диалогов"]');
    area.value=JSON.stringify([{conversation_id:'synthetic-import-reload',language:'EN',channel:'agent chat',audio_url:null,
      transcript:[{timestamp:'00:00',speaker:'operator',text:'Synthetic imported answer.'}]}]);
    [...document.querySelectorAll('[aria-labelledby="imp-json-h"] button')].find(b=>b.textContent.includes('Импортировать JSON')).click();
  })()`);
  await page.waitFor("document.querySelector('.cqa-batch').textContent.includes('synthetic-import-reload')");
  assert.equal(await page.eval("document.querySelector('.cqa-batch select[aria-label=\"Версия чек-листа\"]').value"), custom.id);
  console.log('PASS explicit custom batch choice survives import/reload');

  const evaluation=await api('evaluations',{conversation_id:'synthetic-ungraded-voice',checklist_id:custom.id});
  await page.goto(base+'/#/conversations/synthetic-ungraded-voice?eval='+evaluation.id);
  await page.waitFor("document.querySelector('.evaluate-box select') && document.querySelector('.eval-note')");
  assert.equal(await page.eval("document.querySelector('.evaluate-box select').value"),custom.id);
  console.log('PASS selected historical custom evaluation preserves its checklist');
  assert.equal(page.errors.length,0,page.errors.join('\n'));
  console.log('PASS default checklist regression; 0 browser errors. TEST PROVIDER ONLY.');
} finally {
  if(page)await page.close();
  fixture.kill();
}
