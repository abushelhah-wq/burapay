import React, { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, newIdempotencyKey } from '../lib/api.js'
import { ErrorNotice, Notice, Stat, StatusPill } from '../components/common.jsx'
import { duration, money, ms, timestamp, titleCase } from '../lib/format.js'

/**
 * Transaction detail (§5).
 *
 * The centre of this page is the ordered list of api_call_logs: method, URL,
 * status code, duration, the timeout that applied, and the redacted request and
 * response bodies. §2 requires a transaction to be explainable from these rows
 * alone, without opening a gateway dashboard — this is where that pays off.
 *
 * The action buttons are rendered from `actions`, which the backend computes.
 * The UI never decides for itself whether a refund is legal, so a button and
 * the endpoint behind it cannot disagree.
 */
export default function TransactionDetail() {
  const { id } = useParams()
  const [transaction, setTransaction] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const [captureAmount, setCaptureAmount] = useState('')
  const [refundAmount, setRefundAmount] = useState('')

  const load = useCallback(async () => {
    try {
      setTransaction(await api.transaction(id))
      setError(null)
    } catch (loadError) {
      setError(loadError)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  async function act(action, body) {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      const result = await action(body)
      setMessage(
        `${titleCase(result.operation)} finished as ${result.status}` +
          (result.error_message ? `: ${result.error_message}` : '.'),
      )
      await load()
    } catch (actionError) {
      setError(actionError)
    } finally {
      setBusy(false)
    }
  }

  function minor(text, currency) {
    const exponent = { KWD: 3, BHD: 3, OMR: 3, JOD: 3, TND: 3, JPY: 0, KRW: 0 }[currency] ?? 2
    const parsed = Number.parseFloat(text)
    return Number.isFinite(parsed) ? Math.round(parsed * 10 ** exponent) : null
  }

  if (error && !transaction) return <ErrorNotice error={error} />
  if (!transaction) return <div className="empty">Loading…</div>

  const actions = transaction.actions ?? {}
  const currency = transaction.amount?.currency ?? 'SAR'

  return (
    <>
      <div className="page-head">
        <div>
          <h1>
            Transaction #{transaction.id}{' '}
            <StatusPill status={transaction.status} />
          </h1>
          <p className="subtitle">
            {transaction.gateway_name} · {titleCase(transaction.integration_type)} ·{' '}
            {titleCase(transaction.operation)}
            {transaction.parent_transaction_id && (
              <>
                {' '}· child of{' '}
                <Link to={`/transactions/${transaction.parent_transaction_id}`}>
                  #{transaction.parent_transaction_id}
                </Link>
              </>
            )}
          </p>
        </div>
        <button onClick={load} disabled={busy}>Refresh</button>
      </div>

      <ErrorNotice error={error} />
      {message && <Notice tone="good">{message}</Notice>}

      <div className="grid four" style={{ marginBottom: 18 }}>
        <Stat label="Amount" value={money(transaction.amount)} />
        {/* The headline metric (§3): round trips this operation needed. */}
        <Stat
          label="HTTP round trips"
          value={transaction.request_count}
          hint={`${transaction.api_calls.length} logged`}
        />
        <Stat label="Duration" value={duration(transaction.duration_ms)} />
        <Stat
          label="Card"
          value={transaction.card_last4 ? `····${transaction.card_last4}` : '—'}
          hint={
            transaction.card_exp_month
              ? `${transaction.card_brand ?? ''} ${String(transaction.card_exp_month).padStart(2, '0')}/${transaction.card_exp_year}`
              : undefined
          }
        />
      </div>

      {transaction.error_message && (
        <Notice tone={transaction.status === 'FAILED' ? 'warn' : 'error'}>
          <strong>{transaction.error_code}</strong> — {transaction.error_message}
        </Notice>
      )}

      <div className="card">
        <h3>Identifiers</h3>
        <div className="grid two">
          <div>
            <div className="small muted">Merchant reference</div>
            <div className="mono">{transaction.merchant_reference}</div>
          </div>
          <div>
            <div className="small muted">Idempotency key</div>
            <div className="mono">{transaction.idempotency_key}</div>
          </div>
          <div>
            <div className="small muted">Gateway order ID</div>
            <div className="mono">{transaction.gateway_order_id ?? '—'}</div>
          </div>
          <div>
            <div className="small muted">Gateway transaction ID</div>
            <div className="mono">{transaction.gateway_transaction_id ?? '—'}</div>
          </div>
          <div>
            <div className="small muted">Started</div>
            <div>{timestamp(transaction.started_at)}</div>
          </div>
          <div>
            <div className="small muted">Completed</div>
            <div>{timestamp(transaction.completed_at)}</div>
          </div>
        </div>
      </div>

      {(actions.can_capture || actions.can_refund || actions.can_void || actions.can_query) && (
        <div className="card">
          <h3>Actions</h3>
          {actions.blocked_reason && (
            <p className="small muted">{actions.blocked_reason}</p>
          )}

          {actions.can_capture && (
            <div className="row" style={{ alignItems: 'flex-end', marginBottom: 12 }}>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>
                  Capture amount — up to {money(actions.remaining_capturable)} remaining
                </label>
                <input
                  value={captureAmount}
                  onChange={(event) => setCaptureAmount(event.target.value)}
                  placeholder={actions.remaining_capturable?.display ?? ''}
                  inputMode="decimal"
                />
              </div>
              <button
                disabled={busy}
                onClick={() =>
                  act((body) => api.capture(transaction.id, body), {
                    idempotency_key: newIdempotencyKey(),
                    ...(captureAmount
                      ? { amount_minor: minor(captureAmount, currency) }
                      : {}),
                  })
                }
              >
                {captureAmount ? 'Partial capture' : 'Capture in full'}
              </button>
            </div>
          )}

          {actions.can_refund && (
            <div className="row" style={{ alignItems: 'flex-end', marginBottom: 12 }}>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>
                  Refund amount — up to {money(actions.remaining_refundable)} refundable
                </label>
                <input
                  value={refundAmount}
                  onChange={(event) => setRefundAmount(event.target.value)}
                  placeholder={actions.remaining_refundable?.display ?? ''}
                  inputMode="decimal"
                />
              </div>
              <button
                disabled={busy}
                onClick={() =>
                  act((body) => api.refund(transaction.id, body), {
                    idempotency_key: newIdempotencyKey(),
                    ...(refundAmount
                      ? { amount_minor: minor(refundAmount, currency) }
                      : {}),
                  })
                }
              >
                {refundAmount ? 'Partial refund' : 'Refund in full'}
              </button>
            </div>
          )}

          <div className="button-row">
            {actions.can_void && (
              <button
                disabled={busy}
                onClick={() =>
                  act((body) => api.void(transaction.id, body), {
                    idempotency_key: newIdempotencyKey(),
                  })
                }
              >
                Void authorisation
              </button>
            )}
            {actions.can_query && (
              <button
                disabled={busy}
                onClick={() => act(() => api.queryOrder(transaction.id))}
              >
                Query gateway for status
              </button>
            )}
          </div>
        </div>
      )}

      <div className="card">
        <h3>API call trail — {transaction.api_calls.length} calls</h3>
        <p className="small muted">
          Every HTTP call this operation made, in order. Card numbers and CVVs
          were redacted before these rows were written, and credentials are
          masked to first-four/last-four.
        </p>
        {transaction.api_calls.length === 0 && (
          <p className="muted">
            No calls were made. If this transaction failed, the reason is above —
            a refusal that happens before any request still records why.
          </p>
        )}
        {transaction.api_calls.map((call) => (
          <details className="call" key={call.id}>
            <summary>
              <span className="seq">{call.sequence_number}</span>
              <strong>{call.step_name}</strong>
              <span className="mono muted">{call.http_method}</span>
              <span
                className={`pill ${
                  call.response_status_code === null
                    ? 'bad'
                    : call.response_status_code < 300
                      ? 'good'
                      : 'bad'
                }`}
              >
                {call.response_status_code ?? 'no response'}
              </span>
              <span className="muted">{ms(call.duration_ms)}</span>
              <span className="muted small">timeout {call.timeout_seconds}s</span>
            </summary>
            <div className="call-body">
              <div className="mono small muted" style={{ wordBreak: 'break-all' }}>
                {call.url}
              </div>
              {call.error_text && (
                <div className="notice error" style={{ marginTop: 8 }}>
                  {call.error_text}
                </div>
              )}
              <h3 style={{ marginTop: 12 }}>Request headers</h3>
              <pre>{JSON.stringify(call.request_headers_json, null, 2)}</pre>
              {call.request_body_json !== null && (
                <>
                  <h3 style={{ marginTop: 10 }}>Request body</h3>
                  <pre>{JSON.stringify(call.request_body_json, null, 2)}</pre>
                </>
              )}
              {call.response_body_json !== null && (
                <>
                  <h3 style={{ marginTop: 10 }}>Response body</h3>
                  <pre>{JSON.stringify(call.response_body_json, null, 2)}</pre>
                </>
              )}
              <details style={{ marginTop: 8 }}>
                <summary className="small muted" style={{ cursor: 'pointer' }}>
                  Response headers
                </summary>
                <pre>{JSON.stringify(call.response_headers_json, null, 2)}</pre>
              </details>
            </div>
          </details>
        ))}
      </div>

      {transaction.children.length > 0 && (
        <div className="card">
          <h3>Related operations</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Operation</th>
                  <th>Status</th>
                  <th className="numeric">Amount</th>
                  <th className="numeric">Calls</th>
                  <th className="numeric">Duration</th>
                </tr>
              </thead>
              <tbody>
                {transaction.children.map((child) => (
                  <tr key={child.id}>
                    <td><Link to={`/transactions/${child.id}`}>#{child.id}</Link></td>
                    <td>{titleCase(child.operation)}</td>
                    <td><StatusPill status={child.status} /></td>
                    <td className="numeric">{money(child.amount)}</td>
                    <td className="numeric">{child.request_count}</td>
                    <td className="numeric">{duration(child.duration_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  )
}
