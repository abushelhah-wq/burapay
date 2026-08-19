/** The searchable transaction list (specification section 44). */

import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { api, downloadExport } from '../api/client'
import type { Gateway, Page, Transaction } from '../api/types'
import { Card, EmptyState, ErrorNotice, Spinner, StatusBadge } from '../components/ui'
import { dateTime, integrationLabel, money, ms } from '../lib/format'

const STATUSES = ['SUCCESS', 'DECLINED', 'FAILED', 'ERROR', 'TIMEOUT', 'CANCELLED',
                  'PENDING', 'IN_PROGRESS']
const PAGE_SIZE = 25

export default function Transactions() {
  const [params, setParams] = useSearchParams()
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [page, setPage] = useState<Page<Transaction> | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [offset, setOffset] = useState(0)

  const filters = {
    gateway_code: params.get('gateway_code') ?? '',
    integration_type: params.get('integration_type') ?? '',
    status: params.get('status') ?? '',
    currency: params.get('currency') ?? '',
    benchmark_run_id: params.get('benchmark_run_id') ?? '',
    merchant_reference: params.get('merchant_reference') ?? '',
    gateway_transaction_id: params.get('gateway_transaction_id') ?? '',
    date_from: params.get('date_from') ?? '',
    date_to: params.get('date_to') ?? '',
  }

  useEffect(() => { api.gateways().then(setGateways).catch(() => setGateways([])) }, [])

  const load = useCallback(() => {
    setPage(null)
    api.transactions({
      ...(filters.gateway_code ? { gateway_code: [filters.gateway_code] } : {}),
      integration_type: filters.integration_type || undefined,
      status: filters.status || undefined,
      currency: filters.currency || undefined,
      benchmark_run_id: filters.benchmark_run_id || undefined,
      merchant_reference: filters.merchant_reference || undefined,
      gateway_transaction_id: filters.gateway_transaction_id || undefined,
      date_from: filters.date_from || undefined,
      date_to: filters.date_to || undefined,
      limit: PAGE_SIZE, offset,
    }).then(setPage).catch(setError)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.toString(), offset])

  useEffect(() => { load() }, [load])

  function update(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next)
    setOffset(0)
  }

  async function handleExport(format: 'csv' | 'excel' | 'json') {
    const extension = format === 'excel' ? 'xlsx' : format
    await downloadExport(
      { format, dataset: 'transactions', ...Object.fromEntries(params) },
      `burapay-transactions.${extension}`)
  }

  if (error) return <ErrorNotice error={error} onRetry={load} />

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">Transactions</h1>
          <p className="text-sm text-ink-500">
            Every benchmarked transaction, with the measured gateway API time next to the
            end-to-end duration.
          </p>
        </div>
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={() => handleExport('csv')}>CSV</button>
          <button className="btn-secondary" onClick={() => handleExport('excel')}>Excel</button>
          <button className="btn-secondary" onClick={() => handleExport('json')}>JSON</button>
        </div>
      </header>

      <Card title="Filters">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label className="label" htmlFor="f-gateway">Gateway</label>
            <select id="f-gateway" className="input" value={filters.gateway_code}
                    onChange={(e) => update('gateway_code', e.target.value)}>
              <option value="">All gateways</option>
              {gateways.map((g) => <option key={g.code} value={g.code}>{g.name}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="f-integration">Integration</label>
            <select id="f-integration" className="input" value={filters.integration_type}
                    onChange={(e) => update('integration_type', e.target.value)}>
              <option value="">All</option>
              <option value="hpp">HPP / Hosted Checkout</option>
              <option value="direct">Direct API</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="f-status">Status</label>
            <select id="f-status" className="input" value={filters.status}
                    onChange={(e) => update('status', e.target.value)}>
              <option value="">All</option>
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="f-currency">Currency</label>
            <input id="f-currency" className="input" value={filters.currency}
                   placeholder="SAR" onChange={(e) => update('currency', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="f-reference">Order / reference ID</label>
            <input id="f-reference" className="input" value={filters.merchant_reference}
                   onChange={(e) => update('merchant_reference', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="f-gwid">Gateway transaction ID</label>
            <input id="f-gwid" className="input" value={filters.gateway_transaction_id}
                   onChange={(e) => update('gateway_transaction_id', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="f-from">From</label>
            <input id="f-from" className="input" type="datetime-local" value={filters.date_from}
                   onChange={(e) => update('date_from', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="f-to">To</label>
            <input id="f-to" className="input" type="datetime-local" value={filters.date_to}
                   onChange={(e) => update('date_to', e.target.value)} />
          </div>
        </div>
      </Card>

      <Card title={page ? `${page.total} transactions` : 'Transactions'}>
        {!page ? <Spinner /> : page.items.length === 0 ? (
          <EmptyState title="No transactions match these filters" />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="table">
                <thead>
                  <tr>
                    <th>Started</th><th>Gateway</th><th>Integration</th><th>Amount</th>
                    <th>Status</th><th className="text-right">Calls</th>
                    <th className="text-right">Gateway API</th>
                    <th className="text-right">Total</th><th>Reference</th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((row) => (
                    <tr key={row.id}>
                      <td className="whitespace-nowrap text-xs text-ink-500">
                        {dateTime(row.started_at)}
                      </td>
                      <td className="font-medium">
                        <Link to={`/transactions/${row.id}`}
                              className="text-accent-600 hover:underline">
                          {row.gateway_code}
                        </Link>
                      </td>
                      <td>{integrationLabel(row.integration_type)}</td>
                      <td className="num">{money(row.amount, row.currency)}</td>
                      <td><StatusBadge status={row.status} /></td>
                      <td className="num">{row.api_call_count}</td>
                      <td className="num">{ms(row.gateway_api_time_ms)}</td>
                      <td className="num">{ms(row.total_duration_ms)}</td>
                      <td className="max-w-[14rem] truncate font-mono text-xs text-ink-500">
                        {row.merchant_reference}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-4 flex items-center justify-between text-sm text-ink-600">
              <span>
                Showing {page.offset + 1}–{Math.min(page.offset + page.limit, page.total)} of{' '}
                {page.total}
              </span>
              <div className="flex gap-2">
                <button className="btn-secondary" disabled={offset === 0}
                        onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
                  Previous
                </button>
                <button className="btn-secondary"
                        disabled={offset + PAGE_SIZE >= page.total}
                        onClick={() => setOffset(offset + PAGE_SIZE)}>
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  )
}
