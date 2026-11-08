// Single API client. No other module calls fetch directly.
// Base URL is configurable for embedding (e.g. Dynamics 365 web resource): window.CQA_CONFIG.apiBase.
const base = () => ((window.CQA_CONFIG && window.CQA_CONFIG.apiBase) || '').replace(/\/$/, '');

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request(method, path, body, { timeoutMs = 30000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(base() + path, {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e) {
    throw new ApiError(0, e.name === 'AbortError' ? 'timeout' : 'network',
      e.name === 'AbortError' ? 'Превышено время ожидания ответа сервера' : 'Сервер недоступен');
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) {
    const err = (data && data.error) || {};
    const e = new ApiError(res.status, err.code || 'http_' + res.status, err.message ? translateServerMessage(err.message) : `Ошибка HTTP ${res.status}`);
    e.serverMessage = err.message;
    throw e;
  }
  return data;
}

// Some server validation messages are English; show them in Russian. Unknown messages pass through as-is.
const SERVER_MESSAGES_RU = {
  'Dataset provider did not report success': 'Источник данных не сообщил об успехе (isSuccess ≠ true)',
  'Dataset data must be an object': 'Поле data набора должно быть объектом',
  'dataset_group_id must be a nonempty string': 'dataset_group_id должен быть непустой строкой',
  'Dataset records must be an array': 'records должен быть массивом диалогов',
  'Conversation must be an object': 'Каждый диалог должен быть объектом',
  'Conversation IDs must be nonempty and unique': 'conversation_id должен быть непустым и уникальным',
  'Unsupported conversation language': 'Неподдерживаемый язык диалога (допустимы RU, KZ, EN)',
  'Unsupported conversation channel': 'Неподдерживаемый канал (допустимы «agent chat», «voice call»)',
  'Transcript must be a nonempty array': 'transcript должен быть непустым массивом',
  'audio_url must be a string or null': 'audio_url должен быть строкой или null',
  'audio_url must use HTTP or HTTPS': 'audio_url должен начинаться с http:// или https://',
  'Message must be an object': 'Каждое сообщение должно быть объектом',
  'Unsupported transcript role': 'Неподдерживаемая роль в транскрипте (допустимы operator, client, bot, system)',
  'Message timestamp must be a nonempty string': 'timestamp сообщения должен быть непустой строкой',
  'Message text must be a string': 'text сообщения должен быть строкой',
  'group_id must be a nonempty string': 'group_id должен быть непустой строкой',
  'DEV endpoint is not configured': 'Адрес DEV-набора не настроен на сервере',
  'Dataset request failed; check local endpoint configuration and connectivity': 'Не удалось получить набор данных: проверьте настройку адреса на сервере и сеть',
  'checklist must be an object': 'Чек-лист должен быть объектом',
  'provenance must be demonstration, custom or official': 'Происхождение: demonstration, custom или official',
  'criteria must be a nonempty array': 'Нужен хотя бы один критерий',
  'criterion must be an object': 'Критерий должен быть объектом',
  'duplicate criterion id': 'ID критериев должны быть уникальными',
  'criterion.type must be boolean or numeric': 'Тип критерия: да/нет или числовая шкала',
  'criterion.weight must be nonnegative': 'Вес критерия не может быть отрицательным',
  'criterion.section must be string': 'Раздел критерия должен быть строкой',
  'numeric scale requires min < max': 'Для числовой шкалы минимум должен быть меньше максимума',
  'numeric scale range is too large': 'Слишком большой диапазон шкалы',
  'allowed_values must contain at least two scale steps': 'Нужно не меньше двух ступеней шкалы',
  'allowed_values must be unique, ascending and include min/max endpoints': 'Ступени шкалы: уникальные, по возрастанию, включая минимум и максимум',
  'at least one criterion weight must be positive': 'Хотя бы один критерий должен иметь положительный вес',
  'combined criterion weight is too large': 'Суммарный вес критериев слишком большой',
  'rules.decimals must be integer 0..6': 'Знаков после запятой: целое число от 0 до 6',
  'rules.gross_error_policy must be flag or zero_total': 'Политика грубой ошибки: flag или zero_total',
  'evidence must contain existing message ids': 'Доказательства должны ссылаться на существующие сообщения',
  'duplicate evidence message ids': 'Сообщения-доказательства не должны повторяться',
  'checked_span must name an existing ordered message range': 'Проверенный участок должен быть существующим диапазоном сообщений по порядку',
  'missed action requires checked_span': 'Для пропущенного действия укажите проверенный участок',
  'N/A is not allowed for this criterion': 'N/A не разрешён для этого критерия',
  'N/A cannot be a gross error': 'Ответ N/A не может быть грубой ошибкой',
  'applicable answer requires evidence or a checked missed-action span': 'Для применимого ответа укажите доказательства или проверенный участок пропущенного действия',
  'boolean criterion requires boolean answer': 'Для критерия да/нет нужен ответ Да или Нет',
  'numeric answer is outside the configured scale': 'Значение вне диапазона шкалы',
  'numeric answer must be one of the configured allowed_values': 'Значение должно быть одной из допустимых ступеней шкалы',
  'gross error is not enabled for this criterion': 'Для этого критерия грубая ошибка не предусмотрена',
  'invalid model endpoint configuration': 'Некорректная настройка адреса модели на сервере',
  'model response too large': 'Ответ модели слишком большой',
  'model request failed or returned invalid structured evaluation': 'Запрос к модели не выполнен или модель вернула некорректную структуру оценки',
};

export function translateServerMessage(msg) {
  return SERVER_MESSAGES_RU[msg] || msg;
}

const q = (params = {}) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') s.set(k, v);
  const str = s.toString();
  return str ? '?' + str : '';
};
const enc = encodeURIComponent;

export const api = {
  health: () => request('GET', '/api/health'),
  importDev: (group_id = 'DEV') => request('POST', '/api/imports', { source: 'dev', group_id }, { timeoutMs: 120000 }),
  importJson: (payload, group_id = 'JSON') => request('POST', '/api/imports', { source: 'json', payload, group_id }, { timeoutMs: 120000 }),
  conversations: (filters) => request('GET', '/api/conversations' + q(filters)).then((r) => r.items),
  conversation: (id) => request('GET', '/api/conversations/' + enc(id)),
  audio: (id) => request('GET', '/api/conversations/' + enc(id) + '/audio'),
  checklists: () => request('GET', '/api/checklists').then((r) => r.items),
  checklist: (id) => request('GET', '/api/checklists/' + enc(id)),
  saveChecklist: (checklist) => request('POST', '/api/checklists', checklist),
  // Local reasoning can exceed 300 s; leave a margin beyond the server default of 600 s.
  evaluate: (conversation_id, checklist_id) =>
    request('POST', '/api/evaluations', { conversation_id, checklist_id }, { timeoutMs: 660000 }),
  evaluations: (conversation_id) => request('GET', '/api/evaluations' + q({ conversation_id })).then((r) => r.items),
  evaluation: (id) => request('GET', '/api/evaluations/' + enc(id)),
  appeals: (evaluation_id) => request('GET', '/api/appeals' + q({ evaluation_id })).then((r) => r.items),
  fileAppeal: (evaluation_id, criterion_id, comment) =>
    request('POST', '/api/appeals', { evaluation_id, criterion_id, comment }),
  reviewAppeal: (id, body) => request('POST', '/api/appeals/' + enc(id) + '/review', body),
  // Official appendix JSON 1.0 (results/{dev,prod}/results.json). Returns the server document unchanged;
  // 422 validation errors surface as ApiError like every other call. No client-side scoring or filling.
  exportResults: ({ dataset, group_id, team_name, reviewed = 0 }) =>
    request('GET', '/api/results/export' + q({ dataset, group_id, team_name, reviewed: String(reviewed ? 1 : 0) }), undefined, { timeoutMs: 120000 }),
};
