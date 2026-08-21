import React, { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api.js'
import { Empty, ErrorNotice, Field, StatusPill } from '../components/common.jsx'
import { duration, money, timestamp, titleCase } from '../lib/format.js'

const OPERATIONS = [
  'SALE', 'AUTH', 'CAPTURE', 'PARTIAL_CAPTURE', 'REFUND', 'PARTIAL_REFUND',
  'VOID', 'TOKENIZE', 'CIT_CHARGE', 'MIT_CHARGE', 'ORDER_QUERY',
]
const STATUSES = ['PENDING', 'SUCCESS', 'FAILED', 'ERROR', 'TIMEOUT']

/**
 * The audit log (§5).
 *
 * Filterable by gateway, integration type, operation, status and date range,
 * paginated, and backed by the transactions table directly. Nothing is sampled.
 */
export default function Transactions() {
  const [filters, setFilters] = useState({
    gateway: '', integration_type: '', operation: '', status: '',
    since: '', until: '',
  })
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null)
  const [gateways, setGateways] = useState([])
  const [integrationTypes, setIntegrationTypes] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([api.gateways(), api.integrationTypes()])
      .then(([gatewayList, typeList]) => {
        setGateways(gatewayList)
        setIntegrationTypes(typeList)
      })
      .catch(() => {})
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(
        await api.transactions({
          ...filters,
          // Date inputs give a bare date; widen it to the whole day so "until
          // today" includes today rather than stopping at midnight.
          since: filters.since ? `${filters.since}T00:00:00Z` : '',
          until: filters.until ? `${filters.until}T23:59:59Z` : '',
          page,
          page_size: 25,
        }),
      )
      setError(null)
    } catch (loadError) {
      setError(loadError)
    } finally {
      setLoading(false)
    }
  }, [filters, page])

  useEffect(() => {
    load()
  }, [load])

  function update(key, value) {
    setFilters((current) => ({ ...current, [key]: value }))
    setPage(1)
  }

  const items = data?.items ?? []
  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Transactions</h1>
          <p className="subtitle">
            {data ? `${data.total} recorded` : 'Loading…'}. Open one to see every
            HTTP call it made, in order.
          </p>
        </div>
        <button onClick={load} disabled={loading}>Refresh</button>
      </div>

      <ErrorNotice error={error} />

      <div className="card">
        <div className="filters">
          <Field label="Gateway">
            <select value={filters.gateway} onChange={(e) => update('gateway', e.target.value)}>
              <option value="">All</option>
              {gateways.map((gateway) => (
                <option key={gateway.code} value={gateway.code}>{gateway.display_name}</option>
              ))}
            </select>
          </Field>
          <Field label="Integration">
            <select
              value={filters.integration_type}
              onChange={(e) => update('integration_type', e.target.value)}
            >
              <option value="">All</option>
              {integrationTypes.map((type) => (
                <option key={type.code} value={type.code}>{type.display_name}</option>
              ))}
            </select>
          </Field>
          <Field label="Operation">
            <select value={filters.operation} onChange={(e) => update('operation', e.target.value)}>
              <option value="">All</option>
              {OPERATIONS.map((operation) => (
                <option key={operation} value={operation}>{titleCase(operation)}</option>
              ))}
            </select>
          </Field>
          <Field label="Status">
            <select value={filters.status} onChange={(e) => update('status', e.target.value)}>
              <option value="">All</option>
              {STATUSES.map((status) => (
                <option key={status} value={status}>{status}</option>
              ))}
            </select>
          </Field>
          <Field label="From">
            <input type="date" value={filters.since} onChange={(e) => update('since', e.target.value)} />
          </Field>
          <Field label="To">
            <input type="date" value={filters.until} onChange={(e) => update('until', e.target.value)} />
          </Field>
        </div>

        {items.length === 0 && !loading ? (
          <Empty>No transactions match these filters.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Gateway</th>
                  <th>Integration</th>
                  <th>Operation</th>
                  <th>Status</th>
                  <th className="numeric">Amount</th>
                  <th>Card</th>
                  <th className="numeric">Calls</th>
                  <th className="numeric">Duration</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {items.map((transaction) => (
                  <tr key={transaction.id}>
                    <td><Link to={`/transactions/${transaction.id}`}>#{transaction.id}</Link></td>
                    <td>{transaction.gateway_name}</td>
                    <td className="muted">{titleCase(transaction.integration_type)}</td>
                    <td>{titleCase(transaction.operation)}</td>
                    <td><StatusPill status={transaction.status} /></td>
                    <td className="numeric">{money(transaction.amount)}</td>
                    <td className="muted mono">
                      {transaction.card_last4
                        ? `${transaction.card_brand ?? ''} ····${transaction.card_last4}`
                        : '—'}
                    </td>
                    <td className="numeric"><strong>{transaction.request_count}</strong></td>
                    <td className="numeric">{duration(transaction.duration_ms)}</td>
                    <td className="muted small">{timestamp(transaction.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="pager">
          <button onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={page <= 1}>
            Previous
          </button>
          <span className="muted">Page {page} of {totalPages}</span>
          <button onClick={() => setPage((current) => current + 1)} disabled={page >= totalPages}>
            Next
          </button>
        </div>
      </div>
    </>
  )
}
