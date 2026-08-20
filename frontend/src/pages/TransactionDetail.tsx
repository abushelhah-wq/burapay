/**
 * One transaction in full (specification section 45): the timeline, every API call
 * with its response time, and the timing breakdown.
 *
 * The breakdown is the point of the page. Four numbers are shown separately and never
 * added together into a single "speed" figure, because they measure different things
 * and only one of them is the gateway's responsibility.
 */

import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { collectReturnMetrics } from '../lib/browserMetrics'
import type { TransactionDetail as Detail } from '../api/types'
import { Badge, Card, DurationBar, EmptyState, ErrorNotice, Note, Spinner,
         StatusBadge } from '../components/ui'
import { dateTime, integrationLabel, money, ms, msExact, paymentModeLabel,
         seconds } from '../lib/format'

const OPERATION_TONE: Record<string, string> = {
  SESSION_CREATION: 'bg-sky-50 text-sky-700 ring-sky-600/20',
  PAYMENT_INITIATION: 'bg-accent-50 text-accent-700 ring-accent-500/20',
  AUTHENTICATION: 'bg-violet-50 text-violet-700 ring-violet-600/20',
  AUTHORIZATION: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  STATUS_CHECK: 'bg-ink-100 text-ink-700 ring-ink-500/20',
  TOKENIZATION: 'bg-amber-50 text-amber-700 ring-amber-600/20',
  REFUND: 'bg-orange-50 text-orange-700 ring-orange-600/20',
}

export default function TransactionDetail() {
  const { id = '' } = useParams()
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(() => {
    setDetail(null)
    api.transaction(id).then(setDetail).catch(setError)
  }, [id])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    // Landing here after a hosted checkout is the one moment the browser can report
    // anything: the return navigation is same-origin, and the wall-clock gap since
    // the redirect is the time spent on the gateway's side. Reported once, then the
    // marker is cleared so a refresh does not double-count.
    if (!id) return
    const collected = collectReturnMetrics(id)
    if (!collected) return
    api.reportBrowserMetrics(id, collected).then(load).catch(() => {})
  }, [id, load])

  if (error) return <ErrorNotice error={error} onRetry={load} />
  if (!detail) return <Spinner label="Loading transaction" />

  const { transaction, measurements, events, browser_measurements } = detail
  const timed = measurements.filter((call) => !call.is_setup_call)
  const setup = measurements.filter((call) => call.is_setup_call)
  const slowest = Math.max(1, ...timed.map((call) => call.duration_ms))
  const lastOffset = Math.max(1, ...events.map((event) => event.offset_ms))

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link to="/transactions" className="text-sm text-accent-600 hover:underline">
            ← All transactions
          </Link>
          <h1 className="mt-1 text-2xl font-semibold text-ink-900">
            {transaction.gateway_code} · {integrationLabel(transaction.integration_type)}
          </h1>
          <p className="mt-1 font-mono text-xs text-ink-500">
            {transaction.merchant_reference}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge status={transaction.status} />
          <Badge>{transaction.environment}</Badge>
          <Badge>{transaction.methodology}</Badge>
          {transaction.payment_mode && transaction.payment_mode !== 'standard' && (
            <Badge tone="info">{paymentModeLabel(transaction.payment_mode)}</Badge>
          )}
        </div>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">
            Gateway API time
          </p>
          <p className="mt-2 text-3xl font-semibold tabular text-ink-900">
            {ms(transaction.gateway_api_time_ms)}
          </p>
          <p className="mt-1 text-xs text-ink-500">
            {timed.length} timed call{timed.length === 1 ? '' : 's'}
            {detail.documented_calls && ` · documented: ${detail.documented_calls}`}
          </p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">
            3DS / authentication
          </p>
          <p className="mt-2 text-3xl font-semibold tabular text-ink-900">
            {ms(transaction.three_ds_time_ms)}
          </p>
          <p className="mt-1 text-xs text-ink-500">issuer and customer, not the gateway API</p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">
            Customer interaction
          </p>
          <p className="mt-2 text-3xl font-semibold tabular text-ink-900">
            {ms(transaction.customer_interaction_time_ms)}
          </p>
          <p className="mt-1 text-xs text-ink-500">
            {transaction.integration_type === 'hpp'
              ? 'time spent on the hosted page'
              : 'time spent verifying with the issuer'}
          </p>
        </div>
        <div className="card p-5">
          <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">
            End-to-end
          </p>
          <p className="mt-2 text-3xl font-semibold tabular text-ink-900">
            {seconds(transaction.total_duration_ms)}
          </p>
          <p className="mt-1 text-xs text-ink-500">
            includes everything above{transaction.app_overhead_ms !== null &&
              ` · ${msExact(transaction.app_overhead_ms)} platform overhead`}
          </p>
        </div>
      </div>

      <Note>
        These four figures measure different things and are never summed into a single
        score. Only the first is the gateway’s API latency; the customer’s time on a
        hosted page is not something a gateway can be faster at.
      </Note>

      {detail.awaiting_customer_action && (
        <Card title="Waiting for the cardholder">
          <p className="text-sm text-ink-700">
            This payment stopped for a 3-D Secure challenge. It stays pending until the
            cardholder verifies with their bank and is sent back, and the time in
            between is recorded as customer interaction rather than gateway latency.
          </p>
          <Link to={`/three-ds/${transaction.id}`} className="btn-primary mt-3 inline-flex">
            Continue verification
          </Link>
        </Card>
      )}

      {detail.stored_token && (
        <Card title="Card token">
          <p className="text-sm text-ink-700">
            This payment stored the card. Paste the token into{' '}
            <Link to={`/settings?gateway=${transaction.gateway_code}`}
                  className="text-accent-600 hover:underline">
              {transaction.gateway_code}’s settings
            </Link>{' '}
            to charge it again later without card details.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <code className="select-all break-all rounded-lg border border-ink-200
                             bg-ink-50 px-3 py-2 font-mono text-sm text-ink-800">
              {detail.stored_token}
            </code>
            <button type="button" className="btn-secondary"
                    onClick={() => navigator.clipboard?.writeText(detail.stored_token ?? '')}>
              Copy
            </button>
            {detail.stored_token_hint && (
              <span className="text-xs text-ink-500">{detail.stored_token_hint}</span>
            )}
          </div>
          <p className="mt-3 text-xs text-ink-500">
            Shown here and nowhere else. The platform does not save it into the
            gateway’s credentials on its own: a card token belongs to a cardholder, not
            to the merchant account, and storing one is a decision to make deliberately.
          </p>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-5">
        <Card title="API performance" className="lg:col-span-3">
          {timed.length === 0 ? (
            <EmptyState title="No timed calls recorded"
                        description="The transaction failed before any gateway call completed." />
          ) : (
            <>
              <div className="space-y-3">
                {timed.map((call) => (
                  <div key={call.id}>
                    <div className="mb-1 flex items-center justify-between gap-3">
                      <span className="truncate text-sm text-ink-800">
                        {call.sequence}. {call.operation_name}
                      </span>
                      <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px]
                                        font-medium uppercase ring-1 ring-inset
                        ${OPERATION_TONE[call.normalized_operation] ??
                          'bg-ink-100 text-ink-600 ring-ink-500/20'}`}>
                        {call.normalized_operation.replace(/_/g, ' ')}
                      </span>
                    </div>
                    <DurationBar value={call.duration_ms} max={slowest} />
                  </div>
                ))}
              </div>

              <div className="mt-6 overflow-x-auto">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Step</th><th className="text-right">Response time</th>
                      <th className="text-right">HTTP</th><th>Gateway code</th><th>Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {timed.map((call) => (
                      <Fragment key={call.id}>
                        <tr>
                          <td className="max-w-[18rem] truncate">{call.operation_name}</td>
                          <td className="num">{msExact(call.duration_ms)}</td>
                          <td className="num">{call.http_status ?? '—'}</td>
                          <td className="font-mono text-xs">
                            {call.gateway_response_code ?? '—'}
                          </td>
                          <td>
                            {call.success
                              ? <span className="text-emerald-600">Success</span>
                              : <span className="text-red-600">
                                  {call.error_category ?? 'Failed'}
                                </span>}
                          </td>
                        </tr>
                        {/* What the gateway actually said. A generic code like Geidea's
                            "General error" is unreadable without this, and reading it
                            should not require a shell on the server. Secrets and card
                            numbers are stripped before it is ever stored. */}
                        {(call.gateway_response_message || call.error_message
                          || call.response_snippet) && !call.success && (
                          <tr>
                            <td colSpan={5} className="bg-ink-50 !py-2">
                              {call.gateway_response_message && (
                                <p className="text-xs text-red-700">
                                  {call.gateway_response_message}
                                </p>
                              )}
                              {call.response_snippet && (
                                <details className="mt-1">
                                  <summary className="cursor-pointer text-xs text-ink-500">
                                    Raw gateway response
                                  </summary>
                                  <pre className="mt-2 max-h-56 overflow-auto rounded
                                                  bg-ink-900 p-3 text-[11px] leading-relaxed
                                                  text-ink-100">
{call.response_snippet}
                                  </pre>
                                </details>
                              )}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    ))}
                    <tr className="bg-ink-50 font-semibold">
                      <td>Gateway API time</td>
                      <td className="num">{msExact(detail.gateway_api_time_ms)}</td>
                      <td colSpan={3} />
                    </tr>
                  </tbody>
                </table>
              </div>

              {setup.length > 0 && (
                <div className="mt-6">
                  <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
                    Setup calls — excluded from the measurement
                  </p>
                  <table className="table">
                    <tbody>
                      {setup.map((call) => (
                        <tr key={call.id}>
                          <td className="text-ink-600">{call.operation_name}</td>
                          <td className="num text-ink-500">{msExact(call.duration_ms)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="mt-2 text-xs text-ink-500">
                    Work a real merchant would already have done — minting a token, or
                    creating a payment so there is something to act on. Recorded in full,
                    excluded from the gateway’s reported latency.
                  </p>
                </div>
              )}
            </>
          )}
        </Card>

        <Card title="Timeline" className="lg:col-span-2">
          {events.length === 0 ? <EmptyState title="No timeline events" /> : (
            <ol className="space-y-3">
              {events.map((event) => (
                <li key={event.id} className="flex gap-3">
                  <span className="w-16 shrink-0 tabular text-right font-mono text-xs
                                   text-ink-500">
                    {(event.offset_ms / 1000).toFixed(3)}
                  </span>
                  <span className="relative flex-1 border-l border-ink-200 pb-1 pl-4">
                    <span className="absolute -left-[5px] top-1.5 h-2.5 w-2.5 rounded-full
                                     bg-accent-500" />
                    <span className="block text-sm text-ink-800">
                      {event.label || event.event_type}
                    </span>
                    <span className="block text-[11px] text-ink-400">
                      {new Date(event.event_timestamp).toLocaleTimeString()}
                    </span>
                    <span className="mt-1 block h-1 rounded-full bg-ink-100">
                      <span className="block h-1 rounded-full bg-accent-200"
                            style={{ width: `${(event.offset_ms / lastOffset) * 100}%` }} />
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          )}
        </Card>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Transaction">
          <dl className="grid grid-cols-2 gap-y-3 text-sm">
            <dt className="text-ink-500">Amount</dt>
            <dd className="tabular">{money(transaction.amount, transaction.currency)}</dd>
            <dt className="text-ink-500">Gateway transaction ID</dt>
            <dd className="break-all font-mono text-xs">
              {transaction.gateway_transaction_id ?? '—'}
            </dd>
            <dt className="text-ink-500">Started</dt>
            <dd>{dateTime(transaction.started_at)}</dd>
            <dt className="text-ink-500">Completed</dt>
            <dd>{dateTime(transaction.completed_at)}</dd>
            <dt className="text-ink-500">Redirect time</dt>
            <dd className="tabular">{ms(transaction.redirect_time_ms)}</dd>
            <dt className="text-ink-500">Hosted page load</dt>
            <dd className="tabular">{ms(transaction.page_load_time_ms)}</dd>
            <dt className="text-ink-500">Webhook received</dt>
            <dd className="tabular">
              {transaction.webhook_received_at
                ? `${dateTime(transaction.webhook_received_at)} (${ms(transaction.webhook_latency_ms)} after start)`
                : '—'}
            </dd>
            {transaction.gateway_response_code && (
              <>
                <dt className="text-ink-500">Gateway response</dt>
                <dd className="font-mono text-xs">
                  {transaction.gateway_response_code} ·{' '}
                  {transaction.gateway_response_message ?? ''}
                </dd>
              </>
            )}
            {transaction.error_category && (
              <>
                <dt className="text-ink-500">Error category</dt>
                <dd className="text-red-600">{transaction.error_category}</dd>
              </>
            )}
          </dl>
          {transaction.error_message && (
            <p className="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-xs
                          text-red-700">
              {transaction.error_message}
            </p>
          )}
        </Card>

        <Card title="Browser measurements">
          {browser_measurements.length === 0 ? (
            <EmptyState title="No browser metrics reported"
                        description="Hosted pages on another origin can withhold most Performance API timings. What the browser could not see is left blank rather than estimated." />
          ) : (
            <table className="table">
              <thead>
                <tr><th>Metric</th><th className="text-right">Value</th><th>Scope</th></tr>
              </thead>
              <tbody>
                {browser_measurements.map((metric) => (
                  <tr key={metric.id}>
                    <td>{metric.metric_name}</td>
                    <td className="num">{msExact(metric.value_ms, 1)}</td>
                    <td className="text-xs text-ink-500">{metric.origin_scope}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </div>
  )
}
