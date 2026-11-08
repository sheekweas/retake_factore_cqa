// Guide regression: review a no-gross-error finding using its actual checked span,
// not fabricated evidence or a missed-action flag. Separate synthetic fixture DB;
// no real transcripts, production application or model calls.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launch, sleep } from './cdp.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
mkdirSync(join(ROOT, '.local'), { recursive: true });
const runtime = mkdtempSync(join(ROOT, '.local', 'guide-ui-acceptance-'));
const port = Number(process.env.CQA_GUIDE_UI_PORT || 8047);
const base = `http://127.0.0.1:${port}`;
const python = `
import sys
from pathlib import Path
from server.app import Application, make_server
from server.storage import Store
from web.dev.fixture_server import fixture_provider
def provider(checklist, conversation):
    answers = fixture_provider(checklist, conversation)
    ids = [m['id'] for m in conversation['transcript']]
    for a in answers:
        if a['criterion_id'] == '08':
            if conversation['conversation_id'].endswith('-positive-gross'):
                a.update(answer=True, evidence_message_ids=[ids[2]], missed=False, checked_span=None)
            else:
                a.update(answer=False, evidence_message_ids=[], missed=False,
                         checked_span={'start_message_id':ids[0], 'end_message_id':ids[-1]})
    return answers
app = Application(Store(Path(sys.argv[1]) / 'fixture.sqlite3'), provider=provider)
make_server(app, port=int(sys.argv[2])).serve_forever()
`;
const fixture = spawn(process.env.CQA_PYTHON || 'python', ['-c', python, runtime, String(port)],
  { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
let fixtureLog = '';
fixture.stdout.on('data', d => { fixtureLog += d; });
fixture.stderr.on('data', d => { fixtureLog += d; });
let page;
const api = async (path, body) => {
  const response = await fetch(base + '/api/' + path, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : {});
  const data = await response.json();
  assert.equal(response.status, 200, JSON.stringify(data));
  return data;
};
try {
  let healthy = false;
  for (let i = 0; i < 70; i++) {
    try { if ((await fetch(base + '/api/health')).ok) { healthy = true; break; } } catch { /* starting */ }
    if (fixture.exitCode !== null) break;
    await sleep(100);
  }
  assert(healthy, fixtureLog || 'fixture did not start');
  await api('imports', { source: 'json', group_id: 'GUIDE_UI_TEST', payload: [{
    conversation_id: 'guide-ui-synthetic', language: 'RU', channel: 'agent chat',
    label: 'Синтетический тест интерфейса; не оценка реальной модели',
    transcript: [
      { timestamp: '00:00', speaker: 'system', text: 'Synthetic start.' },
      { timestamp: '00:01', speaker: 'client', text: 'Где тарифы?' },
      { timestamp: '00:02', speaker: 'operator', text: 'Здравствуйте, я Анна. Тарифы на сайте.' },
      { timestamp: '00:03', speaker: 'operator', text: 'Ещё вопросы? До свидания.' },
      { timestamp: '00:04', speaker: 'system', text: 'Synthetic end.' },
    ],
  }] });
  const evaluation = await api('evaluations', { conversation_id: 'guide-ui-synthetic', checklist_id: 'official-cqa-v1' });
  const original = evaluation.original;
  const original08 = original.answers.find(a => a.criterion_id === '08');
  assert.equal(original08.answer, false);
  assert.deepEqual(original08.evidence_message_ids, []);
  const appeal = await api('appeals', { evaluation_id: evaluation.id, criterion_id: '08', comment: 'Synthetic explanation review.' });
  page = await launch({ profileDir: join(runtime, 'browser'), port: 9357 });
  await page.goto(base + '/#/appeals/' + appeal.id);
  await page.waitFor("document.querySelector('.review-form')");
  await page.eval("document.querySelector('input[value=change]').click()");
  assert.equal(await page.eval("document.querySelector('.span-row').hidden"), false);
  assert.deepEqual(await page.eval("[...document.querySelectorAll('.span-row select')].map(s=>s.value)"),
    [original08.checked_span.start_message_id, original08.checked_span.end_message_id]);
  await page.eval(`(() => {
    document.querySelector('.review-form > label.field textarea').value = 'Synthetic reviewer rationale.';
    document.querySelectorAll('.change-editor textarea')[1].value = 'Synthetic: no gross violation in checked span.';
    document.querySelector('.review-form button[type=submit]').click();
  })()`);
  // A success toast from an earlier route can survive hash navigation. Await
  // this form's replacement, which only happens after the review API succeeds.
  await page.waitFor("!document.querySelector('.review-form') && document.body.innerText.includes('Решение сохранено')");
  const after = await api('evaluations/' + evaluation.id);
  const revised = after.final.answers.find(a => a.criterion_id === '08');
  assert.deepEqual(after.original, original);
  assert.equal(revised.answer, false);
  assert.equal(revised.missed, false);
  assert.deepEqual(revised.evidence_message_ids, []);
  assert.deepEqual(revised.checked_span, original08.checked_span);
  assert.equal(after.final.result.gross_error, false);
  console.log('PASS negative gross appeal saves explanation + exact span, no fake evidence/missed action; original immutable');

  // Positive findings must still require a real quote or an explicit missed span.
  const positiveAppeal = await api('appeals', { evaluation_id: evaluation.id, criterion_id: '07', comment: 'Synthetic validation check.' });
  await page.goto(base + '/#/appeals/' + positiveAppeal.id);
  await page.waitFor("document.querySelector('.review-form')");
  await page.eval(`(() => {
    document.querySelector('input[value=change]').click();
    document.querySelector('.review-form > label.field textarea').value = 'Synthetic required evidence check.';
    for (const b of document.querySelectorAll('.ev-list input')) b.checked = false;
    document.querySelector('.review-form button[type=submit]').click();
  })()`);
  await page.waitFor("document.body.innerText.includes('Для применимого ответа укажите доказательства')");
  const appeals = await api('appeals?evaluation_id=' + evaluation.id);
  assert.equal(appeals.items.find(a => a.id === positiveAppeal.id).status, 'pending');
  console.log('PASS positive finding evidence remains required; invalid review not saved');

  // Opposite-verdict quotations must not silently carry over from YES to NO.
  const sourceConversation = await api('conversations/guide-ui-synthetic');
  await api('imports', { source: 'json', group_id: 'GUIDE_UI_TEST', payload: [{
    ...sourceConversation, conversation_id: 'guide-ui-synthetic-positive-gross',
    transcript: sourceConversation.transcript.map(({ id, ...message }) => message),
  }] });
  const positiveGrossEvaluation = await api('evaluations', {
    conversation_id: 'guide-ui-synthetic-positive-gross', checklist_id: 'official-cqa-v1',
  });
  assert.equal(positiveGrossEvaluation.original.result.gross_error, true);
  const grossAppeal = await api('appeals', {
    evaluation_id: positiveGrossEvaluation.id, criterion_id: '08', comment: 'Synthetic YES to NO correction.',
  });
  await page.goto(base + '/#/appeals/' + grossAppeal.id);
  await page.waitFor("document.querySelector('.review-form')");
  await page.eval("document.querySelector('input[value=change]').click()");
  assert.equal(await page.eval("document.querySelectorAll('.ev-list input:checked').length"), 1);
  await page.eval("document.querySelector('.change-editor input[value=false]').click()");
  assert.equal(await page.eval("document.querySelectorAll('.ev-list input:checked').length"), 0);
  const conversation = await api('conversations/guide-ui-synthetic-positive-gross');
  const fullSpan = {
    start_message_id: conversation.transcript[0].id,
    end_message_id: conversation.transcript.at(-1).id,
  };
  assert.deepEqual(await page.eval("[...document.querySelectorAll('.span-row select')].map(s=>s.value)"),
    [fullSpan.start_message_id, fullSpan.end_message_id]);
  // A quotation cannot make a partial review sufficient for an absence verdict.
  await page.eval(`(() => {
    document.querySelector('.review-form > label.field textarea').value = 'Synthetic no violation after full review.';
    document.querySelectorAll('.change-editor textarea')[1].value = 'Synthetic corrected no-violation rationale.';
    document.querySelector('.ev-list input').checked = true;
    const start = document.querySelector('.span-row select');
    start.selectedIndex = 1;
    document.querySelector('.review-form button[type=submit]').click();
  })()`);
  await page.waitFor("document.body.innerText.includes('проверьте весь диалог')");
  await page.eval(`(() => {
    document.querySelector('.ev-list input').checked = false;
    document.querySelector('.span-row select').selectedIndex = 0;
    document.querySelector('.review-form button[type=submit]').click();
  })()`);
  await page.waitFor("!document.querySelector('.review-form') && document.body.innerText.includes('Решение сохранено')");
  const grossAfter = await api('evaluations/' + positiveGrossEvaluation.id);
  const grossFinal = grossAfter.final.answers.find(a => a.criterion_id === '08');
  assert.deepEqual(grossAfter.original, positiveGrossEvaluation.original);
  assert.equal(grossFinal.answer, false);
  assert.equal(grossFinal.missed, false);
  assert.deepEqual(grossFinal.evidence_message_ids, []);
  assert.deepEqual(grossFinal.checked_span, fullSpan);
  assert.equal(grossAfter.final.result.gross_error, false);
  assert.equal(page.errors.length, 0, page.errors.join('\n'));
  console.log('PASS YES->NO clears stale quotes and defaults to full span; partial span with quotes blocked; original immutable, gross=false; 0 browser errors. TEST PROVIDER ONLY.');
} finally {
  if (page) await page.close();
  fixture.kill();
}
