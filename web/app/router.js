// Hash router: '#/path/segments?query'. Route patterns use ':param'.
const routes = [];

export function route(pattern, loader) {
  const keys = [];
  const re = new RegExp('^' + pattern.replace(/:[^/]+/g, (m) => { keys.push(m.slice(1)); return '([^/]+)'; }) + '/?$');
  routes.push({ re, keys, loader, pattern });
}

export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#/, '') || '/conversations';
  const [path, qs = ''] = raw.split('?');
  return { path, query: Object.fromEntries(new URLSearchParams(qs)) };
}

export function href(path, query) {
  const qs = query ? new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== '')).toString() : '';
  return '#' + path + (qs ? '?' + qs : '');
}

export function navigate(path, query, { replace = false } = {}) {
  const target = href(path, query);
  if (replace) history.replaceState(null, '', target), window.dispatchEvent(new HashChangeEvent('hashchange'));
  else location.hash = target;
}

/** Update query params without re-rendering the page (used for deep links like ?msg=). */
export function setQuery(query) {
  const { path, query: current } = parseHash();
  history.replaceState(null, '', href(path, { ...current, ...query }));
}

export function match(path) {
  for (const r of routes) {
    const m = path.match(r.re);
    if (m) return { loader: r.loader, pattern: r.pattern, params: Object.fromEntries(r.keys.map((k, i) => [k, decodeURIComponent(m[i + 1])])) };
  }
  return null;
}
