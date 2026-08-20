/**
 * The call log — every request this platform made to a gateway, and what came back.
 *
 * The transaction pages answer "how did this payment go?". This answers "what does
 * this gateway actually do?" — the question you have when a provider returns nothing
 * but "General error", when two gateways disagree about what a field is called, or
 * when you want a working call side by side with a broken one.
 *
 * Both bodies were sanitized before they were ever stored: card numbers truncated to a
 * last four, CVVs dropped by name, secrets redacted. There is no unsanitized copy for
 * this page to be compared against — the scrubbing happens on the way in.
 */

import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import type { ApiLogEntry, LogSummary, Page as PageOf } from '../api/types'
import { Badge, Card, EmptyState, ErrorNotice, Spinner } from '../components/ui'
import { dateTime, msExact, relativeTime } from '../lib/format'

const PAGE_SIZE = 50

export default function Logs() {
  const [params, setParams] = useSearchParams()
  const [page, setPage] = useState<PageOf<ApiLogEntry> | null>(null)
  const [summary, setSummary] = useState<LogSummary | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  // The URL is the filter state, so a filtered view can be linked to and survives a
  // reload — which matters when you are pasting one into a bug report.
  const gateway = params.get('gateway') ?? ''
  const operation = params.get('operation') ?? ''
  const outcome = params.get('outcome') ?? ''
  const transaction = params.get('transaction') ?? ''
  const search = params.get('search') ?? ''
  const offset = Number(params.get('offset') ?? 0)

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key !== 'offset') next.delete('offset')   // a new filter starts at page one
    setParams(next, { replace: true })
  }

  const load = useCallback(() => {
    api.logs({
      gateway_code: gateway ? [gateway] : undefined,
      normalized_operation: operation || undefined,
      outcome: outcome || undefined,
      transaction_id: transaction || undefined,
      search: search || undefined,
      limit: PAGE_SIZE, offset,
    }).then(setPage).catch(setError)
  }, [gateway, operation, outcome, transaction, search, offset])

  useEffect(load, [load])
  useEffect(() => { api.logSummary().then(setSummary).catch(setError) }, [])

  async function clearLog() {
    if (!window.confirm(
      'Delete every recorded call? Transaction timings are columns on the transaction '
      + 'itself and are not affected, but the request and response bodies are gone.')) return
    setBusy(true)
    setNotice(null)
    try {
      const result = await api.clearLogs()
      setNotice(result.message)
      load()
      setSummary(await api.logSummary())
    } catch (exc) {
      setNotice(exc instanceof ApiError ? exc.message : 'Could not clear the log.')
    } finally {
      setBusy(false)
    }
  }

  if (error) return <ErrorNotice error={error} />
  if (!page) return <Spinner label="Loading the call log" />

  const filtered = Boolean(gateway || operation || outcome || transaction || search)

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">Call log</h1>
          <p className="text-sm text-ink-500">
            Every request sent to a gateway and every response that came back, with the
            time each one took. Card numbers, CVVs and secrets are stripped before
            anything here is stored.
          </p>
        </div>
        {summary && (
          <div className="flex items-center gap-4 text-sm">
            <span className="text-ink-500">
              <strong className="text-ink-900 tabular">{summary.total}</strong> calls
            </span>
            <span className="text-ink-500">
              <strong className={summary.failed ? 'text-red-600 tabular'
                                                : 'text-ink-900 tabular'}>
                {summary.failed}
              </strong> failed
            </span>
            <button type="button" className="btn-secondary" disabled={busy}
                    onClick={clearLog}>
              Clear log
            </button>
          </div>
        )}
      </header>

      {notice && (
        <p className="rounded-lg border border-ink-200 bg-ink-50 p-3 text-sm text-ink-700">
          {notice}
        </p>
      )}

      <Card title="Filters">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label className="label" htmlFor="log-gateway">Gateway</label>
            <select id="log-gateway" className="input" value={gateway}
                    onChange={(e) => setFilter('gateway', e.target.value)}>
              <option value="">All gateways</option>
              {(summary?.gateways ?? []).map((item) => (
                <option key={item.code} value={item.code}>
                  {item.code} ({item.calls})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="log-operation">Operation</label>
            <select id="log-operation" className="input" value={operation}
                    onChange={(e) => setFilter('operation', e.target.value)}>
              <option value="">All operations</option>
              {(summary?.operations ?? []).map((name) => (
                <option key={name} value={name}>{name.replace(/_/g, ' ')}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="log-outcome">Outcome</label>
            <select id="log-outcome" className="input" value={outcome}
                    onChange={(e) => setFilter('outcome', e.target.value)}>
              <option value="">Everything</option>
              <option value="failed">Only failures</option>
              <option value="succeeded">Only successes</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="log-search">Search</label>
            <input id="log-search" className="input" defaultValue={search}
                   placeholder="Endpoint, operation or message"
                   onKeyDown={(e) => {
                     if (e.key === 'Enter') {
                       setFilter('search', (e.target as HTMLInputElement).value.trim())
                     }
                   }} />
            <p className="hint">Press Enter to search.</p>
          </div>
        </div>

        {transaction && (
          <p className="mt-4 flex flex-wrap items-center gap-2 text-sm text-ink-600">
            Showing one transaction:{' '}
            <Link to={`/transactions/${transaction}`}
                  className="font-mono text-xs text-accent-600 hover:underline">
              {transaction}
            </Link>
            <button type="button" className="text-xs font-medium underline"
                    onClick={() => setFilter('transaction', '')}>
              show all
            </button>
          </p>
        )}
      </Card>

      {page.items.length === 0 ? (
        <EmptyState
          title="No calls recorded"
          description={filtered
            ? 'Nothing matches these filters. Widen them, or clear the search.'
            : 'Run a transaction and every call it makes will be listed here.'} />
      ) : (
        <Card title={`${page.total} call${page.total === 1 ? '' : 's'}`}>
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Gateway</th>
                  <th>Operation</th>
                  <th className="num">Time</th>
                  <th>Result</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {page.items.map((call) => {
                  const open = expanded === call.id
                  return (
                    <Fragment key={call.id}>
                      <tr className={open ? 'bg-ink-50' : undefined}>
                        <td className="whitespace-nowrap">
                          <span title={dateTime(call.started_at)}>
                            {relativeTime(call.started_at)}
                          </span>
                        </td>
                        <td>
                          <span className="font-medium text-ink-900">
                            {call.gateway_code}
                          </span>
                          <span className="ml-2 text-xs text-ink-500">
                            {call.integration_type}
                          </span>
                        </td>
                        <td>
                          <span className="font-mono text-xs">{call.endpoint}</span>
                          <span className="ml-2 text-[11px] uppercase tracking-wide
                                           text-ink-400">
                            {call.normalized_operation.replace(/_/g, ' ')}
                          </span>
                          {call.is_setup_call && (
                            <span className="ml-2"><Badge>setup</Badge></span>
                          )}
                        </td>
                        <td className="num tabular">{msExact(call.duration_ms)}</td>
                        <td className="whitespace-nowrap">
                          {call.timed_out ? (
                            <Badge tone="warn">timed out</Badge>
                          ) : call.success ? (
                            <Badge tone="good">{call.http_status ?? 'ok'}</Badge>
                          ) : (
                            <Badge tone="warn">{call.http_status ?? 'failed'}</Badge>
                          )}
                          {call.gateway_response_code && (
                            <span className="ml-2 font-mono text-xs text-ink-500">
                              {call.gateway_response_code}
                            </span>
                          )}
                        </td>
                        <td className="whitespace-nowrap text-right">
                          <button type="button"
                                  className="text-xs font-medium text-accent-600
                                             hover:underline"
                                  onClick={() => setExpanded(open ? null : call.id)}>
                            {open ? 'Hide' : 'Details'}
                          </button>
                        </td>
                      </tr>
                      {open && (
                        <tr>
                          <td colSpan={6} className="bg-ink-50 !py-4">
                            <div className="space-y-4">
                              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs
                                              text-ink-600">
                                <span>{call.http_method} · {call.operation_name}</span>
                                <span>started {dateTime(call.started_at)}</span>
                                {(call.request_size_bytes !== null
                                  || call.response_size_bytes !== null) && (
                                  <span>
                                    {call.request_size_bytes ?? '—'} B out ·{' '}
                                    {call.response_size_bytes ?? '—'} B back
                                  </span>
                                )}
                                <Link to={`/transactions/${call.transaction_id}`}
                                      className="font-medium text-accent-600 hover:underline">
                                  {call.merchant_reference}
                                </Link>
                              </div>

                              {call.gateway_response_message && (
                                <p className={call.success ? 'text-xs text-ink-700'
                                                           : 'text-xs text-red-700'}>
                                  {call.gateway_response_message}
                                </p>
                              )}
                              {call.error_message && (
                                <p className="text-xs text-red-700">{call.error_message}</p>
                              )}

                              <div className="grid gap-4 lg:grid-cols-2">
                                <Body label="Request" text={call.request_snippet} />
                                <Body label="Response" text={call.response_snippet} />
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>

          {page.total > PAGE_SIZE && (
            <div className="mt-4 flex items-center justify-between text-sm">
              <span className="text-ink-500">
                {offset + 1}–{Math.min(offset + PAGE_SIZE, page.total)} of {page.total}
              </span>
              <div className="flex gap-2">
                <button type="button" className="btn-secondary" disabled={offset === 0}
                        onClick={() => setFilter('offset',
                          String(Math.max(0, offset - PAGE_SIZE)))}>
                  Previous
                </button>
                <button type="button" className="btn-secondary"
                        disabled={offset + PAGE_SIZE >= page.total}
                        onClick={() => setFilter('offset', String(offset + PAGE_SIZE))}>
                  Next
                </button>
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  )
}

/** One body, pretty-printed when it is JSON and left alone when it is not. */
function Body({ label, text }: { label: string; text: string | null }) {
  if (!text) {
    return (
      <div>
        <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          {label}
        </p>
        <p className="mt-2 text-xs text-ink-400">Not recorded.</p>
      </div>
    )
  }
  let display = text
  try {
    display = JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    // Not JSON — an HTML error page, a form post, a truncated blob. Show it as it is.
  }
  return (
    <div>
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">
          {label}
        </p>
        <button type="button" className="text-xs text-ink-500 hover:underline"
                onClick={() => navigator.clipboard?.writeText(display)}>
          Copy
        </button>
      </div>
      <pre className="mt-2 max-h-72 overflow-auto rounded bg-ink-900 p-3 text-[11px]
                      leading-relaxed text-ink-100">
{display}
      </pre>
    </div>
  )
}
