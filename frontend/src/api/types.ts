/** Types mirroring the backend's response shapes. */

export interface StatSummary {
  count: number
  min: number | null
  max: number | null
  mean: number | null
  median: number | null
  p50: number | null
  p90: number | null
  p95: number | null
  p99: number | null
  stdev: number | null
  reliable: boolean
}

export interface User {
  id: string
  email: string
  full_name: string | null
  role: 'admin' | 'viewer'
  is_active: boolean
  last_login_at: string | null
}

export interface CredentialField {
  key: string
  label: string
  required: boolean
  secret: boolean
  placeholder: string
  help_text: string
  default: string | null
  choices: string[]
}

export interface Gateway {
  id: string
  code: string
  name: string
  enabled: boolean
  supports_hpp: boolean
  supports_direct: boolean
  supported_currencies: string[]
  supported_payment_modes: string[]
  logo_slug: string | null
  display_order: number
  docs_url: string | null
  notes: string[]
  configured: boolean
  missing_fields: string[]
  /** Stored credential key names only — never values. */
  configured_fields: string[]
  status: string
  credential_fields: CredentialField[]
  documented_calls: Record<string, string>
  is_simulated: boolean
}

export interface Transaction {
  id: string
  benchmark_run_id: string | null
  gateway_code: string
  gateway_transaction_id: string | null
  merchant_reference: string
  integration_type: 'hpp' | 'direct'
  payment_mode: string
  environment: string
  amount: number
  currency: string
  description: string | null
  status: string
  started_at: string
  completed_at: string | null
  total_duration_ms: number | null
  gateway_api_time_ms: number | null
  three_ds_time_ms: number | null
  customer_interaction_time_ms: number | null
  redirect_time_ms: number | null
  page_load_time_ms: number | null
  webhook_latency_ms: number | null
  app_overhead_ms: number | null
  api_call_count: number
  error_category: string | null
  error_message: string | null
  gateway_response_code: string | null
  gateway_response_message: string | null
  methodology: string
  webhook_received_at: string | null
}

export interface ApiMeasurement {
  id: string
  sequence: number
  operation_name: string
  normalized_operation: string
  endpoint: string
  http_method: string
  started_at: string
  completed_at: string | null
  duration_ms: number
  http_status: number | null
  gateway_response_code: string | null
  gateway_response_message: string | null
  success: boolean
  error_category: string | null
  error_message: string | null
  timed_out: boolean
  is_setup_call: boolean
  response_size_bytes: number | null
  response_snippet: string | null
}

export interface TransactionEvent {
  id: string
  event_type: string
  event_timestamp: string
  offset_ms: number
  label: string | null
  event_metadata: Record<string, unknown>
}

export interface BrowserMeasurement {
  id: string
  metric_name: string
  value_ms: number
  origin_scope: string | null
  created_at: string
}

export interface TransactionDetail {
  transaction: Transaction
  measurements: ApiMeasurement[]
  events: TransactionEvent[]
  browser_measurements: BrowserMeasurement[]
  gateway_api_time_ms: number
  setup_call_time_ms: number
  documented_calls: string | null
  /** Present only on the transaction that minted it, for copying into Settings. */
  stored_token: string | null
  stored_token_hint: string | null
}

export interface ComparisonRow {
  gateway_code: string
  gateway_name: string
  integration_type: string
  payment_mode: string
  is_simulated: boolean
  documented_calls: string | null
  transactions: number
  completed: number
  api: StatSummary
  total: StatSummary
  success_rate: number | null
  failure_rate: number | null
  successes: number
  evaluated: number
  api_call_count: number | null
  api_call_count_range: number[]
  status_breakdown: Record<string, number>
  error_breakdown: Record<string, number>
}

export interface HppRow {
  gateway_code: string
  gateway_name: string
  transactions: number
  completed: number
  session_creation: StatSummary
  redirect: StatSummary
  page_load: StatSummary
  customer_interaction: StatSummary
  three_ds: StatSummary
  webhook: StatSummary
  gateway_api: StatSummary
  total: StatSummary
  success_rate: number | null
}

export interface BenchmarkRun {
  id: string
  name: string
  gateway_id: string | null
  comparison_test_id: string | null
  integration_type: string
  payment_mode: string
  environment: string
  currency: string
  amount: number
  transaction_count: number
  interval_seconds: number
  methodology: string
  status: string
  started_at: string | null
  completed_at: string | null
  error_message: string | null
  run_metadata: Record<string, unknown>
  created_at: string
  gateway_code?: string | null
  progress?: RunProgress
  statistics?: ComparisonRow
}

export interface RunProgress {
  completed: number
  total: number
  percent: number
  succeeded: number
  pending: number
  is_active: boolean
}

export interface ComparisonTest {
  id: string
  name: string
  integration_type: string
  environment: string
  currency: string
  amount: number
  transactions_per_gateway: number
  gateway_codes: string[]
  status: string
  started_at: string | null
  completed_at: string | null
  created_at: string
}

export interface RankingEntry {
  gateway_code: string
  gateway_name: string
  integration_type: string
  sample_size: number
  score: number
  rank: number
  components: Record<string, number | null>
  measured_components: string[]
  unmeasurable_components: string[]
}

export interface Ranking {
  label: string
  note: string
  weights: Record<string, number>
  entries: RankingEntry[]
  excluded: { gateway_code: string; gateway_name: string; sample_size: number; reason: string }[]
  min_samples: number
}

export interface DashboardSummary {
  total_transactions: number
  completed_transactions: number
  average_response_ms: number | null
  p95_response_ms: number | null
  sample_size: number
  statistically_reliable: boolean
  success_rate: number | null
  failure_rate: number | null
  fastest_gateway: {
    gateway_code: string
    gateway_name: string
    integration_type: string
    mean_api_ms: number
    sample_size: number
  } | null
  fastest_gateway_note: string | null
}

export interface GatewayHealth {
  gateway_code: string
  available: boolean | null
  response_time_ms: number | null
  http_status: number | null
  checked_at: string
  detail: string | null
  status: string
  history?: { checked_at: string; available: boolean; response_time_ms: number | null }[]
}

export interface Dashboard {
  window_days: number
  summary: DashboardSummary
  comparison: ComparisonRow[]
  latest_runs: BenchmarkRun[]
  recent_transactions: Partial<Transaction>[]
  gateway_health: GatewayHealth[]
}

export interface AppSettings {
  settings: {
    scoring_weights: Record<string, number>
    benchmark_limits: {
      min_interval_seconds: number
      max_transactions_per_run: number
      max_concurrency: number
    }
    test_defaults: { amount: number; currency: string; description: string }
    test_card: Record<string, string>
  }
  descriptions: Record<string, string>
  scoring_components: Record<string, string>
  default_scoring_weights: Record<string, number>
  environment: {
    app_env: string
    app_version: string
    public_base_url: string
    test_mode: boolean
    allow_production_gateways: boolean
    allow_direct_card_entry: boolean
    insecure_defaults: string[]
  }
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}
