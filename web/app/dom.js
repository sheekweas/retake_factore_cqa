// DOM helpers. All untrusted text (transcripts, model output) goes through text nodes, never innerHTML.

/**
 * h('div.cls#id', {attrs, on: {click}}, ...children)
 * children: string | number | Node | null | false | arrays thereof.
 * attrs: plain attributes; `class`, `dataset`, `style` (object), boolean props (`disabled`, `checked`, `hidden`, `selected`), `value`.
 */
export function h(tag, attrs, ...children) {
  if (attrs === null || typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs)) {
    if (attrs !== undefined) children.unshift(attrs);
    attrs = {};
  }
  const [, name = 'div', rest = ''] = tag.match(/^([a-z0-9-]*)(.*)$/i);
  const el = document.createElement(name || 'div');
  for (const part of rest.match(/[.#][^.#]+/g) || []) {
    if (part[0] === '.') el.classList.add(part.slice(1));
    else el.id = part.slice(1);
  }
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === 'class') String(v).split(/\s+/).filter(Boolean).forEach((c) => el.classList.add(c));
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (['disabled', 'checked', 'hidden', 'selected', 'required', 'multiple', 'readOnly'].includes(k)) el[k] = Boolean(v);
    else if (k === 'value') el.value = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false || c === true) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function mount(container, ...children) {
  clear(container);
  append(container, children);
  return container;
}

/** Inline error block that always shows the server message as text. */
export function errorBox(err, title = 'Ошибка') {
  const msg = err && err.message ? err.message : String(err);
  const code = err && err.code ? ` (${err.code}${err.status ? ', HTTP ' + err.status : ''})` : '';
  return h('div.alert.alert-error', { role: 'alert' }, h('strong', title + ': '), msg, h('span.muted', code));
}

export function loading(text = 'Загрузка…') {
  return h('div.loading', { role: 'status', 'aria-live': 'polite' }, text);
}

let toastHost;
export function toast(message, kind = 'info') {
  if (!toastHost) {
    toastHost = h('div.toasts', { 'aria-live': 'polite', role: 'status' });
    document.body.append(toastHost);
  }
  const t = h('div.toast.toast-' + kind, message);
  toastHost.append(t);
  setTimeout(() => t.remove(), 5000);
}

/** Null means "not applicable / no total", never zero. Optional precision is presentation only. */
export function fmtScore(v, decimals) {
  if (v === null || v === undefined) return '—';
  return typeof v === 'number' && decimals !== undefined ? v.toFixed(decimals) : String(v);
}

export function fmtPct(v) {
  return v === null || v === undefined ? '—' : (Math.round(v * 1000) / 10).toString() + '%';
}

export function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

const ISO_TS = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?(Z|[+-]\d{2}:?\d{2})?$/;

/**
 * Compact display of a source message timestamp WITHOUT timezone conversion.
 * Returns { text, date, title }: text = clock time as written in the source (HH:MM[:SS]),
 * date = DD.MM.YYYY when the source contains a date (else ''), title = the verbatim source value.
 * Non-ISO values (e.g. "00:07") are returned unchanged.
 */
export function fmtMsgTime(raw) {
  if (raw === undefined || raw === null || raw === '') return { text: '', date: '', title: '' };
  const s = String(raw).trim();
  const m = ISO_TS.exec(s);
  if (!m) return { text: s, date: '', title: s };
  const [, y, mo, d, hh, mi, ss] = m;
  return { text: `${hh}:${mi}${ss ? ':' + ss : ''}`, date: `${d}.${mo}.${y}`, title: `Исходная метка: ${s}` };
}

/** Provenance badge: everything not explicitly "official" is visibly marked. */
export function provenanceBadge(p) {
  const map = {
    official: ['badge-official', 'официальный чек-лист'],
    demonstration: ['badge-demo', 'демонстрационный пример, не официальный чек-лист'],
    custom: ['badge-custom', 'пользовательский'],
  };
  const [cls, label] = map[p] || ['badge-custom', p || 'не указано'];
  return h('span.badge.' + cls, label);
}

export const ROLE_LABELS = { operator: 'Оператор', client: 'Клиент', bot: 'Бот', system: 'Система' };
export const LANG_ATTR = { RU: 'ru', KZ: 'kk', EN: 'en' };
