// Route #/appeals/:id — appeal details and the reviewer's decision (separate action from filing).
import { api } from '../app/api.js';
import { h, mount, errorBox, loading, toast, fmtDate, fmtMsgTime, ROLE_LABELS, LANG_ATTR } from '../app/dom.js';
import { href } from '../app/router.js';
import {
  criterionIndex, formatTotal, criterionTypeLabel, answerDetails, appealStatusChip,
  decisionLabel, checklistHeader, spanIds, shortMsgId,
} from '../results/answer-format.js';

const CONTEXT = 2;

export async function render(container, { params }) {
  const id = params.id;
  mount(container, loading('Загрузка апелляции…'));
  const back = h('p', h('a', { href: href('/appeals') }, '← Все апелляции'));

  let appeal;
  try {
    const all = await api.appeals();
    appeal = all.find((a) => a.id === id);
  } catch (e) {
    mount(container, back, errorBox(e, 'Не удалось загрузить апелляции'));
    return;
  }
  if (!appeal) {
    mount(container, back, errorBox({ message: 'Апелляция не найдена: ' + id, status: 404, code: 'not_found' }, 'Не найдено'));
    return;
  }

  let evaluation, checklist, conversation;
  try {
    evaluation = await api.evaluation(appeal.evaluation_id);
    [checklist, conversation] = await Promise.all([
      api.checklist(evaluation.checklist_id),
      api.conversation(evaluation.conversation_id),
    ]);
  } catch (e) {
    mount(container, back, errorBox(e, 'Не удалось загрузить оценку, чек-лист или диалог'));
    return;
  }

  const state = { appeal, evaluation, checklist, conversation };
  draw(container, state, null);
}

function draw(container, state, successInfo) {
  const { appeal, evaluation, checklist, conversation } = state;
  const criterion = criterionIndex(checklist).get(appeal.criterion_id);
  const cid = conversation.conversation_id;
  const origAnswer = evaluation.original.answers.find((a) => a.criterion_id === appeal.criterion_id);
  const finalAnswer = evaluation.final.answers.find((a) => a.criterion_id === appeal.criterion_id);
  const cardLink = (mid) => href('/conversations/' + encodeURIComponent(cid), { eval: evaluation.id, msg: mid });
  const onEvidence = (mid) => { location.hash = cardLink(mid); };

  const title = criterion ? criterion.title : appeal.criterion_id;
  const reviewed = appeal.status === 'reviewed' && appeal.review;
  const origTotal = evaluation.original.result.total;
  const finalTotal = evaluation.final.result.total;
  const totalsDiffer = origTotal !== finalTotal;
  const header = h('div.appeal-header',
    h('div.card-top', h('a.back-link', { href: href('/appeals') }, '← Все апелляции')),
    h('div.page-head.appeal-title',
      h('h1', h('span.crit-num', appeal.criterion_id), ' ', title),
      appealStatusChip(appeal)),
    h('div.appeal-meta',
      h('span', 'Диалог ', h('a', { href: href('/conversations/' + encodeURIComponent(cid), { eval: evaluation.id }) }, cid)),
      checklistHeader(checklist),
      criterion ? h('span.badge.badge-neutral', criterionTypeLabel(criterion)) : null));

  const steps = h('ol.appeal-steps', { 'aria-label': 'Ход апелляции' },
    step('done', 'Подана', fmtDate(appeal.created_at)),
    step(reviewed ? 'done' : 'current', 'Рассмотрение', reviewed ? 'завершено' : 'ожидает решения проверяющего'),
    step(reviewed ? 'done' : 'todo', 'Решение', reviewed ? `${decisionLabel(appeal.review.decision)} · ${fmtDate(appeal.review.reviewed_at)}` : '—'));

  const comment = h('section.panel.appeal-comment-panel',
    h('h2', 'Комментарий заявителя'),
    h('div.appeal-comment-text', appeal.comment));

  const compare = h('div.appeal-compare',
    h('section.panel.panel-disputed',
      h('h2', 'Оспариваемый ответ'),
      h('div.muted.small', 'снимок на момент подачи'),
      answerDetails(appeal.disputed_answer, { criterion, conversation, onEvidence, label: 'Ответ' })),
    h('section.panel.panel-original' + (totalsDiffer ? '.is-superseded' : ''),
      h('h2', 'Исходная оценка модели'),
      h('div.total-line', 'Итог: ', h('strong', formatTotal(origTotal)), h('span.muted.small', ' неизменяемый')),
      answerDetails(origAnswer, { criterion, conversation, onEvidence, label: 'Ответ' })),
    h('section.panel.panel-final',
      h('h2', 'Текущий итоговый результат'),
      h('div.total-line', 'Итог: ', h('strong', formatTotal(finalTotal))),
      answerDetails(finalAnswer, { criterion, conversation, onEvidence, label: 'Ответ' })));

  const evidenceSets = [appeal.disputed_answer, origAnswer, finalAnswer, appeal.review && appeal.review.answer].filter(Boolean);
  const fragment = dialogFragment(conversation, evidenceSets, cardLink);

  const decision = reviewed
    ? reviewRecord(appeal, criterion, conversation, onEvidence, successInfo)
    : reviewForm(state, criterion, finalAnswer || appeal.disputed_answer, (updated) => {
      toast('Решение сохранено', 'ok');
      draw(container, { ...state, appeal: updated.appeal, evaluation: updated.evaluation }, { justSaved: true });
    });

  if (reviewed) mount(container, header, steps, h('div.appeal-top-grid', decision, comment), compare, fragment);
  else mount(container, header, steps, comment, compare, fragment, decision);
  if (successInfo) decision.scrollIntoView({ block: 'start' });
}

function step(state, label, detail) {
  return h('li.appeal-step.step-' + state, { 'aria-current': state === 'current' ? 'step' : undefined },
    h('span.step-dot', { 'aria-hidden': 'true' }),
    h('span.step-body', h('span.step-label', label), h('span.step-detail', detail)));
}

/** Evidence/span messages with ±CONTEXT neighbours, grouped into contiguous ranges. */
function dialogFragment(conversation, answers, cardLink) {
  const tr = conversation.transcript || [];
  const idx = new Map(tr.map((m, i) => [m.id, i]));
  const key = new Set();
  for (const a of answers) {
    for (const mid of a.evidence_message_ids || []) if (idx.has(mid)) key.add(idx.get(mid));
    for (const mid of spanIds(tr, a.checked_span)) key.add(idx.get(mid));
  }
  const panel = h('section.panel', h('h2', 'Фрагмент диалога'));
  if (!key.size) {
    panel.append(h('div.muted', 'В ответах нет ссылок на сообщения (например, N/A без доказательств). '),
      h('a', { href: cardLink() }, 'Открыть весь диалог в карточке'));
    return panel;
  }
  const show = new Set();
  for (const i of key) for (let j = Math.max(0, i - CONTEXT); j <= Math.min(tr.length - 1, i + CONTEXT); j++) show.add(j);
  const sorted = [...show].sort((a, b) => a - b);
  const lang = LANG_ATTR[conversation.language];
  const list = h('ol.fragment-list');
  let prev = -1;
  for (const i of sorted) {
    if (prev >= 0 && i > prev + 1) list.append(h('li.fragment-gap.muted', `… пропущено сообщений: ${i - prev - 1}`));
    if (prev < 0 && i > 0) list.append(h('li.fragment-gap.muted', `… ${i} сообщ. выше`));
    const m = tr[i];
    const speaker = ROLE_LABELS[m.speaker] ? m.speaker : 'system';
    list.append(h('li.fragment-msg.msg-' + speaker + (key.has(i) ? '.is-key' : ''),
      h('div.msg-head',
        h('span.role.role-' + speaker, ROLE_LABELS[m.speaker] || String(m.speaker || '')),
        msgTs(m.timestamp),
        key.has(i) ? h('span.badge.badge-custom', 'доказательство / участок') : h('span.muted.small', 'контекст'),
        h('a.small', { href: cardLink(m.id), title: m.id }, shortMsgId(m.id) + ' — открыть в карточке')),
      h('div.msg-text', { lang }, m.text === undefined || m.text === null ? '' : String(m.text))));
    prev = i;
  }
  if (prev < tr.length - 1) list.append(h('li.fragment-gap.muted', `… ${tr.length - 1 - prev} сообщ. ниже`));
  panel.append(list);
  return panel;
}

function reviewRecord(appeal, criterion, conversation, onEvidence, successInfo) {
  const r = appeal.review;
  const prev = formatTotal(r.previous_result && r.previous_result.total);
  const next = formatTotal(r.final_result && r.final_result.total);
  return h('section.panel.decision-panel',
    h('h2', 'Решение проверяющего'),
    successInfo && successInfo.justSaved ? h('div.alert.alert-info', 'Решение сохранено. Исходная оценка модели не изменилась; обновлён только итоговый результат.') : null,
    h('div.decision-head',
      h('span.badge.decision-badge' + (r.decision === 'change' ? '.badge-custom' : '.badge-ok'), decisionLabel(r.decision)),
      h('span.muted.small', fmtDate(r.reviewed_at)),
      h('span.decision-total', { title: 'Итог до решения → после решения' },
        h('span.val-was', prev), ' → ', h('strong', next))),
    h('div.decision-reason', h('div.muted.small', 'Причина решения'), h('div.pre', r.reason)),
    r.decision === 'change' && r.answer
      ? h('div.decision-answer', h('div.muted.small', 'Новый ответ'), answerDetails(r.answer, { criterion, conversation, onEvidence, label: 'Ответ' }))
      : h('div.muted.small', 'Текущий ответ подтверждён без изменений.'),
    h('div.muted.small', 'Повторное решение по этой апелляции невозможно (сервер вернёт 409).'));
}

/** Compact source clock time; the verbatim source value stays in title and data-source-timestamp. */
function msgTs(raw) {
  const t = fmtMsgTime(raw);
  const src = raw === undefined || raw === null ? '' : String(raw);
  return h('span.msg-ts', { title: t.title || null, dataset: src ? { sourceTimestamp: src } : {} }, t.text);
}

let formSeq = 0;

function reviewForm(state, criterion, current, onSaved) {
  const { appeal, conversation } = state;
  const n = ++formSeq;
  const tr = conversation.transcript || [];
  const err = h('div.form-status', { 'aria-live': 'polite' });
  const panel = h('section.panel.decision-panel', h('h2', 'Решение проверяющего'),
    h('p.muted.small', '«Подтвердить» сохраняет текущий ответ; «Изменить» заменяет его и пересчитывает итог. Исходная оценка модели не меняется.'));

  const radio = (value, label, hint, checked) => h('label.decision-option',
    h('input', { type: 'radio', name: 'decision-' + n, value, checked }),
    h('span.decision-option-body', h('span.decision-option-label', label), h('span.muted.small', hint)));
  const decisionGroup = h('fieldset.decision-choice', h('legend', 'Решение'),
    h('div.decision-options',
      radio('confirm', 'Подтвердить', 'ответ остаётся, итог не меняется', true),
      radio('change', 'Изменить ответ', 'новый ответ заменит текущий', false)));
  const reason = h('textarea', { required: true, placeholder: 'Обоснование решения (обязательно)' });

  // ---- change editor ----
  const editor = h('fieldset.change-editor', { hidden: true }, h('legend', 'Новый ответ (полностью заменяет текущий)'));
  if (!criterion) {
    editor.append(h('div.alert.alert-error', 'Критерий не найден в чек-листе оценки — изменение невозможно.'));
  }
  const isNumeric = criterion && criterion.type === 'numeric';
  const allowNA = Boolean(criterion && criterion.allow_na);
  const curNA = current && (current.answer === null || current.answer === undefined);

  const valueName = 'value-' + n;
  const valueOpts = [];
  let numInput = null;
  if (isNumeric) {
    numInput = Array.isArray(criterion.allowed_values) ? h('select', { 'aria-label': 'Значение' }, criterion.allowed_values.map(v => h('option', { value: v, selected: current?.answer === v }, `${v} — ${criterion.scale_labels?.[String(v)] || ''}`))) : h('input', { type: 'number', min: criterion.min, max: criterion.max, step: 'any', value: !curNA && typeof current.answer === 'number' ? current.answer : '', 'aria-label': 'Значение' });
    valueOpts.push(h('label.radio.value-option', h('input', { type: 'radio', name: valueName, value: 'num', checked: !curNA }), ' Значение ', numInput, h('span.muted', ` (${criterion.min}–${criterion.max})`)));
  } else if (criterion) {
    valueOpts.push(h('label.radio', h('input', { type: 'radio', name: valueName, value: 'true', checked: !curNA && current.answer === true }), ' Да'));
    valueOpts.push(h('label.radio', h('input', { type: 'radio', name: valueName, value: 'false', checked: !curNA && current.answer === false }), ' Нет'));
  }
  if (allowNA) valueOpts.push(h('label.radio', h('input', { type: 'radio', name: valueName, value: 'na', checked: curNA }), ' N/A (неприменимо)'));
  const naReason = h('textarea', { value: (current && current.na_reason) || '', placeholder: 'Причина N/A (обязательно для N/A)' });
  const naField = h('label.field', h('span', 'Причина N/A'), naReason);
  const explanation = h('textarea', { value: (current && current.explanation) || '' });

  const curEvidence = new Set((current && current.evidence_message_ids) || []);
  const evBoxes = tr.map((m) => h('label.ev-option',
    h('input', { type: 'checkbox', value: m.id, checked: curEvidence.has(m.id) }), ' ',
    h('span.role.role-' + (ROLE_LABELS[m.speaker] ? m.speaker : 'system'), ROLE_LABELS[m.speaker] || String(m.speaker || '')), ' ',
    msgTs(m.timestamp), ' ',
    h('span.ev-text', { lang: LANG_ATTR[conversation.language] }, truncate(m.text, 120)), ' ',
    h('code.muted.small', shortMsgId(m.id))));

  const missed = h('input', { type: 'checkbox', checked: Boolean(current && current.missed) });
  const missedLabel = h('label.radio', missed, ' Действие пропущено (нет доказательств; укажите проверенный участок)');
  const msgOption = (m, sel) => h('option', { value: m.id, selected: sel }, `${shortMsgId(m.id)} ${ROLE_LABELS[m.speaker] || m.speaker} ${fmtMsgTime(m.timestamp).text}`.trim());
  const sp = current && current.checked_span;
  const spanStart = h('select', { 'aria-label': 'Начало участка' }, tr.map((m, i) => msgOption(m, sp ? m.id === sp.start_message_id : i === 0)));
  const spanEnd = h('select', { 'aria-label': 'Конец участка' }, tr.map((m, i) => msgOption(m, sp ? m.id === sp.end_message_id : i === tr.length - 1)));
  const spanRow = h('div.row.span-row', h('label.field', h('span', 'Начало проверенного участка'), spanStart), h('label.field', h('span', 'Конец'), spanEnd));

  const gross = criterion && criterion.gross_error && typeof criterion.gross_error_when !== 'boolean' ? h('input', { type: 'checkbox', checked: Boolean(current && current.gross_error) }) : null;
  const grossLabel = gross ? h('label.radio', gross, ' Грубая ошибка (только для применимого ответа)') : null;

  if (criterion) {
    editor.append(...[
      h('div.field', h('span', `Ответ (${criterionTypeLabel(criterion)}${allowNA ? ', N/A разрешён' : ', N/A не разрешён'})`), h('div.row', valueOpts)),
      criterion.na_condition ? h('p.muted', 'N/A только если: ', criterion.na_condition) : null,
      typeof criterion.gross_error_when === 'boolean' ? h('p.muted', 'Флаг грубой ошибки определяется ответом: Да — выявлена, Нет — не выявлена.') : null,
      naField,
      h('label.field', h('span', 'Пояснение (обязательно)'), explanation),
      h('div.field', h('span', 'Доказательства — сообщения транскрипта'), h('div.ev-list', evBoxes)),
      missedLabel,
      spanRow,
      grossLabel].filter(Boolean)); // native append() would print null as text
  }

  const selectedValue = () => {
    const r = editor.querySelector(`input[name="${valueName}"]:checked`);
    return r ? r.value : null;
  };
  // A negative gross-error finding describes the inspected conversation, not a
  // missed operator action. Preserve its span without inventing a quotation.
  const isNegativeGross = () => criterion && typeof criterion.gross_error_when === 'boolean'
    && ['true', 'false'].includes(selectedValue())
    && (selectedValue() === 'true') !== criterion.gross_error_when;
  let wasNegativeGross = isNegativeGross();
  const syncEditor = () => {
    const v = selectedValue();
    const na = v === 'na';
    naField.hidden = !na;
    if (numInput) numInput.disabled = v !== 'num';
    const negativeGross = isNegativeGross();
    if (negativeGross && !wasNegativeGross) {
      // Quotes supporting the previous opposite verdict are not evidence for
      // absence. A fresh negative verdict must cover the whole conversation.
      for (const box of evBoxes) box.querySelector('input').checked = false;
      missed.checked = false;
      spanStart.value = tr[0]?.id || '';
      spanEnd.value = tr.at(-1)?.id || '';
    }
    wasNegativeGross = negativeGross;
    missedLabel.hidden = negativeGross;
    spanRow.hidden = !(missed.checked || negativeGross);
    if (gross) { gross.disabled = na; if (na) gross.checked = false; }
  };
  editor.addEventListener('change', syncEditor);
  syncEditor();

  const submit = h('button.btn.btn-primary', { type: 'submit' }, 'Сохранить решение');
  const form = h('form.review-form', { novalidate: true }, decisionGroup,
    h('label.field', h('span', 'Причина решения (обязательно)'), reason),
    editor, err, h('div.row', submit));
  decisionGroup.addEventListener('change', () => {
    editor.hidden = form.querySelector(`input[name="decision-${n}"]:checked`).value !== 'change';
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const decision = form.querySelector(`input[name="decision-${n}"]:checked`).value;
    const errors = [];
    const r = reason.value.trim();
    if (!r) errors.push('Причина решения обязательна.');
    let body;
    if (decision === 'confirm') {
      body = { decision: 'confirm', reason: r };
    } else {
      const built = buildAnswer();
      errors.push(...built.errors);
      body = { decision: 'change', reason: r, answer: built.answer };
    }
    if (errors.length) {
      mount(err, h('div.alert.alert-error', { role: 'alert' }, h('strong', 'Проверьте форму:'), h('ul', errors.map((x) => h('li', x)))));
      return;
    }
    submit.disabled = true;
    mount(err, h('div.loading', 'Сохранение решения…'));
    try {
      const res = await api.reviewAppeal(appeal.id, body);
      onSaved(res);
    } catch (ex) {
      mount(err, errorBox(ex, ex.status === 409 ? 'Решение уже сохранено' : 'Решение не сохранено'));
      submit.disabled = false;
    }
  });

  function buildAnswer() {
    const errors = [];
    if (!criterion) return { errors: ['Критерий не найден в чек-листе.'], answer: null };
    const v = selectedValue();
    let value;
    if (!v) errors.push('Выберите ответ.');
    else if (v === 'na') value = null;
    else if (v === 'true') value = true;
    else if (v === 'false') value = false;
    else if (v === 'num') {
      const raw = numInput.value.trim();
      value = raw === '' ? NaN : Number(raw);
      if (!Number.isFinite(value)) errors.push('Введите числовое значение.');
      else if (criterion.allowed_values && !criterion.allowed_values.includes(value)) errors.push('Выберите допустимую ступень шкалы.');
      else if (value < criterion.min || value > criterion.max) errors.push(`Значение должно быть в диапазоне ${criterion.min}–${criterion.max}.`);
    }
    const isNA = v === 'na';
    if (isNA && !allowNA) errors.push('N/A не разрешён для этого критерия.');
    const expl = explanation.value.trim();
    if (!expl) errors.push('Пояснение обязательно.');
    const evidence = evBoxes.map((l) => l.querySelector('input')).filter((c) => c.checked).map((c) => c.value);
    const answer = { criterion_id: criterion.id, answer: value, explanation: expl, evidence_message_ids: evidence };
    if (isNA) {
      const nr = naReason.value.trim();
      if (!nr) errors.push('Для N/A нужна причина.');
      answer.na_reason = nr;
    } else {
      const negativeGross = isNegativeGross();
      if (missed.checked || negativeGross) {
        const ids = tr.map((m) => m.id);
        const start = ids.indexOf(spanStart.value), end = ids.indexOf(spanEnd.value);
        if (start < 0 || end < 0 || start > end) errors.push('Укажите действительный проверенный участок: начало должно быть не позже конца.');
        if (negativeGross && (start !== 0 || end !== ids.length - 1)) errors.push('Чтобы подтвердить отсутствие грубой ошибки, проверьте весь диалог: от первой до последней реплики.');
        answer.missed = negativeGross ? false : true;
        answer.checked_span = { start_message_id: spanStart.value, end_message_id: spanEnd.value };
      } else if (!evidence.length) {
        errors.push('Для применимого ответа укажите доказательства или отметьте «Действие пропущено» с проверенным участком.');
      }
      if (gross && gross.checked) answer.gross_error = true;
    }
    return { errors, answer };
  }

  panel.append(form);
  return panel;
}

function truncate(text, n) {
  const s = text == null ? '' : String(text);
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}
