import { route, match, parseHash, navigate } from './router.js';
import { h, mount, errorBox, loading } from './dom.js';
import { api } from './api.js';

// Feature modules export: async render(container, { params, query }) -> optional cleanup function.
route('/conversations', () => import('../import/conversations.js'));
route('/import', () => import('../import/import.js'));
route('/conversations/:id', () => import('../results/card.js'));
route('/checklists', () => import('../checklists/list.js'));
route('/checklists/new', () => import('../checklists/editor.js'));
route('/checklists/:id', () => import('../checklists/editor.js'));
route('/appeals', () => import('../appeals/list.js'));
route('/appeals/:id', () => import('../appeals/review.js'));

const view = document.getElementById('view');
let cleanup = null;
let renderSeq = 0;

function highlightNav(path) {
  for (const a of document.querySelectorAll('.nav a')) {
    const target = a.getAttribute('href').slice(1);
    const active = path === target || path.startsWith(target + '/');
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
}

async function render() {
  const seq = ++renderSeq;
  const { path, query } = parseHash();
  if (!location.hash) return navigate('/conversations', null, { replace: true });
  highlightNav(path);
  if (typeof cleanup === 'function') { try { cleanup(); } catch { /* ignore */ } }
  cleanup = null;
  const m = match(path);
  if (!m) return mount(view, errorBox({ message: 'Страница не найдена: ' + path }));
  mount(view, loading());
  try {
    const mod = await m.loader();
    if (seq !== renderSeq) return;
    const container = h('div.page');
    mount(view, container);
    cleanup = await mod.render(container, { params: m.params, query });
  } catch (e) {
    if (seq !== renderSeq) return;
    console.error(e);
    mount(view, errorBox(e, 'Не удалось открыть страницу'));
  }
}

async function healthBadge() {
  const el = document.getElementById('health');
  const status = (kind, label, detail) => mount(el,
    h('span.health-status.health-' + kind, h('span.health-dot', { 'aria-hidden': 'true' }), h('span.health-label', label)),
    detail ? h('span.health-detail', detail) : null);
  try {
    const hres = await api.health();
    if (hres.model_configured) {
      status('ok', 'Модель настроена', 'Готовность модели проверяется при оценке');
      el.title = 'Сервер сообщает, что модель задана в конфигурации (CQA_MODEL). Это флаг настройки, а не проверка доступности модели: ошибки оценки показываются по каждому диалогу.';
    } else {
      status('warn', 'Модель не настроена', 'Оценка вернёт ошибку');
      el.title = 'На сервере не задан CQA_MODEL: импорт, просмотр, чек-листы и апелляции работают, автоматическая оценка — нет.';
    }
  } catch (e) {
    status('error', 'Сервер недоступен', null);
    el.title = 'Не удалось получить состояние сервера: ' + (e && e.message ? e.message : String(e));
  }
}

// Skip link: the router owns location.hash, so move focus without changing the hash.
document.addEventListener('click', (e) => {
  const link = e.target.closest && e.target.closest('.skip-link');
  if (!link) return;
  e.preventDefault();
  view.focus();
});

window.addEventListener('hashchange', render);
render();
healthBadge();
