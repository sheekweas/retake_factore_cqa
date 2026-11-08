// Right column of the conversation card: score summary, actions, criteria, appeals.
// Main flow shows the score, status and short rationale; weights, normalization and execution
// metadata live in `details.disclosure` blocks.
import { api } from '../app/api.js';
import { h, mount, errorBox, toast, fmtScore, fmtPct, fmtDate, provenanceBadge } from '../app/dom.js';
import { navigate, href } from '../app/router.js';
import {
  criterionIndex, formatAnswerValue, formatTotal, criterionTypeLabel, answersEqual, answerDetails,
  appealStatusChip, decisionLabel, appealLink, checklistLabel, answerPill,
} from './answer-format.js';

const POLICY_TEXT = {
  flag: 'политика «flag»: грубая ошибка только отмечается, итог не меняется',
  zero_total: 'политика «zero_total»: при грубой ошибке итог обнулён (баллы разделов сохранены для диагностики)',
};
const RATIONALE_MAX = 220;

/**
 * ctx: { conversation, evaluations (newest first), selected, checklists (Map id->checklist|Error),
 *        allChecklists (array|Error), appeals (array|Error), transcript, onSelect(evalId), onReload() }
 */
export function renderEvaluationPanel(ctx) {
  const root = h('section.panel.eval-panel', { 'aria-label': 'Оценка' });
  if (!ctx.evaluations.length) {
    root.append(
      h('div.eval-empty',
        h('h2', 'Оценка'),
        h('p.muted', 'Диалог ещё не оценён. Выберите версию чек-листа и запустите оценку.')),
      evaluateBox(ctx));
    return root;
  }
  if (!ctx.selected) {
    root.append(h('h2', 'Оценка'), evaluateBox(ctx), selectorBox(ctx),
      h('div.alert.alert-warn', 'Выбранная оценка не найдена для этого диалога.'));
    return root;
  }
  root.append(evaluationView(ctx));
  return root;
}

function evaluateBox(ctx) {
  const box = h('div.evaluate-box');
  const status = h('div.evaluate-status', { 'aria-live': 'polite' });
  if (ctx.allChecklists instanceof Error) {
    box.append(errorBox(ctx.allChecklists, 'Не удалось загрузить чек-листы'));
    return box;
  }
  const items = [...ctx.allChecklists].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  if (!items.length) {
    box.append(h('div.alert.alert-warn', 'Нет сохранённых чек-листов. ', h('a', { href: href('/checklists/new') }, 'Создать чек-лист')));
    return box;
  }
  const official = items.find((c) => c.id === 'official-cqa-v1') || items.find((c) => c.provenance === 'official');
  const preferred = ctx.selected ? ctx.selected.checklist_id : (official || items[0]).id;
  const select = h('select', { 'aria-label': 'Версия чек-листа' },
    items.map((c) => h('option', { value: c.id, selected: c.id === preferred },
      `${checklistLabel(c)} — ${c.provenance === 'official' ? 'официальный' : c.provenance === 'demonstration' ? 'демонстрационный' : 'пользовательский'}`)));
  const btn = h('button.btn.btn-primary', { type: 'button' }, 'Оценить');
  btn.addEventListener('click', async () => {
    btn.disabled = true; select.disabled = true;
    mount(status, h('div.loading', 'Модель выполняет оценку… Это может занять несколько минут.'));
    try {
      const ev = await api.evaluate(ctx.conversation.conversation_id, select.value);
      toast('Оценка создана', 'ok');
      navigate('/conversations/' + encodeURIComponent(ctx.conversation.conversation_id), { eval: ev.id });
    } catch (e) {
      const timedOut = e.code === 'timeout' || e.serverMessage === 'Модель не ответила вовремя. Повторите оценку; предыдущие результаты сохранены.';
      mount(status, errorBox(e, 'Оценка не выполнена'),
        timedOut ? h('p.muted.small', e.code === 'timeout'
          ? 'Проверьте историю оценок перед повтором: сервер мог ещё завершать запрос.'
          : 'Оценка не сохранена. Можно повторить попытку по выбранному чек-листу.') : null);
      if (timedOut) btn.textContent = 'Повторить оценку';
      btn.disabled = false; select.disabled = false;
    }
  });
  box.append(h('div.evaluate-row', h('label.field', h('span', ctx.evaluations.length ? 'Новая оценка по чек-листу' : 'Чек-лист'), select), btn), status);
  return box;
}

function selectorBox(ctx) {
  const select = h('select', { 'aria-label': 'Оценка' },
    ctx.evaluations.map((ev) => {
      const cl = ctx.checklists.get(ev.checklist_id);
      const clText = cl && !(cl instanceof Error) ? checklistLabel(cl) : 'чек-лист ' + ev.checklist_id;
      return h('option', { value: ev.id, selected: ctx.selected && ev.id === ctx.selected.id }, `${fmtDate(ev.created_at)} — ${clText}${!ev.provider ? ' — архивная, модель не подтверждена' : ''}`);
    }));
  select.addEventListener('change', () => ctx.onSelect(select.value));
  return h('div.eval-selector',
    h('label.field', h('span', `Показана оценка (всего ${ctx.evaluations.length})`), select));
}

function totalBlock(label, total, extraCls = '', title) {
  return h('div.total-box' + extraCls, { title },
    h('div.total-label', label),
    h('div.total-value', h('div.score-big', formatTotal(total)), total === null || total === undefined ? null : h('span.total-of', 'из 100')));
}

function evaluationView(ctx) {
  const ev = ctx.selected;
  const cl = ctx.checklists.get(ev.checklist_id);
  const checklist = cl && !(cl instanceof Error) ? cl : null;
  const crit = criterionIndex(checklist);
  const fin = ev.final.result;
  const orig = ev.original.result;
  const rules = fin.rules || {};
  const appeals = ctx.appeals instanceof Error ? [] : ctx.appeals;
  const pendingCount = appeals.filter((a) => a.status === 'pending').length;
  const changed = fin.total !== orig.total;
  const answersChanged = !answersListEqual(ev);
  const out = h('div.eval-view');

  // ---------- summary: score, status, gross flag, actions ----------
  const statusBadges = h('div.eval-status',
    !ev.provider ? h('span.badge.badge-warn', 'архивная · модель не подтверждена') : null,
    pendingCount
      ? h('span.badge.badge-warn', `апелляции на рассмотрении: ${pendingCount}`)
      : answersChanged
        ? h('span.badge.badge-custom', 'скорректировано по апелляции')
        : h('span.badge.badge-ok', 'оценено'),
    fin.gross_error
      ? h('span.badge.badge-gross', 'грубая ошибка')
      : h('span.badge.badge-neutral', 'грубых ошибок нет'));

  const meta = h('div.eval-meta',
    provenanceBadge(fin.provenance || (checklist && checklist.provenance)),
    h('span.eval-checklist', checklist ? checklistLabel(checklist) : 'чек-лист ' + ev.checklist_id),
    h('span.muted', fmtDate(ev.created_at)),
    ev.provider && ev.provider.type === 'injected' ? providerLabel(ev.provider) : null);

  out.append(h('div.eval-summary',
    h('div.eval-scores',
      totalBlock('Итоговый балл',fin.total, '.total-final'),
      changed ? totalBlock('Исходный балл модели', orig.total, '.total-original', 'Исходная оценка модели не изменяется; итог пересчитан по решениям апелляций') : null),
    h('div.eval-side', statusBadges, meta)));

  if (cl instanceof Error) out.append(errorBox(cl, 'Чек-лист оценки не загружен'));

  if (fin.gross_error) {
    const grossRow = (fin.criteria || []).find((r) => r.gross_error);
    out.append(h('div.alert.alert-error.gross-banner', { role: 'alert' },
      h('span.badge.badge-gross', 'ГРУБАЯ ОШИБКА'),
      h('span', rules.gross_error_policy === 'zero_total'
        ? 'Итог обнулён (политика zero_total). Баллы разделов сохранены для диагностики.'
        : 'Отмечена отдельно, итог не изменён (политика flag).'),
      grossRow ? h('button.btn-link', { type: 'button', on: { click: () => scrollToCrit(grossRow.criterion_id) } }, 'К критерию ' + grossRow.criterion_id) : null));
  }
  if (orig.gross_error !== fin.gross_error) {
    out.append(h('div.muted.small', `Исходная оценка модели: грубая ошибка — ${orig.gross_error ? 'да' : 'нет'}.`));
  }
  if (!changed && answersChanged) {
    out.append(h('div.muted.small', 'Ответы изменены по апелляциям; итог совпадает с исходным.'));
  }

  out.append(h('div.eval-actions', evaluateBox(ctx), ctx.evaluations.length > 1 ? selectorBox(ctx) : null));

  out.append(h('p.eval-note',
    ev.provider && ev.provider.type === 'injected'
      ? 'Ответы созданы тестовым провайдером без модели — только для проверки интерфейса.'
      : !ev.provider
        ? 'Архивная оценка: модель и параметры выполнения не записаны. Для актуального результата запустите новую оценку; история сохранится.'
        : 'Автоматическая оценка — предложение модели: проверьте доказательства, при несогласии подайте апелляцию.'));

  out.append(techDetails(ev, fin, orig, rules, checklist));

  // ---------- criteria grouped by section ----------
  if (ctx.appeals instanceof Error) out.append(errorBox(ctx.appeals, 'Апелляции не загружены'));
  const origRows = new Map((orig.criteria || []).map((r) => [r.criterion_id, r]));
  const groups = new Map();
  for (const row of fin.criteria || []) {
    const key = row.section || '';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  }
  out.append(h('h3.crit-heading', 'Критерии'));
  for (const [name, rows] of groups) {
    out.append(h('div.crit-group',
      h('h4.crit-group-title', name || 'Без раздела'),
      rows.map((row) => criterionCard(row, origRows.get(row.criterion_id), crit.get(row.criterion_id), appeals, ctx))));
  }
  return out;
}

function scrollToCrit(id) {
  const el = document.getElementById('crit-' + id);
  if (!el) return;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}

function techDetails(ev, fin, orig, rules, checklist) {
  const origSec = new Map((orig.sections || []).map((s) => [s.name, s]));
  const p = ev.provider;
  return h('details.disclosure.eval-tech',
    h('summary', 'Расчёт и технические детали'),
    h('dl.kv.totals-kv',
      h('dt', 'Применимый вес'), h('dd', fmtScore(fin.applicable_weight)),
      h('dt', 'Округление'), h('dd', `${rules.decimals ?? '—'} знак(а), HALF_UP`),
      h('dt', 'Грубая ошибка'), h('dd', POLICY_TEXT[rules.gross_error_policy] || String(rules.gross_error_policy || '—')),
      h('dt', 'Исполнитель'), h('dd', providerLabel(p)),
      p && p.prompt_revision ? [h('dt', 'Ревизия промпта'), h('dd', h('code', p.prompt_revision))] : null,
      h('dt', 'Чек-лист'), h('dd', checklist ? checklistLabel(checklist) : ev.checklist_id),
      h('dt', 'ID оценки'), h('dd', h('code.small', ev.id))),
    h('p.muted.small', fin.provenance === 'official'
      ? 'Итог — сумма вкладов применимых критериев, нормированная на применимый вес; N/A не учитывается. Округление точной половины HALF_UP выбрано нами (PDF его не уточняет).'
      : 'Правила этой версии заданы в редакторе чек-листа.'),
    (fin.sections || []).length
      ? h('div.table-scroll', h('table.table.sections-table',
        h('thead', h('tr', h('th', 'Раздел'), h('th', 'Итог раздела'), h('th', 'Применимый вес'), h('th', 'Исходный итог'))),
        h('tbody', fin.sections.map((s) => {
          const o = origSec.get(s.name);
          return h('tr',
            h('td', s.name || 'Без раздела'),
            h('td', formatTotal(s.total)),
            h('td', fmtScore(s.applicable_weight)),
            h('td.muted', o ? formatTotal(o.total) : '—'));
        }))))
      : null);
}

// Execution metadata snapshotted on the evaluation (contracts/api.md); legacy rows omit it.
function providerLabel(p) {
  let text;
  let cls = '.badge-neutral';
  if (!p) text = 'модель не записана (оценка до сохранения метаданных)';
  else if (p.type === 'ollama') text = 'модель ' + (p.model || 'не указана');
  else if (p.type === 'injected') { text = 'ТЕСТОВЫЙ ПРОВАЙДЕР (injected), не модель'; cls = '.badge-warn'; }
  else text = 'провайдер: ' + String(p.type || 'неизвестен');
  return h('span.badge' + cls + '.eval-provider', { title: 'Метаданные выполнения, сохранённые при создании оценки' }, text);
}

function answersListEqual(ev) {
  const o = new Map(ev.original.answers.map((a) => [a.criterion_id, a]));
  return ev.final.answers.every((a) => answersEqual(a, o.get(a.criterion_id)));
}

const isNullish = (v) => v === null || v === undefined;

function criterionCard(row, origRow, criterion, appeals, ctx) {
  const isNA = isNullish(row.answer);
  const differs = origRow && !answersEqual(row, origRow);
  const handlers = {
    criterion,
    conversation: ctx.conversation,
    onEvidence: (id) => ctx.transcript.focusMessage(id),
    onSpan: (sp) => ctx.transcript.highlightSpan(sp),
  };
  const critDef = criterion || { type: row.type, weight: row.weight };
  const card = h('article.crit' + (isNA ? '.crit-na' : '') + (row.gross_error ? '.crit-gross' : ''), { id: 'crit-' + row.criterion_id });

  card.append(h('header.crit-head',
    h('span.crit-num', row.criterion_id),
    h('div.crit-head-main',
      h('div.crit-title', row.title),
      h('div.crit-tags',
        Number(row.weight) === 0 ? h('span.badge.badge-neutral', 'без баллов') : null,
        row.gross_error ? h('span.badge.badge-gross', 'грубая ошибка') : null,
        differs ? h('span.badge.badge-custom', 'изменено по апелляции') : null)),
    h('div.crit-pill',
      // Strike the original only when the value itself changed; evidence/explanation-only
      // changes keep the "изменено по апелляции" tag without a misleading "10/10 → 10/10".
      differs && (origRow.answer !== row.answer || origRow.score !== row.score)
        ? h('span.pill-was', { title: 'Исходный ответ модели' }, answerPill(origRow, critDef)) : null,
      answerPill(row, critDef, '.pill-lg'))));

  card.append(h('div.crit-body', answerDetails(row, { ...handlers, showValue: false, maxExplanation: RATIONALE_MAX })));

  if (differs) {
    card.append(h('details.disclosure.crit-original',
      h('summary', 'Исходный ответ модели (неизменяемый)'),
      h('div.muted.small', Number(origRow.weight) === 0
        ? 'Без баллов, отдельная отметка.'
        : `Балл исходного ответа: ${isNullish(origRow.score) ? '—' : fmtScore(origRow.score)}.`),
      answerDetails(origRow, { ...handlers, label: 'Исходный ответ' })));
  }

  const fullExpl = row.explanation && String(row.explanation).length > RATIONALE_MAX ? String(row.explanation) : null;
  card.append(h('details.disclosure.crit-more',
    h('summary', 'Подробнее'),
    h('dl.kv.crit-scores',
      h('dt', 'Ответ'), h('dd', formatAnswerValue(row.answer, criterion)),
      h('dt', 'Балл'), h('dd', isNA ? '— (N/A, не учитывается)'
        : Number(row.weight) === 0 ? '— (без баллов, отдельная отметка)' : fmtScore(row.score)),
      h('dt', 'Вес → эффективный'), h('dd', `${fmtScore(row.weight)} → ${fmtPct(row.effective_weight)}`),
      h('dt', 'Вклад в итог'), h('dd', fmtScore(row.weighted_score)),
      criterion ? [h('dt', 'Тип'), h('dd', criterionTypeLabel(criterion))] : null),
    fullExpl ? h('div.crit-full-expl', h('div.muted.small', 'Полное пояснение'), h('div.pre', fullExpl)) : null,
    criterion && criterion.description ? h('div.crit-desc', h('div.muted.small', 'Что проверяет критерий'), h('div.pre', criterion.description)) : null,
    criterion && criterion.na_condition ? h('div.crit-desc', h('div.muted.small', 'Когда N/A'), h('div', criterion.na_condition)) : null));

  card.append(appealsArea(row, appeals.filter((a) => a.criterion_id === row.criterion_id), ctx));
  return card;
}

function appealsArea(row, list, ctx) {
  const area = h('div.crit-appeals');
  const pending = list.find((a) => a.status === 'pending');
  const reviewed = list.filter((a) => a.status === 'reviewed')
    .sort((a, b) => String(b.review?.reviewed_at || b.created_at).localeCompare(String(a.review?.reviewed_at || a.created_at)));

  if (pending) {
    area.append(h('div.appeal-pending', appealStatusChip(pending),
      h('span.muted.small', 'подана ' + fmtDate(pending.created_at)),
      appealLink(pending, 'Открыть для решения')));
  } else {
    const formHost = h('div.appeal-form-host');
    const btn = h('button.btn.btn-small', { type: 'button', 'aria-expanded': 'false' }, 'Апелляция');
    btn.addEventListener('click', () => {
      if (formHost.firstChild) { mount(formHost); btn.setAttribute('aria-expanded', 'false'); return; }
      btn.setAttribute('aria-expanded', 'true');
      mount(formHost, appealForm(row, ctx, () => { mount(formHost); btn.setAttribute('aria-expanded', 'false'); }));
      formHost.querySelector('textarea').focus();
    });
    area.append(h('div.appeal-actions', btn), formHost);
  }

  if (reviewed.length) {
    area.append(h('details.appeal-history', { open: false },
      h('summary', `Решения по апелляциям (${reviewed.length})`),
      h('ul', reviewed.map((a) => h('li',
        h('div.appeal-history-head', h('strong', decisionLabel(a.review?.decision)),
          h('span.muted', fmtDate(a.review?.reviewed_at)),
          a.review ? h('span.appeal-total-change',
            h('span.val-was', formatTotal(a.review.previous_result?.total)), ' → ',
            h('strong', formatTotal(a.review.final_result?.total))) : null,
          appealLink(a, 'подробнее')),
        h('div.pre', h('span.muted', 'Комментарий: '), a.comment),
        h('div.pre', h('span.muted', 'Причина решения: '), a.review?.reason || ''))))));
  }
  return area;
}

function appealForm(row, ctx, close) {
  const ta = h('textarea', { required: true, 'aria-label': 'Комментарий к апелляции', placeholder: 'Почему ответ по критерию неверен' });
  const err = h('div.form-status', { 'aria-live': 'polite' });
  const submit = h('button.btn.btn-primary.btn-small', { type: 'submit' }, 'Подать апелляцию');
  const form = h('form.appeal-form', { novalidate: true },
    h('label.field', h('span', `Комментарий к апелляции по «${row.title}»`), ta),
    h('div.muted.small', 'Подача не меняет балл — решение принимает проверяющий.'),
    err,
    h('div.row', submit, h('button.btn.btn-small', { type: 'button', on: { click: close } }, 'Отмена')));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const comment = ta.value.trim();
    if (!comment) { mount(err, h('div.field-error', 'Комментарий обязателен')); ta.focus(); return; }
    submit.disabled = true;
    mount(err, h('div.loading', 'Отправка…'));
    try {
      await api.fileAppeal(ctx.selected.id, row.criterion_id, comment);
      toast('Апелляция подана; балл не изменён до решения проверяющего', 'ok');
      await ctx.onReload();
    } catch (e2) {
      mount(err, errorBox(e2, 'Апелляция не подана'));
      submit.disabled = false;
    }
  });
  return form;
}
