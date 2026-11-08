// Route #/conversations/:id?eval=<evaluationId>&msg=<messageId>
import { api } from '../app/api.js';
import { h, mount, errorBox, loading } from '../app/dom.js';
import { href, setQuery } from '../app/router.js';
import { createTranscript } from './transcript.js';
import { renderEvaluationPanel } from './evaluation-panel.js';

const settle = (p) => p.then((v) => v, (e) => (e instanceof Error ? e : new Error(String(e))));

export async function render(container, { params, query }) {
  const cid = params.id;
  let alive = true;
  // The router only receives our cleanup after this async render resolves, so a navigation
  // during loading cannot flip `alive`; a detached container means the page was replaced.
  const isAlive = () => alive && container.isConnected;
  mount(container, loading('Загрузка диалога…'));

  let conversation;
  try {
    conversation = await api.conversation(cid);
  } catch (e) {
    if (!isAlive()) return;
    mount(container,
      h('p', h('a', { href: href('/conversations') }, '← К списку диалогов')),
      errorBox(e, e.status === 404 ? 'Диалог не найден' : 'Не удалось загрузить диалог'));
    return;
  }
  if (!isAlive()) return;

  const transcript = createTranscript(conversation);
  const pauseAudio = () => { for (const a of transcript.el.querySelectorAll('audio')) { try { a.pause(); } catch { /* ignore */ } } };

  // Evidence/span clicks and ?msg= deep links also cue the recording to the message offset
  // (no autoplay). A cue requested before the audio is resolved is applied once it is attached.
  let cueId = query.msg || null;
  const cue = (id) => { if (!id) return; cueId = id; transcript.cueMessage(id); };
  const linkedTranscript = {
    ...transcript,
    focusMessage(id, opts) {
      const found = transcript.focusMessage(id, opts);
      if (found) cue(id);
      return found;
    },
    highlightSpan(span) {
      transcript.highlightSpan(span);
      if (span && span.start_message_id) cue(span.start_message_id);
    },
  };

  const audioReady = conversation.audio_url
    ? api.audio(cid)
      .catch((e) => ({ status: 'error', message: e.message }))
      .then((res) => {
        if (!isAlive()) return;
        transcript.attachAudio(res);
        if (cueId) transcript.cueMessage(cueId);
      })
    : Promise.resolve();
  const right = h('div.card-right', loading('Загрузка оценок…'));
  mount(container,
    h('div.card-top',
      h('a.back-link', { href: href('/conversations') }, '← Диалоги')),
    h('div.page-head.card-head',
      h('h1', 'Диалог ', h('span.card-id', conversation.conversation_id)),
      conversation.label ? h('span.badge.badge-demo', conversation.label) : null),
    h('div.card-grid', h('div.card-left', transcript.el), right));

  const checklists = new Map();
  let allChecklists = null;
  let evaluations = [];
  let selectedId = query.eval || null;

  async function loadChecklist(id) {
    if (!checklists.has(id)) checklists.set(id, await settle(api.checklist(id)));
    return checklists.get(id);
  }

  async function load() {
    const [evs, all] = await Promise.all([settle(api.evaluations(cid)), allChecklists && !(allChecklists instanceof Error) ? allChecklists : settle(api.checklists())]);
    allChecklists = all;
    if (evs instanceof Error) throw evs;
    evaluations = [...evs].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    await Promise.all([...new Set(evaluations.map((e) => e.checklist_id))].map(loadChecklist));
  }

  async function renderRight() {
    const selected = selectedId ? evaluations.find((e) => e.id === selectedId) || null : evaluations[0] || null;
    let appeals = [];
    if (selected) appeals = await settle(api.appeals(selected.id));
    if (!isAlive()) return;
    mount(right, renderEvaluationPanel({
      conversation, evaluations, selected, checklists, allChecklists, appeals, transcript: linkedTranscript,
      onSelect: (id) => { selectedId = id; setQuery({ eval: id, msg: '' }); transcript.clearHighlight(); renderRight(); },
      onReload: async () => { await load(); await renderRight(); },
    }));
  }

  try {
    await load();
    await renderRight();
  } catch (e) {
    if (isAlive()) mount(right, errorBox(e, 'Не удалось загрузить оценки'));
  }

  if (!isAlive()) { pauseAudio(); return; }
  // Audio cue for ?msg= is applied by the audioReady chain above (cueId starts as query.msg).
  if (query.msg) {
    requestAnimationFrame(() => { if (isAlive()) transcript.focusMessage(query.msg, { updateQuery: false }); });
  }

  // A detached <audio> keeps playing, so stop it when the route changes.
  return () => { alive = false; pauseAudio(); };
}
