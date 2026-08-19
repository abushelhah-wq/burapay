/** One gateway comparison test (specification section 17). */

import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import type { BenchmarkRun, ComparisonRow, ComparisonTest, Ranking } from '../api/types'
import { MetricBarChart } from '../components/Charts'
import { Card, EmptyState, ErrorNotice, Note, SampleSize, Spinner,
         StatusBadge } from '../components/ui'
import { count, integrationLabel, money, ms, percent } from '../lib/format'

interface Payload {
  test: ComparisonTest
  runs: BenchmarkRun[]
  results: ComparisonRow[]
  ranking: Ranking
  progress: Record<string, { completed: number; total: number }>
}

export default function ComparisonTestDetail() {
  const { id = '' } = useParams()
  const [data, setData] = useState<Payload | null>(null)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(() => {
    api.comparisonTest(id).then(setData).catch(setError)
  }, [id])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    if (!data || !['QUEUED', 'RUNNING'].includes(data.test.status)) return
    const timer = window.setInterval(load, 3000)
    return () => window.clearInterval(timer)
  }, [data, load])

  if (error) return <ErrorNotice error={error} onRetry={load} />
  if (!data) return <Spinner label="Loading comparison test" />

  const { test, runs, results, ranking, progress } = data

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link to="/comparison" className="text-sm text-accent-600 hover:underline">
            ← Comparison
          </Link>
          <h1 className="mt-1 text-2xl font-semibold text-ink-900">{test.name}</h1>
          <p className="text-sm text-ink-500">
            {integrationLabel(test.integration_type)} · {money(test.amount, test.currency)} ·{' '}
            {test.transactions_per_gateway} transactions per gateway ·{' '}
            {test.gateway_codes.join(', ')}
          </p>
        </div>
        <StatusBadge status={test.status} />
      </header>

      <Card title="Progress">
        <ul className="space-y-3">
          {runs.map((run) => {
            const runProgress = progress[run.id] ?? { completed: 0, total: run.transaction_count }
            const percentDone = runProgress.total
              ? (runProgress.completed / runProgress.total) * 100 : 0
            return (
              <li key={run.id}>
                <div className="mb-1 flex items-center justify-between text-sm">
                  <Link to={`/runs/${run.id}`} className="text-accent-600 hover:underline">
                    {run.name}
                  </Link>
                  <span className="text-ink-500">
                    {runProgress.completed}/{runProgress.total} · {run.status}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-ink-100">
                  <div className="h-full rounded-full bg-accent-500"
                       style={{ width: `${percentDone}%` }} />
                </div>
              </li>
            )
          })}
        </ul>
      </Card>

      <Card title="Mean gateway API time">
        {results.length ? (
          <MetricBarChart data={results.map((row) => ({
            label: row.gateway_name, value: row.api.mean,
            sampleSize: row.api.count, simulated: row.is_simulated }))} />
        ) : <EmptyState title="No results yet" />}
      </Card>

      <Card title="Results">
        {results.length === 0 ? <EmptyState title="No completed transactions yet" /> : (
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th>Gateway</th><th className="text-right">Transactions</th>
                  <th className="text-right">Mean API</th><th className="text-right">P50</th>
                  <th className="text-right">P95</th><th className="text-right">P99</th>
                  <th className="text-right">Mean total</th>
                  <th className="text-right">Success</th><th>Sample</th>
                </tr>
              </thead>
              <tbody>
                {results.map((row) => (
                  <tr key={`${row.gateway_code}-${row.integration_type}`}>
                    <td className="font-medium">{row.gateway_name}</td>
                    <td className="num">{count(row.transactions)}</td>
                    <td className="num">{ms(row.api.mean)}</td>
                    <td className="num">{ms(row.api.p50)}</td>
                    <td className="num">{ms(row.api.p95)}</td>
                    <td className="num">{ms(row.api.p99)}</td>
                    <td className="num">{ms(row.total.mean)}</td>
                    <td className="num">{percent(row.success_rate)}</td>
                    <td><SampleSize stats={row.api} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title={ranking.label}>
        {ranking.entries.length === 0 ? (
          <EmptyState title="No ranking yet" description={ranking.note} />
        ) : (
          <>
            <ol className="space-y-2">
              {ranking.entries.map((entry) => (
                <li key={entry.gateway_code}
                    className="flex items-center justify-between rounded-lg border
                               border-ink-200 px-4 py-3">
                  <span className="flex items-center gap-3">
                    <span className="flex h-7 w-7 items-center justify-center rounded-full
                                     bg-accent-600 text-xs font-semibold text-white">
                      {entry.rank}
                    </span>
                    <span className="font-medium text-ink-900">{entry.gateway_name}</span>
                    <span className="text-xs text-ink-500">n={entry.sample_size}</span>
                  </span>
                  <span className="tabular font-mono text-lg font-semibold text-ink-900">
                    {entry.score.toFixed(1)}
                  </span>
                </li>
              ))}
            </ol>
            <div className="mt-4"><Note>{ranking.note}</Note></div>
          </>
        )}
      </Card>
    </div>
  )
}
