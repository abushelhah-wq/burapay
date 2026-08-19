/** The home page (specification section 47). */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import type { Dashboard as DashboardData } from '../api/types'
import { MetricBarChart } from '../components/Charts'
import { Badge, Card, EmptyState, ErrorNotice, Note, SampleSize, Spinner, StatCard,
         StatusBadge } from '../components/ui'
import { dateTime, integrationLabel, money, ms, percent, relativeTime } from '../lib/format'

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [days, setDays] = useState(30)

  useEffect(() => {
    let cancelled = false
    setData(null)
    api.dashboard(days)
      .then((result) => { if (!cancelled) setData(result) })
      .catch((exc) => { if (!cancelled) setError(exc) })
    return () => { cancelled = true }
  }, [days])

  if (error) return <ErrorNotice error={error} onRetry={() => setDays((d) => d)} />
  if (!data) return <Spinner label="Loading dashboard" />

  const { summary, comparison, latest_runs, recent_transactions, gateway_health } = data
  const realGateways = comparison.filter((row) => !row.is_simulated)

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">Dashboard</h1>
          <p className="text-sm text-ink-500">
            Gateway performance over the last {data.window_days} days.
          </p>
        </div>
        <select className="input w-auto" value={days}
                onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>Last 24 hours</option>
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <StatCard label="Total transactions" value={summary.total_transactions}
                  sub={`${summary.completed_transactions} reached a final state`} />
        <StatCard label="Average API response" value={ms(summary.average_response_ms)}
                  sub={summary.sample_size ? `across ${summary.sample_size} transactions`
                                           : 'no completed transactions yet'} />
        <StatCard label="P95 API response" value={ms(summary.p95_response_ms)}
                  sub={summary.statistically_reliable
                    ? 'sample is large enough to be meaningful'
                    : 'small sample — treat as indicative only'}
                  tone={summary.statistically_reliable ? 'default' : 'warn'} />
        <StatCard label="Success rate" value={percent(summary.success_rate)}
                  tone={summary.success_rate === null ? 'default'
                        : summary.success_rate >= 95 ? 'good' : 'warn'} />
        <StatCard label="Fastest gateway"
                  value={summary.fastest_gateway
                    ? summary.fastest_gateway.gateway_name : '—'}
                  sub={summary.fastest_gateway
                    ? `${ms(summary.fastest_gateway.mean_api_ms)} mean API time, n=${summary.fastest_gateway.sample_size}`
                    : summary.fastest_gateway_note ?? undefined} />
      </div>

      <Card title="Gateway API time — mean and P95"
            action={<Link to="/comparison" className="text-sm text-accent-600
                                                     hover:underline">Full comparison →</Link>}>
        {realGateways.length ? (
          <>
            <div className="grid gap-6 lg:grid-cols-2">
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
                  Mean gateway API time
                </p>
                <MetricBarChart data={realGateways.map((row) => ({
                  label: `${row.gateway_name} ${row.integration_type}`,
                  value: row.api.mean, sampleSize: row.api.count,
                  simulated: row.is_simulated,
                }))} />
              </div>
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-500">
                  P95 gateway API time
                </p>
                <MetricBarChart data={realGateways.map((row) => ({
                  label: `${row.gateway_name} ${row.integration_type}`,
                  value: row.api.p95, sampleSize: row.api.count,
                  simulated: row.is_simulated,
                }))} />
              </div>
            </div>
            <div className="mt-4">
              <Note>
                Gateway API time is the sum of the timed merchant-server calls only. It
                excludes redirects, 3DS and any time a customer spent on a hosted page,
                which are reported separately.
              </Note>
            </div>
          </>
        ) : (
          <EmptyState title="No gateway measurements yet"
                      description="Configure a gateway's sandbox credentials and run a benchmark to populate this chart."
                      action={<Link to="/run" className="btn-primary">Run a benchmark</Link>} />
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Latest benchmark runs">
          {latest_runs.length ? (
            <div className="overflow-x-auto">
              <table className="table">
                <thead>
                  <tr><th>Run</th><th>Integration</th><th>Transactions</th>
                      <th>Status</th><th>Started</th></tr>
                </thead>
                <tbody>
                  {latest_runs.map((run) => (
                    <tr key={run.id}>
                      <td className="max-w-[16rem] truncate">
                        <Link to={`/runs/${run.id}`} className="text-accent-600 hover:underline">
                          {run.name}
                        </Link>
                      </td>
                      <td>{integrationLabel(run.integration_type)}</td>
                      <td className="num">{run.transaction_count}</td>
                      <td><StatusBadge status={run.status} /></td>
                      <td className="text-xs text-ink-500">{relativeTime(run.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <EmptyState title="No benchmark runs yet" />}
        </Card>

        <Card title="Gateway availability"
              action={<Link to="/gateways" className="text-sm text-accent-600
                                                     hover:underline">Details →</Link>}>
          {gateway_health.length ? (
            <ul className="space-y-2">
              {gateway_health.map((row) => (
                <li key={row.gateway_code}
                    className="flex items-center justify-between rounded-lg border
                               border-ink-200 px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className={`h-2.5 w-2.5 rounded-full
                      ${row.available === true ? 'bg-emerald-500'
                        : row.available === false ? 'bg-red-500' : 'bg-ink-300'}`} />
                    <span className="text-sm font-medium text-ink-800">{row.gateway_code}</span>
                  </div>
                  <div className="flex items-center gap-3 text-xs text-ink-500">
                    <span className="tabular font-mono">{ms(row.response_time_ms)}</span>
                    <span>{relativeTime(row.checked_at)}</span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="No health checks recorded"
                        description="Health probes run automatically once a gateway has credentials configured." />
          )}
        </Card>
      </div>

      <Card title="Recent transactions"
            action={<Link to="/transactions" className="text-sm text-accent-600
                                                       hover:underline">All transactions →</Link>}>
        {recent_transactions.length ? (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr><th>Gateway</th><th>Integration</th><th>Amount</th><th>Status</th>
                    <th className="text-right">Gateway API</th>
                    <th className="text-right">Total</th><th>Started</th></tr>
              </thead>
              <tbody>
                {recent_transactions.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <Link to={`/transactions/${row.id}`}
                            className="text-accent-600 hover:underline">
                        {row.gateway_code}
                      </Link>
                    </td>
                    <td>{integrationLabel(row.integration_type ?? '')}</td>
                    <td className="num">{money(row.amount ?? 0, row.currency ?? '')}</td>
                    <td><StatusBadge status={row.status ?? ''} /></td>
                    <td className="num">{ms(row.gateway_api_time_ms)}</td>
                    <td className="num">{ms(row.total_duration_ms)}</td>
                    <td className="text-xs text-ink-500">{dateTime(row.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <EmptyState title="No transactions yet" />}
      </Card>

      {realGateways.length > 0 && (
        <Card title="Comparison summary">
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>Gateway</th><th>Integration</th><th className="text-right">Transactions</th>
                  <th className="text-right">Mean API</th><th className="text-right">P50</th>
                  <th className="text-right">P95</th><th className="text-right">P99</th>
                  <th className="text-right">Success</th><th>Sample</th>
                </tr>
              </thead>
              <tbody>
                {realGateways.map((row) => (
                  <tr key={`${row.gateway_code}-${row.integration_type}`}>
                    <td className="font-medium">
                      {row.gateway_name}{' '}
                      {row.is_simulated && <Badge tone="warn">simulator</Badge>}
                    </td>
                    <td>{integrationLabel(row.integration_type)}</td>
                    <td className="num">{row.transactions}</td>
                    <td className="num">{ms(row.api.mean)}</td>
                    <td className="num">{ms(row.api.p50)}</td>
                    <td className="num">{ms(row.api.p95)}</td>
                    <td className="num">{ms(row.api.p99)}</td>
                    <td className="num">{percent(row.success_rate)}</td>
                    <td><SampleSize stats={row.api} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}
