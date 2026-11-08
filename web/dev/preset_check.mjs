// Browser regression for the official preset. ONLY use the labelled synthetic fixture server.
// Start python web/dev/fixture_server.py --port 8033, then node web/dev/preset_check.mjs.
import assert from 'node:assert/strict';
import { launch } from './cdp.mjs';
const base = process.env.CQA_FIXTURE_BASE || 'http://127.0.0.1:8033';
const req = async (path, body) => {
  const r = await fetch(base + '/api/' + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  const data = await r.json();
  assert.equal(r.status, 200, JSON.stringify(data)); return data;
};
const page = await launch({ profileDir: '.local/preset-browser', port: 9343 });
try {
  await page.goto(base + '/#/checklists/official-cqa-v1');
  await page.waitFor("document.querySelectorAll('.cqa-crit').length === 8");
  assert.equal(await page.eval("document.body.innerText.includes('не предоставлен')"), false);
  assert.equal(await page.eval("[...document.querySelectorAll('input')].filter(i => i.id.endsWith('-allowed_values')).map(i => i.value).join('|')"), '0; 5; 10; 15; 20|0; 4; 7; 10');
  // User changes an existing criterion and adds one through the actual editor.
  await page.eval(`(() => {
    const title = document.querySelector('[id$="-title"]'); title.value += ' (проверка UI)'; title.dispatchEvent(new Event('input', {bubbles:true}));
    [...document.querySelectorAll('button')].find(b => b.textContent.includes('Добавить критерий')).click();
    const row = [...document.querySelectorAll('.cqa-crit')].at(-1);
    for (const [key, value] of [['id','browser-new'],['title','Новый критерий через настройки'],['description','ТЕСТОВЫЙ критерий']]) {
      const i = row.querySelector('[id$="-' + key + '"]'); i.value = value; i.dispatchEvent(new Event('input', {bubbles:true}));
    }
    [...document.querySelectorAll('button')].find(b => b.textContent === 'Сохранить как новую версию').click();
  })()`);
  await page.waitFor("!location.hash.endsWith('/official-cqa-v1') && document.querySelectorAll('.cqa-crit').length === 9");
  const editedId = await page.eval("location.hash.split('/').at(-1)");
  const edited = await req('checklists/' + editedId);
  assert.equal(edited.provenance, 'custom');
  assert.deepEqual(edited.criteria.find(c => c.id === '06').allowed_values, [0,4,7,10]);
  assert.equal((await req('checklists/official-cqa-v1')).criteria.length, 8);
  // Deterministic test provider, never inference; proves the new criterion is applied.
  const editedEvaluation = await req('evaluations', {conversation_id:'demo-ru',checklist_id:editedId});
  assert(editedEvaluation.final.result.criteria.some(c => c.criterion_id === 'browser-new'));
  const ev = await req('evaluations', {conversation_id:'demo-ru',checklist_id:'official-cqa-v1'});
  assert(ev.original.answers.every(a => a.explanation.includes('ТЕСТОВЫЙ ПРОВАЙДЕР')));
  const appeal = await req('appeals', {evaluation_id:ev.id,criterion_id:'04',comment:'ТЕСТ UI: пересмотр дискретной шкалы'});
  await page.goto(base + '/#/appeals/' + appeal.id);
  await page.waitFor("document.querySelector('.review-form')");
  await page.eval(`document.querySelector('input[value="change"]').click()`);
  assert.deepEqual(await page.eval("[...document.querySelector('select[aria-label=\"Значение\"]').options].map(o=>Number(o.value))"), [0,5,10,15,20]);
  await page.eval(`(() => {
    const f=document.querySelector('.review-form');
    const reason=f.querySelector('textarea'); reason.value='ТЕСТ UI: подтверждён частичный ответ';
    f.querySelector('select[aria-label="Значение"]').value='15';
    f.querySelector('button[type="submit"]').click();
  })()`);
  await page.waitFor("document.body.innerText.includes('Решение сохранено')");
  const after = await req('evaluations/' + ev.id);
  assert.deepEqual(after.original, ev.original);
  assert.equal(after.final.answers.find(a=>a.criterion_id==='04').answer,15);
  const grossAppeal=await req('appeals',{evaluation_id:ev.id,criterion_id:'08',comment:'ТЕСТ UI: флаг'});
  await page.goto(base + '/#/appeals/' + grossAppeal.id);
  await page.waitFor("document.querySelector('.review-form')");
  await page.eval(`document.querySelector('input[value="change"]').click()`);
  assert(await page.eval("document.body.innerText.includes('Флаг грубой ошибки определяется ответом')"));
  assert.equal(await page.eval("[...document.querySelectorAll('label')].some(l => l.textContent.includes('Грубая ошибка (только'))"),false);
  await page.goto(base + '/#/conversations/demo-ru?eval=' + ev.id);
  await page.waitFor("document.querySelectorAll('.crit').length === 8");
  assert(await page.eval("document.body.innerText.includes('ТЕСТОВЫЙ ПРОВАЙДЕР')"));
  assert.equal(page.errors.length,0,page.errors.join('\n'));
  console.log('PASS: official editor, custom version + new criterion evaluation, discrete appeal select + saved review, original immutability, derived gross flag UI, result card; 0 browser errors. TEST PROVIDER ONLY.');
} finally { await page.close(); }
