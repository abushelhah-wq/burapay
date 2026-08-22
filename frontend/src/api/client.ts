/**
 * The API client.
 *
 * One place that knows how to talk to the backend, so error handling, the auth
 * header and the base path are consistent. The token lives in memory plus
 * localStorage; the backend also sets an httpOnly cookie, which is what carries
 * browser-driven navigations such as the gateway return leg and file downloads.
 */

import type {
  AppSettings, AuditLogEntry, BenchmarkRun, ComparisonRow, ComparisonTest, Dashboard,
  Gateway, ApiLogEntry, GatewayHealth, HppRow, LogFilters, LogSummary, Page, Ranking,
  RoleDescription, ThreeDsChallenge, Transaction, TransactionDetail, User, UserFilters,
} from './types'

const BASE = '/api'
const TOKEN_KEY = 'burapay.token'
const CSRF_KEY = 'burapay.csrf'

export class ApiError extends Error {
  status: number
  category?: string
  detail?: unknown

  constructor(message: string, status: number, category?: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.category = category
    this.detail = detail
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

/**
 * The CSRF token from the last sign-in.
 *
 * Only needed for requests the browser authenticates with the session cookie — the
 * gateway return leg, a report opened in a new tab. Requests from this client carry a
 * bearer header instead, which nothing attaches automatically and so cannot be
 * forged cross-site. It is sent on every write regardless, so the app keeps working if
 * the bearer token is ever dropped in favour of the cookie alone.
 */
export function getCsrfToken(): string | null {
  return localStorage.getItem(CSRF_KEY)
}

export function setCsrfToken(token: string | null): void {
  if (token) localStorage.setItem(CSRF_KEY, token)
  else localStorage.removeItem(CSRF_KEY)
}

function query(params: Record<string, unknown> = {}): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((item) => search.append(key, String(item)))
    else search.append(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const csrf = getCsrfToken()
  const method = (init.method ?? 'GET').toUpperCase()
  if (csrf && method !== 'GET' && method !== 'HEAD') headers.set('X-CSRF-Token', csrf)

  const response = await fetch(`${BASE}${path}`, { ...init, headers, credentials: 'same-origin' })

  if (response.status === 401) {
    // The session is gone; clearing the token routes the app back to the sign-in page
    // rather than leaving every panel showing a permission error.
    setToken(null)
    setCsrfToken(null)
    throw new ApiError('Your session has expired. Please sign in again.', 401)
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`
    let category: string | undefined
    let detail: unknown
    try {
      const body = await response.json()
      message = body.message ?? body.detail ?? message
      category = body.category
      detail = body.detail
      if (typeof message !== 'string') message = JSON.stringify(message)
    } catch {
      /* a non-JSON error body; the status line is all there is to report */
    }
    throw new ApiError(message, response.status, category, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export interface TransactionFilters {
  gateway_code?: string[]
  integration_type?: string
  status?: string
  payment_mode?: string
  currency?: string
  environment?: string
  benchmark_run_id?: string
  merchant_reference?: string
  gateway_transaction_id?: string
  methodology?: string
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
}

export const api = {
  // -- auth ------------------------------------------------------------- //
  /** `username` accepts a username or an email address (specification section 9). */
  login: (username: string, password: string) =>
    request<{ access_token: string; expires_in: number; csrf_token: string; user: User }>(
      '/v1/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  logout: () => request<{ message: string }>('/v1/auth/logout', { method: 'POST' }),
  me: () => request<User>('/v1/auth/me'),
  changePassword: (current_password: string, new_password: string,
                   confirm_password?: string) =>
    request<{ message: string }>('/v1/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password, confirm_password }),
    }),

  // -- users (administrators only) --------------------------------------- //
  users: (filters: UserFilters = {}) =>
    request<Page<User>>(`/v1/users${query(filters as Record<string, unknown>)}`),
  user: (id: string) => request<User>(`/v1/users/${id}`),
  roles: () => request<RoleDescription[]>('/v1/users/roles'),
  createUser: (payload: {
    full_name?: string; username: string; email: string; role: string
    password: string; confirm_password: string; status: string
  }) => request<User>('/v1/users', { method: 'POST', body: JSON.stringify(payload) }),
  updateUser: (id: string, payload: {
    full_name?: string | null; email?: string; role?: string; status?: string
  }) => request<User>(`/v1/users/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  disableUser: (id: string) =>
    request<User>(`/v1/users/${id}/disable`, { method: 'POST' }),
  enableUser: (id: string) =>
    request<User>(`/v1/users/${id}/enable`, { method: 'POST' }),
  resetUserPassword: (id: string, new_password: string, confirm_password: string) =>
    request<{ message: string }>(`/v1/users/${id}/reset-password`, {
      method: 'POST', body: JSON.stringify({ new_password, confirm_password }),
    }),
  userAudit: (id: string, limit = 50) =>
    request<Page<AuditLogEntry>>(`/v1/users/${id}/audit${query({ limit })}`),

  // -- audit log (administrators only) ----------------------------------- //
  auditLogs: (filters: { event?: string; user_id?: string; limit?: number; offset?: number } = {}) =>
    request<Page<AuditLogEntry>>(`/v1/audit-logs${query(filters)}`),
  auditEvents: () => request<string[]>('/v1/audit-logs/events'),

  // -- gateways --------------------------------------------------------- //
  gateways: (environment = 'sandbox') =>
    request<Gateway[]>(`/v1/gateways${query({ environment })}`),
  gateway: (code: string, environment = 'sandbox') =>
    request<Gateway>(`/v1/gateways/${code}${query({ environment })}`),
  updateGateway: (code: string, payload: Record<string, unknown>) =>
    request<Gateway>(`/v1/gateways/${code}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  credentials: (code: string, environment = 'sandbox') =>
    request<{ gateway_code: string; environment: string; values: Record<string, string>;
      configured: boolean; missing_fields: string[] }>(
      `/v1/gateways/${code}/credentials${query({ environment })}`),
  saveCredentials: (code: string, environment: string, values: Record<string, unknown>) =>
    request<{ values: Record<string, string>; configured: boolean; missing_fields: string[] }>(
      `/v1/gateways/${code}/credentials`,
      { method: 'PUT', body: JSON.stringify({ environment, values }) }),
  deleteCredentials: (code: string, environment = 'sandbox') =>
    request<{ message: string }>(`/v1/gateways/${code}/credentials${query({ environment })}`,
      { method: 'DELETE' }),
  health: (hours = 24) =>
    request<GatewayHealth[]>(`/v1/gateways/health/status${query({ hours })}`),
  runHealthCheck: () =>
    request<GatewayHealth[]>('/v1/gateways/health/check', { method: 'POST' }),

  // -- transactions ----------------------------------------------------- //
  startTransaction: (payload: Record<string, unknown>) =>
    request<{ transaction_id: string; status: string; redirect_url: string | null;
      mode: string | null; gateway_reference: string | null;
      stored_token: string | null; requires_customer_action: boolean;
      agreement_id: string | null }>(
      '/v1/transactions/start', { method: 'POST', body: JSON.stringify(payload) }),
  threeDsChallenge: (id: string) =>
    request<ThreeDsChallenge>(`/v1/transactions/${id}/three-ds`),
  transactions: (filters: TransactionFilters = {}) =>
    request<Page<Transaction>>(`/v1/transactions${query(filters as Record<string, unknown>)}`),
  transaction: (id: string) => request<TransactionDetail>(`/v1/transactions/${id}`),
  hppHandoff: (id: string) =>
    request<{ transaction_id: string; gateway_code: string; status: string; mode: string;
      redirect_url: string | null; return_url: string; environment: string; amount: number;
      currency: string; widget: Record<string, string> }>(`/v1/transactions/${id}/hpp`),
  // -- call log --------------------------------------------------------- //
  logs: (filters: LogFilters = {}) =>
    request<Page<ApiLogEntry>>(`/v1/logs${query(filters as Record<string, unknown>)}`),
  logSummary: () => request<LogSummary>('/v1/logs/summary'),
  clearLogs: (before?: string) =>
    request<{ message: string }>(`/v1/logs${query({ before })}`, { method: 'DELETE' }),

  deleteTransaction: (id: string) =>
    request<{ message: string }>(`/v1/transactions/${id}`, { method: 'DELETE' }),
  reportBrowserMetrics: (id: string, payload: {
    metrics: Record<string, number>; origin_scope: string; page_load_time_ms?: number }) =>
    request<{ message: string }>(`/v1/transactions/${id}/browser-metrics`, {
      method: 'POST', body: JSON.stringify(payload) }),

  // -- benchmarks ------------------------------------------------------- //
  limits: () => request<{ min_interval_seconds: number; max_transactions_per_run: number;
    note: string }>('/v1/benchmarks/limits'),
  createRun: (payload: Record<string, unknown>) =>
    request<BenchmarkRun>('/v1/benchmarks/runs', { method: 'POST', body: JSON.stringify(payload) }),
  runs: (params: Record<string, unknown> = {}) =>
    request<Page<BenchmarkRun>>(`/v1/benchmarks/runs${query(params)}`),
  run: (id: string) => request<BenchmarkRun>(`/v1/benchmarks/runs/${id}`),
  cancelRun: (id: string) =>
    request<{ message: string }>(`/v1/benchmarks/runs/${id}/cancel`, { method: 'POST' }),
  deleteRun: (id: string) =>
    request<{ message: string }>(`/v1/benchmarks/runs/${id}`, { method: 'DELETE' }),
  createComparisonTest: (payload: Record<string, unknown>) =>
    request<ComparisonTest>('/v1/benchmarks/comparison-tests', {
      method: 'POST', body: JSON.stringify(payload) }),
  comparisonTests: () =>
    request<Page<ComparisonTest>>('/v1/benchmarks/comparison-tests'),
  comparisonTest: (id: string) =>
    request<{ test: ComparisonTest; runs: BenchmarkRun[]; results: ComparisonRow[];
      ranking: Ranking; progress: Record<string, { completed: number; total: number }> }>(
      `/v1/benchmarks/comparison-tests/${id}`),

  // -- comparison and reports ------------------------------------------- //
  dashboard: (days = 30) => request<Dashboard>(`/v1/comparison/dashboard${query({ days })}`),
  comparison: (params: Record<string, unknown> = {}) =>
    request<{ rows: ComparisonRow[]; note: string }>(`/v1/comparison${query(params)}`),
  hppComparison: (params: Record<string, unknown> = {}) =>
    request<{ rows: HppRow[]; note: string }>(`/v1/comparison/hpp${query(params)}`),
  integrationComparison: (params: Record<string, unknown> = {}) =>
    request<{ rows: { gateway_code: string; gateway_name: string; hpp: ComparisonRow | null;
      direct: ComparisonRow | null; api_delta_ms: number | null }[] }>(
      `/v1/comparison/integrations${query(params)}`),
  timeseries: (params: Record<string, unknown> = {}) =>
    request<{ bucket: string; series: { bucket: string; gateway_code: string;
      integration_type: string; count: number; mean: number; p95: number;
      median: number }[] }>(`/v1/comparison/timeseries${query(params)}`),
  ranking: (params: Record<string, unknown> = {}) =>
    request<Ranking>(`/v1/comparison/ranking${query(params)}`),
  gatewaySummary: (params: Record<string, unknown> = {}) =>
    request<{ rows: ComparisonRow[] }>(`/v1/reports/gateway-summary${query(params)}`),
  apiBreakdown: (params: Record<string, unknown> = {}) =>
    request<{ rows: Record<string, unknown>[] }>(`/v1/reports/api-breakdown${query(params)}`),
  hppPerformance: (params: Record<string, unknown> = {}) =>
    request<{ rows: HppRow[] }>(`/v1/reports/hpp-performance${query(params)}`),
  exportUrl: (params: Record<string, unknown>) => `${BASE}/v1/reports/export${query(params)}`,

  // -- settings --------------------------------------------------------- //
  settings: () => request<AppSettings>('/v1/settings'),
  updateSetting: (key: string, value: Record<string, unknown>) =>
    request<{ key: string; value: Record<string, unknown> }>(`/v1/settings/${key}`, {
      method: 'PUT', body: JSON.stringify(value) }),
}

/**
 * Download an export.
 *
 * Fetched rather than opened in a new tab so the Authorization header travels with
 * it; a plain link would rely on the cookie alone and fail for anyone whose browser
 * blocks it.
 */
export async function downloadExport(params: Record<string, unknown>,
                                     filename: string): Promise<void> {
  const token = getToken()
  const response = await fetch(api.exportUrl(params), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    credentials: 'same-origin',
  })
  if (!response.ok) {
    throw new ApiError(`Export failed (${response.status})`, response.status)
  }
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
