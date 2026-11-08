// Transcript column: messages with roles, verbatim timestamps, language attribute, recording panel.
// Optional second view: automatic recording transcription (audio offsets, no speaker roles).
import { h, ROLE_LABELS, LANG_ATTR, toast, fmtDate, fmtMsgTime } from '../app/dom.js';
import { setQuery } from '../app/router.js';
import { safeHttpUrl, fmtOffset, spanIds, shortMsgId } from './answer-format.js';

/** Compact clock as written in the source; verbatim value in title and data-source-ts. */
function msgTime(raw) {
  const t = fmtMsgTime(raw);
  const src = raw === undefined || raw === null ? '' : String(raw);
  return h('span.msg-ts', { title: t.title || null, dataset: src ? { sourceTs: src } : {} }, t.text);
}

/**
 * Build transcript view. Returns { el, focusMessage(id, opts), highlightSpan(span), clearHighlight(),
 *   attachAudio(resolution), cueMessage(id) }.
 */
export function createTranscript(conversation) {
  const transcript = conversation.transcript || [];
  const lang = LANG_ATTR[conversation.language] || undefined;
  const itemsById = new Map();

  // --- recording panel ---
  // audio_url is usually a player page (HTML), not media. The card resolves it server-side
  // (GET /api/conversations/:id/audio) and calls attachAudio(); only then an <audio> is shown.
  const audioUrl = safeHttpUrl(conversation.audio_url);
  let audio = null;
  let audioState = audioUrl ? 'pending' : 'none'; // pending | playable | failed | page_only | none
  let pendingSeek = null;
  const audioSlot = h('div.audio-slot');
  const audioNote = h('div.muted.audio-note', { role: 'status', 'aria-live': 'polite' }, audioUrl ? 'Поиск аудиофайла для встроенного проигрывателя…' : '');
  const offsetNote = h('div.audio-offset-note', { hidden: true, role: 'status', 'aria-live': 'polite' });
  let recordingPanel = null;
  const hasOffsets = transcript.some((m) => typeof m.audio_offset_seconds === 'number');
  const originalLink = audioUrl
    ? h('a.btn.btn-small', { href: audioUrl, target: '_blank', rel: 'noopener noreferrer' }, 'Открыть оригинал записи ↗')
    : null;

  function markFailed(message) {
    audioState = 'failed';
    if (audio) audio.hidden = true;
    audioNote.hidden = false;
    audioNote.textContent = message || 'Встроенное воспроизведение недоступно — используйте ссылку на оригинал записи.';
  }

  // Applies to the original messages only; hidden while the recording transcription is shown.
  const computedNote = hasOffsets ? h('div.muted.small.offset-disclaimer', 'Смещения ▶ у реплик вычислены и не сверены с записью.') : null;
  if (audioUrl) {
    recordingPanel = h('div.recording-panel',
      h('div.row',
        h('strong', 'Запись разговора'),
        originalLink),
      audioSlot, audioNote, offsetNote, computedNote);
  } else if (conversation.audio_url) {
    recordingPanel = h('div.recording-panel.muted', 'Ссылка на запись имеет недопустимый формат (разрешены только http/https) и не показана.');
  } else if (conversation.channel === 'voice call') {
    recordingPanel = h('div.recording-panel.muted', 'Ссылка на запись звонка не предоставлена в данных.');
  }

  /** Absolute http(s) only; relative or other schemes are refused. */
  function absoluteHttpUrl(url) {
    if (typeof url !== 'string' || !url) return null;
    try {
      const u = new URL(url);
      return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null;
    } catch { return null; }
  }

  let playingLi = null;
  function markPlaying() {
    if (!audio) return;
    const t = audio.currentTime;
    let li = null;
    if (view === 'asr') {
      let best = -1; // robust to out-of-order segments: latest start not after t
      for (const s of segments) if (s.start <= t + 0.25 && s.start >= best) { best = s.start; li = s.li; }
    } else {
      let current = null;
      for (const m of transcript) {
        if (typeof m.audio_offset_seconds !== 'number') continue;
        if (m.audio_offset_seconds <= t + 0.25) current = m; else break;
      }
      li = current ? itemsById.get(current.id) : null;
    }
    if (li === playingLi) return;
    if (playingLi) playingLi.classList.remove('is-playing');
    playingLi = li;
    if (li && !audio.paused) li.classList.add('is-playing');
  }

  /** Called by the card with the server resolution {status, media_url, media_type, page_url, message}. */
  function attachAudio(res) {
    if (!audioUrl || audioState !== 'pending') return;
    const page = absoluteHttpUrl(res && res.page_url);
    if (page && originalLink) originalLink.href = page;
    const media = res && res.status === 'playable' ? absoluteHttpUrl(res.media_url) : null;
    if (!media) {
      audioState = res && res.status === 'page_only' ? 'page_only' : 'failed';
      const why = res && typeof res.message === 'string' && res.message ? res.message : 'Аудиофайл не найден.';
      audioNote.textContent = 'Встроенный проигрыватель недоступен: ' + why;
      if (pendingSeek !== null) { const s = pendingSeek; pendingSeek = null; openOriginal(s.sec, s.exact); }
      return;
    }
    audio = h('audio', { controls: true, preload: 'metadata', src: media });
    audio.addEventListener('error', () => markFailed('Аудиофайл не удалось загрузить (ссылка могла устареть) — откройте оригинал записи.'));
    audio.addEventListener('timeupdate', markPlaying);
    audio.addEventListener('play', markPlaying);
    audio.addEventListener('pause', () => { if (playingLi) playingLi.classList.remove('is-playing'); playingLi = null; });
    audioSlot.append(audio);
    audioState = 'playable';
    audioNote.textContent = 'Нажмите на время, чтобы перейти к позиции в записи.';
    if (pendingSeek !== null) { const s = pendingSeek; pendingSeek = null; seek(s.sec, s.exact); }
  }

  function setTime(sec) {
    const apply = () => { try { audio.currentTime = sec; } catch { /* not seekable yet */ } };
    if (audio.readyState >= 1) apply();
    else audio.addEventListener('loadedmetadata', apply, { once: true });
  }

  /** Position the player at a message without autoplay (used by ?msg= deep links). */
  function cueMessage(id) {
    showOriginal();
    const m = transcript.find((x) => x.id === id);
    if (!m || typeof m.audio_offset_seconds !== 'number') return;
    if (audio && audioState === 'playable') setTime(m.audio_offset_seconds);
  }

  /** exact=true: an actual audio offset (recording transcription), not a computed message offset. */
  function openOriginal(sec, exact = false) {
    const target = (originalLink && originalLink.href) || audioUrl;
    window.open(target, '_blank', 'noopener,noreferrer');
    offsetNote.hidden = false;
    offsetNote.textContent = `Запись открыта в новой вкладке. Перейдите к позиции ${fmtOffset(sec)}`
      + (exact ? '.' : ' (смещение вычислено, не сверено).');
    toast(`Перейдите к позиции ${fmtOffset(sec)} в записи`);
  }

  function seek(sec, exact = false) {
    if (!audioUrl) return;
    if (audioState === 'pending') {
      pendingSeek = { sec, exact };
      offsetNote.hidden = false;
      offsetNote.textContent = `Подготовка записи… воспроизведение начнётся с ${fmtOffset(sec)}.`;
      return;
    }
    if (audio && audioState === 'playable') {
      setTime(sec);
      const p = audio.play();
      if (p && typeof p.then === 'function') {
        p.then(() => { if (Math.abs(audio.currentTime - sec) > 1) setTime(sec); offsetNote.hidden = true; })
          .catch(() => {
            if (audio.error) markFailed();
            offsetNote.hidden = false;
            offsetNote.textContent = `Не удалось начать воспроизведение. Откройте оригинал записи и перейдите к ${fmtOffset(sec)}.`;
          });
      }
      return;
    }
    openOriginal(sec, exact);
  }

  // --- messages ---
  const list = h('ol.transcript-list', { 'aria-label': 'Сообщения диалога' });
  for (const m of transcript) {
    const speaker = ROLE_LABELS[m.speaker] ? m.speaker : 'system';
    const isContext = m.speaker !== 'operator';
    const li = h('li.msg.msg-' + speaker + (isContext ? '.msg-context' : '.msg-operator'), {
      id: 'msg-' + m.id, tabindex: '-1', dataset: { msgId: m.id },
    },
    h('div.msg-head',
      h('span.role.role-' + speaker, ROLE_LABELS[m.speaker] || String(m.speaker || 'неизвестно')),
      msgTime(m.timestamp),
      audioUrl && typeof m.audio_offset_seconds === 'number'
        ? h('button.btn.btn-small.msg-play', {
          type: 'button', title: 'Перейти к позиции в записи (смещение вычислено, не сверено)',
          on: { click: () => seek(m.audio_offset_seconds) },
        }, '▶ ' + fmtOffset(m.audio_offset_seconds))
        : null,
      h('span.msg-id.muted', { title: m.id }, shortMsgId(m.id))),
    h('div.msg-text', { lang }, m.text === undefined || m.text === null ? '' : String(m.text)));
    itemsById.set(m.id, li);
    list.append(li);
  }

  const scroller = h('div.transcript-scroll', list);
  const legend = h('div.transcript-legend',
    h('span.legend-item.legend-op', 'Оператор — оценивается'),
    h('span.legend-item.legend-ctx', 'Клиент, бот, система — контекст'));

  // --- recording transcription (automatic, audio-based, no speaker roles) ---
  // Evaluation evidence is bound to the original messages, so every evidence action below
  // switches back to the original view first.
  const rt = conversation.recording_transcription;
  const segments = [];
  if (rt && rt.status === 'ready' && Array.isArray(rt.segments)) {
    const used = new Set();
    rt.segments.forEach((s, i) => {
      if (!s || typeof s !== 'object' || typeof s.text !== 'string') return;
      const start = typeof s.start === 'number' ? s.start : NaN;
      if (!Number.isFinite(start) || start < 0) return;
      const end = typeof s.end === 'number' && Number.isFinite(s.end) ? s.end : null;
      const rawId = typeof s.id === 'string' || typeof s.id === 'number' ? String(s.id) : String(i);
      let domId = 'asr-' + rawId.replace(/[^A-Za-z0-9_-]/g, '_');
      if (used.has(domId)) domId += '-' + i;
      used.add(domId);
      const segmentLanguage = typeof s.language === 'string' && /^(ru|kk|en)$/.test(s.language) ? s.language : null;
      segments.push({ start, end, text: s.text, rawId, domId, language: segmentLanguage });
    });
  }
  const hasAsr = segments.length > 0;
  let view = hasAsr ? 'asr' : 'original';
  let asrPane = null, originalPane = null, tabs = null;

  if (hasAsr) {
    const asrLang = typeof rt.language === 'string' && /^[a-z]{2,3}$/i.test(rt.language)
      ? LANG_ATTR[rt.language.toUpperCase()] || rt.language.toLowerCase() : lang;
    const asrList = h('ol.asr-list', { 'aria-label': 'Сегменты расшифровки записи' });
    for (const s of segments) {
      const range = fmtOffset(s.start) + (s.end !== null ? '–' + fmtOffset(s.end) : '');
      const timeLabel = audioUrl
        ? h('button.btn.btn-small.asr-seek', {
          type: 'button', title: `Перейти к ${range} в записи (${s.start} с)`,
          'aria-label': 'Перейти к позиции ' + fmtOffset(s.start) + ' в записи',
          on: { click: () => seek(s.start, true) },
        }, fmtOffset(s.start))
        : h('span.asr-seek.asr-time', { title: `${range} (${s.start} с)` }, fmtOffset(s.start));
      s.li = h('li.asr-seg', { id: s.domId, dataset: { segId: s.rawId } },
        timeLabel, h('div.asr-text', { lang: s.language || asrLang }, s.text));
      asrList.append(s.li);
    }
    const reviewed = rt.reviewed === true;
    const model = typeof rt.model === 'string' && rt.model ? rt.model : null;
    const created = typeof rt.created_at === 'string' && rt.created_at ? rt.created_at : null;
    const meta = [
      model ? h('span.asr-model', { title: model }, model) : null,
      h('span.asr-review', reviewed ? 'проверена' : 'не проверена человеком'),
      created ? h('span.asr-date', { title: created }, fmtDate(created)) : null,
    ].filter(Boolean).flatMap((x, i) => (i ? [' · ', x] : [x]));
    asrPane = h('div.transcript-view.asr-view', { id: 'tv-asr', role: 'tabpanel', 'aria-labelledby': 'tv-tab-asr' },
      h('div.asr-head',
        h('span.badge.badge-asr', 'Автоматическая расшифровка'),
        h('span.muted.small.asr-meta', meta),
        h('span.muted.small', 'Оценки привязаны к исходным репликам с ролями.')),
      h('div.transcript-scroll.asr-scroll', asrList));
    originalPane = h('div.transcript-view.original-view', { id: 'tv-original', role: 'tabpanel', 'aria-labelledby': 'tv-tab-original' },
      legend, scroller);
    const tab = (name, label) => h('button.transcript-view-tab', {
      type: 'button', role: 'tab', id: 'tv-tab-' + name, 'aria-controls': 'tv-' + name, dataset: { view: name },
      on: { click: () => setView(name), keydown: onTabKey },
    }, label);
    tabs = h('div.transcript-views', { role: 'tablist', 'aria-label': 'Вид текста' },
      tab('asr', 'По записи'), tab('original', 'Исходные реплики'));
  }

  function onTabKey(e) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Home' && e.key !== 'End') return;
    e.preventDefault();
    const next = view === 'asr' ? 'original' : 'asr';
    setView(e.key === 'Home' ? 'asr' : e.key === 'End' ? 'original' : next);
    tabs.querySelector(`[data-view="${view}"]`).focus();
  }

  function setView(name) {
    if (!hasAsr) return;
    view = name === 'original' ? 'original' : 'asr';
    for (const b of tabs.children) {
      const on = b.dataset.view === view;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    asrPane.hidden = view !== 'asr';
    originalPane.hidden = view !== 'original';
    if (computedNote) computedNote.hidden = view !== 'original';
    if (playingLi) { playingLi.classList.remove('is-playing'); playingLi = null; }
    markPlaying();
  }
  if (hasAsr) setView(view);

  const el = h('section.panel.transcript-panel', { 'aria-label': 'Транскрипт' },
    h('div.transcript-head',
      h('h2', 'Переписка'),
      h('dl.transcript-meta',
        h('div', h('dt', 'Язык'), h('dd', conversation.language || '—')),
        h('div', h('dt', 'Канал'), h('dd', conversation.channel || '—')),
        h('div', h('dt', 'Группа'), h('dd', conversation.dataset_group_id || '—')),
        h('div', h('dt', 'Сообщений'), h('dd', String(transcript.length))))),
    recordingPanel,
    ...(hasAsr ? [tabs, asrPane, originalPane] : [legend, scroller]));

  /** Evidence is bound to the original transcript: make it visible before acting on it. */
  function showOriginal() { if (view !== 'original') setView('original'); }

  function clearHighlight() {
    showOriginal();
    for (const li of list.querySelectorAll('.is-highlighted')) li.classList.remove('is-highlighted');
  }

  function flash(li) {
    li.classList.remove('flash');
    void li.offsetWidth; // restart animation
    li.classList.add('flash');
  }

  function reveal(li) {
    // Scroll inside the transcript column and bring the column itself into view.
    const top = li.offsetTop - scroller.offsetTop - 24;
    scroller.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    const r = scroller.getBoundingClientRect();
    if (r.top < 0 || r.top > window.innerHeight * 0.5) scroller.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  function focusMessage(id, { updateQuery = true, keep = false } = {}) {
    showOriginal();
    const li = itemsById.get(id);
    if (!li) { toast('Сообщение не найдено в транскрипте: ' + id, 'error'); return false; }
    if (!keep) clearHighlight();
    li.classList.add('is-highlighted');
    reveal(li);
    flash(li);
    li.focus({ preventScroll: true });
    if (updateQuery) setQuery({ msg: id });
    return true;
  }

  function highlightSpan(span) {
    showOriginal();
    const ids = spanIds(transcript, span);
    if (!ids.length) { toast('Проверенный участок не найден в транскрипте', 'error'); return; }
    clearHighlight();
    ids.forEach((id) => { const li = itemsById.get(id); li.classList.add('is-highlighted'); flash(li); });
    const first = itemsById.get(ids[0]);
    reveal(first);
    first.focus({ preventScroll: true });
    setQuery({ msg: ids[0] });
  }

  return { el, focusMessage, highlightSpan, clearHighlight, attachAudio, cueMessage };
}
