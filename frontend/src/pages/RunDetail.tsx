/** One benchmark run: live progress plus the resulting statistics. */

import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import type { BenchmarkRun } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { Card, ErrorNotice, SampleSize, Spinner, StatusBadge } from '../components/ui'
import { dateTime, integrationLabel, money, ms, percent } from '../lib/format'

export default function RunDetail() {
  const { id = '' } = useParams()
  const { isAdmin } = useAuth()
  const [run, setRun] = useState<BenchmarkRun | null>(null)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(() => {
    api.run(id).then(setRun).catch(setError)
  }, [id])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    // Poll only while the run is actually moving; a finished run needs no refresh.
    if (!run || !['QUEUED', 'RUNNING'].includes(run.status)) return
    const timer = window.setInterval(load, 2000)
    return () => window.clearInterval(timer)
  }, [run, load])

  if (error) return <ErrorNotice error={error} onRetry={load} />
  if (!run) return <Spinner label="Loading run" />

  const progress = run.progress
  const stats = run.statistics
  const metadata = run.run_metadata as Record<string, unknown>

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <Link to="/transactions" className="text-sm text-accent-600 hover:underline">
            ← Transactions
          </Link>
          <h1 className="mt-1 text-2xl font-semibold text-ink-900">{run.name}</h1>
          <p className="text-sm text-ink-500">
            {run.gateway_code} · {integrationLabel(run.integration_type)} ·{' '}
            {money(run.amount, run.currency)} · {run.transaction_count} transactions
            every {run.interval_seconds}s
          </p>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge status={run.status} />
          {isAdmin && ['QUEUED', 'RUNNING'].includes(run.status) && (
            <button className="btn-secondary"
                    onClick={async () => { await api.cancelRun(run.id); load() }}>
              Cancel
            </button>
          )}
        </div>
      </header>

      {progress && (
        <Card title="Progress">
          <div className="mb-2 flex items-center justify-between text-sm text-ink-600">
            <span>{progress.completed} of {progress.total} transactions</span>
            <span>{progress.succeeded} succeeded</span>
          </div>
          <div className="h-3 overflow-hidden rounded-full bg-ink-100">
            <div className="h-full rounded-full bg-accent-500 transition-all"
                 style={{ width: `${progress.percent}%` }} />
          </div>
        </Card>
      )}

      {run.error_message && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm
                      text-amber-800">{run.error_message}</p>
      )}

      {stats && stats.api && (
        <Card title="Results">
          <div className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr>
                  <th className="text-right">Transactions</th>
                  <th className="text-right">Success</th><th className="text-right">Mean API</th>
                  <th className="text-right">P50</th><th className="text-right">P95</th>
                  <th className="text-right">P99</th><th className="text-right">Std dev</th>
                  <th className="text-right">Mean total</th><th>Sample</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="num">{stats.transactions}</td>
                  <td className="num">{percent(stats.success_rate)}</td>
                  <td className="num">{ms(stats.api.mean)}</td>
                  <td className="num">{ms(stats.api.p50)}</td>
                  <td className="num">{ms(stats.api.p95)}</td>
                  <td className="num">{ms(stats.api.p99)}</td>
                  <td className="num">{ms(stats.api.stdev)}</td>
                  <td className="num">{ms(stats.total.mean)}</td>
                  <td><SampleSize stats={stats.api} /></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div className="mt-4">
            <Link to={`/transactions?benchmark_run_id=${run.id}`}
                  className="text-sm text-accent-600 hover:underline">
              View this run’s transactions →
            </Link>
          </div>
        </Card>
      )}

      <Card title="Run metadata">
        <p className="mb-3 text-xs text-ink-500">
          Recorded so two runs from different dates or hosts can be told apart before
          their numbers are compared.
        </p>
        <dl className="grid grid-cols-2 gap-y-2 text-sm sm:grid-cols-3">
          {Object.entries(metadata).map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="text-ink-500">{key.replace(/_/g, ' ')}</dt>
              <dd className="break-all sm:col-span-2">
                {typeof value === 'object' ? JSON.stringify(value) : String(value)}
              </dd>
            </div>
          ))}
          <dt className="text-ink-500">started</dt>
          <dd className="sm:col-span-2">{dateTime(run.started_at)}</dd>
          <dt className="text-ink-500">completed</dt>
          <dd className="sm:col-span-2">{dateTime(run.completed_at)}</dd>
        </dl>
      </Card>
    </div>
  )
}
