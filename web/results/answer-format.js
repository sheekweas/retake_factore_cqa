// Shared formatting for criterion answers, scores and evidence. Used by results card and appeals.
// All model output is rendered as text nodes via h(); never innerHTML.
import { h, fmtScore, fmtDate, fmtMsgTime, provenanceBadge, ROLE_LABELS } from '../app/dom.js';
import { href } from '../app/router.js';

export const NULL_TOTAL_LABEL = '— (все критерии N/A)';

/** Map criterion_id -> checklist criterion definition. */
export function criterionIndex(checklist) {
  const map = new Map();
  for (const c of (checklist && checklist.criteria) || []) map.set(c.id, c);
  return map;
}

/** Human-readable answer value. criterion may be undefined (falls back to raw value). */
export function formatAnswerValue(answer, criterion) {
  if (answer === null || answer === undefined) return 'N/A';
  if (typeof answer === 'boolean') return answer ? 'Да' : 'Нет';
  if (typeof answer === 'number') {
    if (criterion && criterion.type === 'numeric') return `${answer} (шкала ${criterion.min}–${criterion.max})`;
    return String(answer);
  }
  return String(answer);
}

export function formatTotal(total) {
  return total === null || total === undefined ? NULL_TOTAL_LABEL : fmtScore(total, 2);
}

export function criterionTypeLabel(c) {
  if (!c) return '';
  return c.type === 'numeric' ? `числовой ${c.min}–${c.max}` : 'да/нет';
}

/** Compare two answer objects on the fields that matter for the score and evidence. */
export function answersEqual(a, b) {
  if (!a || !b) return a === b;
  const norm = (x) => JSON.stringify({
    answer: x.answer === undefined ? null : x.answer,
    evidence: x.evidence_message_ids || [],
    missed: Boolean(x.missed),
    span: x.checked_span ? [x.checked_span.start_message_id, x.checked_span.end_message_id] : null,
    na: x.na_reason || null,
    gross: Boolean(x.gross_error),
    expl: x.explanation || '',
  });
  return norm(a) === norm(b);
}

/** IDs of messages from start..end inclusive (in transcript order). Empty if invalid. */
export function spanIds(transcript, span) {
  if (!span) return [];
  const ids = (transcript || []).map((m) => m.id);
  const s = ids.indexOf(span.start_message_id);
  const e = ids.indexOf(span.end_message_id);
  if (s < 0 || e < 0 || e < s) return [];
  return ids.slice(s, e + 1);
}

export function shortMsgId(id) {
  const s = String(id);
  const i = s.lastIndexOf(':');
  return i >= 0 ? '#' + s.slice(i + 1) : s;
}

/** Compact answer text for pills: "15 / 20", "Да", "Нет", "N/A". */
export function formatAnswerShort(answer, criterion) {
  if (answer === null || answer === undefined) return 'N/A';
  if (typeof answer === 'boolean') return answer ? 'Да' : 'Нет';
  if (typeof answer === 'number' && criterion && criterion.type === 'numeric') return `${answer} / ${criterion.max}`;
  return String(answer);
}

/** Tone of an answer: ok | warn | err | na | neutral. Derived from the answer itself, never invented. */
export function answerTone(ans, criterion) {
  if (!ans) return 'neutral';
  const v = ans.answer;
  if (v === null || v === undefined) return 'na';
  if (criterion && typeof criterion.gross_error_when === 'boolean') return v === criterion.gross_error_when ? 'err' : 'ok';
  if (ans.gross_error) return 'err';
  if (criterion && Number(criterion.weight) === 0) return 'neutral';
  let n = typeof ans.normalized_score === 'number' ? ans.normalized_score : null;
  if (n === null && typeof v === 'boolean') n = v ? 1 : 0;
  if (n === null && typeof v === 'number' && criterion && criterion.type === 'numeric' && criterion.max > criterion.min) {
    n = (v - criterion.min) / (criterion.max - criterion.min);
  }
  if (n === null) return 'neutral';
  return n >= 1 ? 'ok' : n <= 0 ? 'err' : 'warn';
}

/** Answer pill element (text only). extra: additional class suffix like '.pill-lg'. */
export function answerPill(ans, criterion, extra = '') {
  const v = ans ? ans.answer : undefined;
  return h('span.score-pill.pill-' + answerTone(ans, criterion) + extra,
    { title: formatAnswerValue(v, criterion) }, formatAnswerShort(v, criterion));
}

/**
 * Detailed answer block: value, explanation, evidence quotes, missed span, N/A reason, gross error.
 * opts.criterion: checklist criterion; opts.conversation: source transcript;
 * opts.onEvidence(id); opts.onSpan(span); opts.label;
 * opts.showValue (default true): value line with pill; opts.maxExplanation: truncate the rationale.
 */
export function answerDetails(ans, opts = {}) {
  const { criterion, conversation, onEvidence, onSpan, label, showValue = true, maxExplanation } = opts;
  if (!ans) return h('div.muted', 'нет ответа');
  const isNA = ans.answer === null || ans.answer === undefined;
  const parts = [];
  if (showValue) {
    parts.push(h('div.ans-line',
      label ? h('span.muted', label + ':') : null,
      h('strong.ans-value.score-pill.pill-' + answerTone(ans, criterion) + (isNA ? '.ans-na' : ''),
        { title: formatAnswerValue(ans.answer, criterion) }, formatAnswerShort(ans.answer, criterion)),
      ans.gross_error ? h('span.badge.badge-gross', 'грубая ошибка') : null,
    ));
  }
  if (ans.explanation) {
    const full = String(ans.explanation);
    const short = maxExplanation && full.length > maxExplanation ? full.slice(0, maxExplanation).replace(/\s+\S*$/, '') + '…' : full;
    parts.push(h('div.ans-expl', { title: short !== full ? full : undefined }, h('span.muted', 'Пояснение: '), short));
  }
  if (isNA && ans.na_reason) parts.push(h('div.ans-na-reason', h('span.muted', 'Причина N/A: '), ans.na_reason));
  const ev = ans.evidence_message_ids || [];
  const messages = new Map((conversation?.transcript || []).map((message) => [message.id, message]));
  if (ev.length) {
    parts.push(h('div.ans-evidence', h('div.ans-sub.muted', ev.length > 1 ? `Доказательства (${ev.length})` : 'Доказательство'),
      ev.map((id) => h('div.evidence-item',
        onEvidence
          ? h('button.chip.chip-evidence', { type: 'button', title: 'Показать сообщение ' + id, 'aria-label': 'Показать сообщение ' + shortMsgId(id) + ' в переписке', on: { click: () => onEvidence(id) } }, shortMsgId(id))
          : h('span.chip.chip-evidence', { title: id }, shortMsgId(id)),
        evidenceFragment(messages.get(id), id, Boolean(conversation), onEvidence)))));
  } else if (!isNA && !ans.missed && !ans.checked_span) {
    parts.push(h('div.muted.small', 'Доказательства не указаны'));
  }
  if (ans.missed || ans.checked_span) {
    const sp = ans.checked_span;
    const text = sp ? `проверенный участок: ${shortMsgId(sp.start_message_id)} … ${shortMsgId(sp.end_message_id)}` : 'проверенный участок не указан';
    parts.push(h('div.ans-missed',
      ans.missed ? h('span.badge.badge-warn', 'действие пропущено') : null,
      sp && onSpan
        ? h('button.btn-link', { type: 'button', title: `${sp.start_message_id} … ${sp.end_message_id}`, on: { click: () => onSpan(sp) } }, text)
        : h('span.muted', text)));
    // Endpoint quotes only when there is no direct evidence; otherwise they repeat the evidence quote.
    if (sp && conversation && (ans.missed || !ev.length)) {
      parts.push(h('div.checked-span-fragments',
        [...new Set([sp.start_message_id, sp.end_message_id])].map((id) => evidenceFragment(messages.get(id), id, true))));
    }
  }
  return h('div.ans-details', parts);
}

/** Resolve evidence from the original message, never from model-authored quotations. */
function evidenceFragment(message, id, hasConversation, onJump) {
  if (!message) return hasConversation ? h('div.muted.small', 'Исходный фрагмент не найден') : null;
  const source = String(message.text || '');
  const excerpt = source.length > 240 ? source.slice(0, 240) + '…' : source;
  const role = ROLE_LABELS[message.speaker] || message.speaker || 'роль не указана';
  return h('blockquote.evidence-fragment' + (onJump ? '.is-jump' : ''), {
    'data-msg-id': id, 'data-speaker': message.speaker,
    on: onJump ? { click: (e) => { if (!window.getSelection || !String(window.getSelection())) onJump(id); e.stopPropagation(); } } : undefined,
  },
    h('div.evidence-meta.small.muted',
      message.timestamp
        ? h('span.evidence-timestamp', { title: fmtMsgTime(message.timestamp).title, 'data-source-ts': String(message.timestamp) }, fmtMsgTime(message.timestamp).text)
        : h('span.evidence-timestamp', 'время не указано'), ' · ',
      h('span.evidence-role', { 'data-speaker': message.speaker }, role)),
    h('div.evidence-text', excerpt));
}

const STATUS_LABELS = { pending: 'на рассмотрении', reviewed: 'решение принято' };
const DECISION_LABELS = { confirm: 'подтверждено', change: 'изменено' };

export function appealStatusChip(appeal) {
  const cls = appeal.status === 'pending' ? 'badge-warn' : 'badge-ok';
  return h('span.badge.' + cls, STATUS_LABELS[appeal.status] || appeal.status);
}

export function decisionLabel(decision) {
  return DECISION_LABELS[decision] || decision || '';
}

export function appealLink(appeal, text) {
  return h('a', { href: href('/appeals/' + encodeURIComponent(appeal.id)) }, text || 'апелляция');
}

export function checklistLabel(cl) {
  if (!cl) return 'чек-лист не найден';
  return `${cl.name} · v${cl.version}`;
}

export function checklistHeader(cl) {
  return h('span.checklist-label', checklistLabel(cl), ' ', provenanceBadge(cl && cl.provenance));
}

export { fmtDate };

/** Only http(s) URLs are allowed as links (audio_url is untrusted data). */
export function safeHttpUrl(url) {
  if (typeof url !== 'string' || !url) return null;
  try {
    const u = new URL(url, location.href);
    return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
  } catch { return null; }
}

export function fmtOffset(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (n) => String(n).padStart(2, '0');
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}
