/**
 * The comparison dashboard (specification sections 13, 14, 15, 18).
 *
 * Three tabs over the same filtered set: the API comparison, the HPP breakdown, and
 * HPP against Direct for each gateway. The ranking sits underneath, and refuses to
 * rank anything without enough samples.
 */

import { useCallback, useEffect, useState } from 'react'

import { api } from '../api/client'
import type { ComparisonRow, Gateway, HppRow, Ranking } from '../api/types'
import { MetricBarChart, TimeSeriesChart } from '../components/Charts'
import type { SeriesPoint } from '../components/Charts'
import { Badge, Card, EmptyState, ErrorNotice, Note, SampleSize, Spinner } from '../components/ui'
import { count, integrationLabel, ms, percent } from '../lib/format'

type Tab = 'api' | 'hpp' | 'integrations'

export default function Comparison() {
  const [tab, setTab] = useState<Tab>('api')
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [rows, setRows] = useState<ComparisonRow[] | null>(null)
  const [note, setNote] = useState('')
  const [hppRows, setHppRows] = useState<HppRow[] | null>(null)
  const [hppNote, setHppNote] = useState('')
  const [integrations, setIntegrations] = useState<
    { gateway_code: string; gateway_name: string; hpp: ComparisonRow | null;
      direct: ComparisonRow | null; api_delta_ms: number | null }[] | null>(null)
  const [ranking, setRanking] = useState<Ranking | null>(null)
  const [series, setSeries] = useState<SeriesPoint[]>([])
  const [seriesNames, setSeriesNames] = useState<string[]>([])
  const [error, setError] = useState<unknown>(null)

  const [integration, setIntegration] = useState('')
  const [currency, setCurrency] = useState('')
  const [days, setDays] = useState(30)
  const [includeSimulated, setIncludeSimulated] = useState(false)

  useEffect(() => { api.gateways().then(setGateways).catch(() => setGateways([])) }, [])

  const load = useCallback(() => {
    const dateFrom = new Date(Date.now() - days * 86_400_000).toISOString()
    const filters = {
      integration_type: integration || undefined,
      currency: currency || undefined,
      date_from: dateFrom,
    }
    setRows(null)
    setHppRows(null)
    setIntegrations(null)

    Promise.all([
      api.comparison({ ...filters, include_simulated: includeSimulated }),
      api.hppComparison({ currency: currency || undefined, date_from: dateFrom }),
      api.integrationComparison({ currency: currency || undefined, date_from: dateFrom }),
      api.ranking({ ...filters }),
      api.timeseries({ ...filters, bucket: days > 7 ? 'day' : 'hour' }),
    ]).then(([comparison, hpp, integrationRows, rankingResult, timeseries]) => {
      setRows(comparison.rows)
      setNote(comparison.note)
      setHppRows(hpp.rows)
      setHppNote(hpp.note)
      setIntegrations(integrationRows.rows)
      setRanking(rankingResult)

      const names = new Set<string>()
      const buckets = new Map<string, SeriesPoint>()
      for (const point of timeseries.series) {
        const name = `${point.gateway_code} ${point.integration_type}`
        names.add(name)
        const existing = buckets.get(point.bucket) ?? { bucket: point.bucket }
        existing[name] = point.mean
        buckets.set(point.bucket, existing)
      }
      setSeriesNames([...names])
      setSeries([...buckets.values()].sort((a, b) => a.bucket.localeCompare(b.bucket)))
    }).catch(setError)
  }, [integration, currency, days, includeSimulated])

  useEffect(() => { load() }, [load])

  if (error) return <ErrorNotice error={error} onRetry={load} />

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">Comparison</h1>
        <p className="text-sm text-ink-500">
          Gateway performance side by side, with the sample size behind every figure.
        </p>
      </header>

      <Card title="Filters">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label className="label" htmlFor="c-integration">Integration</label>
            <select id="c-integration" className="input" value={integration}
                    onChange={(e) => setIntegration(e.target.value)}>
              <option value="">All</option>
              <option value="hpp">HPP / Hosted Checkout</option>
              <option value="direct">Direct API</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="c-currency">Currency</label>
            <input id="c-currency" className="input" value={currency} placeholder="All"
                   onChange={(e) => setCurrency(e.target.value.toUpperCase())} />
          </div>
          <div>
            <label className="label" htmlFor="c-days">Period</label>
            <select id="c-days" className="input" value={days}
                    onChange={(e) => setDays(Number(e.target.value))}>
              <option value={1}>Last 24 hours</option>
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-2 text-sm text-ink-700">
              <input type="checkbox" checked={includeSimulated}
                     onChange={(e) => setIncludeSimulated(e.target.checked)} />
              Include the MockPay simulator
            </label>
          </div>
        </div>
      </Card>

      <div className="flex flex-wrap gap-2">
        {([['api', 'Gateway API'], ['hpp', 'Hosted checkout'],
           ['integrations', 'HPP vs Direct']] as [Tab, string][]).map(([value, label]) => (
          <button key={value} type="button" onClick={() => setTab(value)}
                  className={tab === value ? 'btn-primary' : 'btn-secondary'}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'api' && (
        <>
          <div className="grid gap-6 lg:grid-cols-2">
            <Card title="Mean gateway API time">
              {rows ? (
                <MetricBarChart data={rows.map((row) => ({
                  label: `${row.gateway_name} ${row.integration_type}`,
                  value: row.api.mean, sampleSize: row.api.count,
                  simulated: row.is_simulated }))} />
              ) : <Spinner />}
            </Card>
            <Card title="P95 gateway API time">
              {rows ? (
                <MetricBarChart data={rows.map((row) => ({
                  label: `${row.gateway_name} ${row.integration_type}`,
                  value: row.api.p95, sampleSize: row.api.count,
                  simulated: row.is_simulated }))} />
              ) : <Spinner />}
            </Card>
            <Card title="Total transaction duration">
              {rows ? (
                <MetricBarChart data={rows.map((row) => ({
                  label: `${row.gateway_name} ${row.integration_type}`,
                  value: row.total.mean, sampleSize: row.total.count,
                  simulated: row.is_simulated }))} />
              ) : <Spinner />}
            </Card>
            <Card title="Success rate">
              {rows ? (
                <MetricBarChart unit="percent" data={rows.map((row) => ({
                  label: `${row.gateway_name} ${row.integration_type}`,
                  value: row.success_rate, sampleSize: row.transactions,
                  simulated: row.is_simulated }))} />
              ) : <Spinner />}
            </Card>
          </div>

          <Card title="Response time over time">
            {series.length ? <TimeSeriesChart data={series} series={seriesNames} />
                           : <EmptyState title="No completed transactions in this period" />}
          </Card>

          <Card title="Comparison table">
            {!rows ? <Spinner /> : rows.length === 0 ? (
              <EmptyState title="Nothing to compare yet"
                          description="Run a benchmark against at least one gateway." />
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Gateway</th><th>Integration</th>
                        <th className="text-right">Transactions</th>
                        <th className="text-right">Avg API</th><th className="text-right">P50</th>
                        <th className="text-right">P90</th><th className="text-right">P95</th>
                        <th className="text-right">P99</th><th className="text-right">Std dev</th>
                        <th className="text-right">Avg total</th>
                        <th className="text-right">Success</th><th>Calls</th><th>Sample</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <tr key={`${row.gateway_code}-${row.integration_type}`}>
                          <td className="font-medium">
                            {row.gateway_name}{' '}
                            {row.is_simulated && <Badge tone="warn">simulator</Badge>}
                          </td>
                          <td>{integrationLabel(row.integration_type)}</td>
                          <td className="num">{count(row.transactions)}</td>
                          <td className="num">{ms(row.api.mean)}</td>
                          <td className="num">{ms(row.api.p50)}</td>
                          <td className="num">{ms(row.api.p90)}</td>
                          <td className="num">{ms(row.api.p95)}</td>
                          <td className="num">{ms(row.api.p99)}</td>
                          <td className="num">{ms(row.api.stdev)}</td>
                          <td className="num">{ms(row.total.mean)}</td>
                          <td className="num">{percent(row.success_rate)}</td>
                          <td className="text-xs text-ink-600">
                            {row.api_call_count ?? (row.api_call_count_range.join('–') || '—')}
                            {row.documented_calls && (
                              <span className="block text-[11px] text-ink-400">
                                doc: {row.documented_calls}
                              </span>
                            )}
                          </td>
                          <td><SampleSize stats={row.api} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="mt-4"><Note>{note}</Note></div>
              </>
            )}
          </Card>
        </>
      )}

      {tab === 'hpp' && (
        <Card title="Hosted checkout breakdown">
          {!hppRows ? <Spinner /> : hppRows.length === 0 ? (
            <EmptyState title="No hosted checkout transactions yet"
                        description="Start a single HPP transaction and complete the payment to populate this view." />
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Gateway</th><th className="text-right">Transactions</th>
                      <th className="text-right">Session creation</th>
                      <th className="text-right">Redirect</th>
                      <th className="text-right">Page load</th>
                      <th className="text-right">Customer</th>
                      <th className="text-right">3DS</th>
                      <th className="text-right">Callback</th>
                      <th className="text-right">Gateway API</th>
                      <th className="text-right">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {hppRows.map((row) => (
                      <tr key={row.gateway_code}>
                        <td className="font-medium">{row.gateway_name}</td>
                        <td className="num">{count(row.transactions)}</td>
                        <td className="num">{ms(row.session_creation.mean)}</td>
                        <td className="num">{ms(row.redirect.mean)}</td>
                        <td className="num">{ms(row.page_load.mean)}</td>
                        <td className="num">{ms(row.customer_interaction.mean)}</td>
                        <td className="num">{ms(row.three_ds.mean)}</td>
                        <td className="num">{ms(row.webhook.mean)}</td>
                        <td className="num">{ms(row.gateway_api.mean)}</td>
                        <td className="num">{ms(row.total.mean)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-4"><Note>{hppNote}</Note></div>
            </>
          )}
        </Card>
      )}

      {tab === 'integrations' && (
        <Card title="HPP against Direct API">
          {!integrations ? <Spinner /> : integrations.length === 0 ? (
            <EmptyState title="No transactions to compare" />
          ) : (
            <div className="overflow-x-auto">
              <table className="table">
                <thead>
                  <tr>
                    <th>Gateway</th>
                    <th className="text-right">HPP mean API</th>
                    <th className="text-right">HPP n</th>
                    <th className="text-right">Direct mean API</th>
                    <th className="text-right">Direct n</th>
                    <th className="text-right">Difference</th>
                  </tr>
                </thead>
                <tbody>
                  {integrations.map((row) => (
                    <tr key={row.gateway_code}>
                      <td className="font-medium">{row.gateway_name}</td>
                      <td className="num">{ms(row.hpp?.api.mean)}</td>
                      <td className="num">{row.hpp?.api.count ?? '—'}</td>
                      <td className="num">{ms(row.direct?.api.mean)}</td>
                      <td className="num">{row.direct?.api.count ?? '—'}</td>
                      <td className={`num ${row.api_delta_ms === null ? ''
                        : row.api_delta_ms > 0 ? 'text-amber-600' : 'text-emerald-600'}`}>
                        {row.api_delta_ms === null ? '—'
                          : `${row.api_delta_ms > 0 ? '+' : ''}${row.api_delta_ms.toFixed(0)} ms`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}

      {ranking && (
        <Card title={ranking.label}>
          {ranking.entries.length === 0 ? (
            <EmptyState title="Not enough data to rank" description={ranking.note} />
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="table">
                  <thead>
                    <tr>
                      <th>#</th><th>Gateway</th><th>Integration</th>
                      <th className="text-right">Score</th>
                      <th className="text-right">API time</th>
                      <th className="text-right">P95</th>
                      <th className="text-right">Completion</th>
                      <th className="text-right">Success</th>
                      <th className="text-right">Sample</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ranking.entries.map((entry) => (
                      <tr key={`${entry.gateway_code}-${entry.integration_type}`}>
                        <td className="num">{entry.rank}</td>
                        <td className="font-medium">{entry.gateway_name}</td>
                        <td>{integrationLabel(entry.integration_type)}</td>
                        <td className="num font-semibold">{entry.score.toFixed(1)}</td>
                        <td className="num">
                          {entry.components.api_response_time?.toFixed(0) ?? '—'}
                        </td>
                        <td className="num">
                          {entry.components.p95_latency?.toFixed(0) ?? '—'}
                        </td>
                        <td className="num">
                          {entry.components.transaction_completion?.toFixed(0) ?? '—'}
                        </td>
                        <td className="num">
                          {entry.components.success_rate?.toFixed(1) ?? '—'}
                        </td>
                        <td className="num">{entry.sample_size}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {ranking.excluded.length > 0 && (
                <p className="mt-3 text-xs text-ink-500">
                  Not ranked:{' '}
                  {ranking.excluded.map((item) =>
                    `${item.gateway_name} (${item.sample_size} transactions)`).join(', ')} —
                  a gateway needs at least {ranking.min_samples} comparable transactions
                  before it is scored.
                </p>
              )}
              <div className="mt-4"><Note>{ranking.note}</Note></div>
            </>
          )}
        </Card>
      )}

      {gateways.length === 0 && (
        <EmptyState title="No gateways configured yet" />
      )}
    </div>
  )
}
