/** Reports and exports (specification sections 27 and 28). */

import { useCallback, useEffect, useState } from 'react'

import { api, downloadExport } from '../api/client'
import type { ComparisonRow, HppRow } from '../api/types'
import { Card, EmptyState, ErrorNotice, Note, SampleSize, Spinner } from '../components/ui'
import { count, integrationLabel, ms, percent } from '../lib/format'

interface BreakdownRow {
  gateway_code: string
  integration_type: string
  operation_name: string
  normalized_operation: string
  http_method: string
  count: number
  mean: number | null
  median: number | null
  p95: number | null
  p99: number | null
  max: number | null
  success_rate: number | null
  reliable: boolean
}

export default function Reports() {
  const [summary, setSummary] = useState<ComparisonRow[] | null>(null)
  const [breakdown, setBreakdown] = useState<BreakdownRow[] | null>(null)
  const [hpp, setHpp] = useState<HppRow[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [days, setDays] = useState(30)
  const [exporting, setExporting] = useState(false)

  const load = useCallback(() => {
    const dateFrom = new Date(Date.now() - days * 86_400_000).toISOString()
    setSummary(null)
    Promise.all([
      api.gatewaySummary({ date_from: dateFrom }),
      api.apiBreakdown({ date_from: dateFrom }),
      api.hppPerformance({ date_from: dateFrom }),
    ]).then(([summaryResult, breakdownResult, hppResult]) => {
      setSummary(summaryResult.rows)
      setBreakdown(breakdownResult.rows as unknown as BreakdownRow[])
      setHpp(hppResult.rows)
    }).catch(setError)
  }, [days])

  useEffect(() => { load() }, [load])

  async function handleExport(format: 'csv' | 'excel' | 'json', dataset: string) {
    setExporting(true)
    try {
      const extension = format === 'excel' ? 'xlsx' : format
      await downloadExport(
        { format, dataset, date_from: new Date(Date.now() - days * 86_400_000).toISOString() },
        `burapay-${dataset}.${extension}`)
    } finally {
      setExporting(false)
    }
  }

  if (error) return <ErrorNotice error={error} onRetry={load} />

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">Reports</h1>
          <p className="text-sm text-ink-500">
            Gateway summary, per-operation breakdown and hosted checkout performance.
          </p>
        </div>
        <select className="input w-auto" value={days}
                onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
          <option value={365}>Last year</option>
        </select>
      </header>

      <Card title="Export">
        <div className="flex flex-wrap gap-2">
          <button className="btn-secondary" disabled={exporting}
                  onClick={() => handleExport('excel', 'all')}>
            Excel — everything
          </button>
          <button className="btn-secondary" disabled={exporting}
                  onClick={() => handleExport('csv', 'summary')}>
            CSV — gateway summary
          </button>
          <button className="btn-secondary" disabled={exporting}
                  onClick={() => handleExport('csv', 'api_breakdown')}>
            CSV — API breakdown
          </button>
          <button className="btn-secondary" disabled={exporting}
                  onClick={() => handleExport('csv', 'transactions')}>
            CSV — transactions
          </button>
          <button className="btn-secondary" disabled={exporting}
                  onClick={() => handleExport('json', 'all')}>
            JSON — everything
          </button>
        </div>
        <div className="mt-4">
          <Note>
            Every export carries the filters that produced it and the sample size behind
            each figure, so a table pasted into a document can still be checked against
            what it was measuring.
          </Note>
        </div>
      </Card>

      <Card title="Gateway summary">
        {!summary ? <Spinner /> : summary.length === 0 ? (
          <EmptyState title="No transactions in this period" />
        ) : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>Gateway</th><th>Integration</th>
                  <th className="text-right">Transactions</th>
                  <th className="text-right">Success rate</th>
                  <th className="text-right">Avg API</th>
                  <th className="text-right">P95</th><th className="text-right">P99</th>
                  <th className="text-right">Avg complete</th><th>Sample</th>
                </tr>
              </thead>
              <tbody>
                {summary.map((row) => (
                  <tr key={`${row.gateway_code}-${row.integration_type}`}>
                    <td className="font-medium">{row.gateway_name}</td>
                    <td>{integrationLabel(row.integration_type)}</td>
                    <td className="num">{count(row.transactions)}</td>
                    <td className="num">{percent(row.success_rate)}</td>
                    <td className="num">{ms(row.api.mean)}</td>
                    <td className="num">{ms(row.api.p95)}</td>
                    <td className="num">{ms(row.api.p99)}</td>
                    <td className="num">{ms(row.total.mean)}</td>
                    <td><SampleSize stats={row.api} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Gateway API breakdown">
        {!breakdown ? <Spinner /> : breakdown.length === 0 ? (
          <EmptyState title="No API calls recorded in this period" />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="table">
                <thead>
                  <tr>
                    <th>Gateway</th><th>Integration</th><th>Operation</th>
                    <th>Normalized</th><th className="text-right">Calls</th>
                    <th className="text-right">Mean</th><th className="text-right">Median</th>
                    <th className="text-right">P95</th><th className="text-right">P99</th>
                    <th className="text-right">Max</th><th className="text-right">Success</th>
                  </tr>
                </thead>
                <tbody>
                  {breakdown.map((row, index) => (
                    <tr key={`${row.gateway_code}-${row.operation_name}-${index}`}>
                      <td className="font-medium">{row.gateway_code}</td>
                      <td>{integrationLabel(row.integration_type)}</td>
                      <td className="max-w-[20rem] truncate font-mono text-xs">
                        {row.operation_name}
                      </td>
                      <td className="text-xs text-ink-500">
                        {row.normalized_operation.replace(/_/g, ' ')}
                      </td>
                      <td className="num">{count(row.count)}</td>
                      <td className="num">{ms(row.mean)}</td>
                      <td className="num">{ms(row.median)}</td>
                      <td className="num">{ms(row.p95)}</td>
                      <td className="num">{ms(row.p99)}</td>
                      <td className="num">{ms(row.max)}</td>
                      <td className="num">{percent(row.success_rate)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-4">
              <Note>
                Operations are grouped by the gateway’s own name with the normalized
                category beside it. A Stripe PaymentIntent and an Adyen /payments call are
                different operations that both map to payment initiation; flattening them
                together would hide the difference this report exists to show.
              </Note>
            </div>
          </>
        )}
      </Card>

      <Card title="Hosted checkout performance">
        {!hpp ? <Spinner /> : hpp.length === 0 ? (
          <EmptyState title="No hosted checkout transactions in this period" />
        ) : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>Gateway</th><th className="text-right">Session</th>
                  <th className="text-right">Redirect</th><th className="text-right">Page load</th>
                  <th className="text-right">Payment</th><th className="text-right">3DS</th>
                  <th className="text-right">Callback</th><th className="text-right">Total</th>
                </tr>
              </thead>
              <tbody>
                {hpp.map((row) => (
                  <tr key={row.gateway_code}>
                    <td className="font-medium">{row.gateway_name}</td>
                    <td className="num">{ms(row.session_creation.mean)}</td>
                    <td className="num">{ms(row.redirect.mean)}</td>
                    <td className="num">{ms(row.page_load.mean)}</td>
                    <td className="num">{ms(row.customer_interaction.mean)}</td>
                    <td className="num">{ms(row.three_ds.mean)}</td>
                    <td className="num">{ms(row.webhook.mean)}</td>
                    <td className="num">{ms(row.total.mean)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
