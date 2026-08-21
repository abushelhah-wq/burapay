/**
 * API client.
 *
 * Same-origin requests to /api, which the reverse proxy routes to the backend.
 * Credentials ride on the session cookie, so every call sends credentials and
 * nothing here ever holds a token.
 *
 * A failed response is turned into an ApiError carrying the backend's
 * structured error body. The backend returns a stable `code` on every typed
 * failure precisely so the UI can branch on it rather than match message text.
 */

export class ApiError extends Error {
  constructor(status, payload) {
    const detail = payload?.error ?? payload?.detail ?? payload
    const message =
      (typeof detail === 'string' ? detail : detail?.message) ||
      `Request failed with status ${status}`
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = typeof detail === 'object' ? detail?.code : undefined
    this.docUrl = typeof detail === 'object' ? detail?.doc_url : undefined
    this.detail = detail
  }
}

async function request(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
    signal,
  })

  if (response.status === 204) return null

  let payload = null
  const text = await response.text()
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { detail: text }
    }
  }

  if (!response.ok) throw new ApiError(response.status, payload)
  return payload
}

function query(params) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((item) => search.append(key, item))
    else search.append(key, value)
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

/**
 * A client-generated idempotency key (§0.6).
 *
 * Generated once per submit attempt and reused if the user retries the same
 * submission, so a double-click or a flaky connection cannot produce two
 * transactions.
 */
export function newIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return `ui-${globalThis.crypto.randomUUID()}`
  return `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export const api = {
  session: () => request('/api/auth/session'),
  login: (email, password) =>
    request('/api/auth/login', { method: 'POST', body: { email, password } }),
  logout: () => request('/api/auth/logout', { method: 'POST' }),

  flags: () => request('/api/settings/flags'),
  health: () => request('/health'),

  gateways: () => request('/api/gateways'),
  gateway: (code) => request(`/api/gateways/${code}`),
  integrationTypes: () => request('/api/gateways/integration-types'),
  testCards: (code) => request(`/api/gateways/${code}/test-cards`),

  credentials: (code) => request(`/api/settings/gateways/${code}/credentials`),
  putCredential: (code, payload) =>
    request(`/api/settings/gateways/${code}/credentials`, { method: 'PUT', body: payload }),
  deleteCredential: (code, keyName) =>
    request(`/api/settings/gateways/${code}/credentials/${encodeURIComponent(keyName)}`, {
      method: 'DELETE',
    }),
  putConfig: (code, payload) =>
    request(`/api/settings/gateways/${code}/config`, { method: 'PUT', body: payload }),
  testConnection: (code) =>
    request(`/api/settings/gateways/${code}/test-connection`, { method: 'POST' }),

  transactions: (params) => request(`/api/transactions${query(params)}`),
  transaction: (id) => request(`/api/transactions/${id}`),
  webhooks: (params) => request(`/api/webhooks${query(params)}`),
  tokens: (params) => request(`/api/tokens${query(params)}`),

  hppSession: (code, body) =>
    request(`/api/payments/${code}/hpp/session`, { method: 'POST', body }),
  directSale: (code, body) =>
    request(`/api/payments/${code}/direct/sale`, { method: 'POST', body }),
  directAuth: (code, body) =>
    request(`/api/payments/${code}/direct/auth`, { method: 'POST', body }),
  tokenize: (code, body) =>
    request(`/api/payments/${code}/tokenize`, { method: 'POST', body }),
  chargeCit: (code, body) =>
    request(`/api/payments/${code}/token/cit`, { method: 'POST', body }),
  chargeMit: (code, body) =>
    request(`/api/payments/${code}/token/mit`, { method: 'POST', body }),

  capture: (id, body) =>
    request(`/api/payments/transactions/${id}/capture`, { method: 'POST', body }),
  refund: (id, body) =>
    request(`/api/payments/transactions/${id}/refund`, { method: 'POST', body }),
  void: (id, body) =>
    request(`/api/payments/transactions/${id}/void`, { method: 'POST', body }),
  queryOrder: (id) =>
    request(`/api/payments/transactions/${id}/query`, { method: 'POST' }),

  benchmark: (params) => request(`/api/analytics/benchmark${query(params)}`),
  comparison: (params) => request(`/api/analytics/comparison${query(params)}`),
}
