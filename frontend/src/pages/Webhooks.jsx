import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api.js'
import { Empty, ErrorNotice } from '../components/common.jsx'
import { timestamp } from '../lib/format.js'

/**
 * Inbound callbacks (§4k).
 *
 * Every callback is stored before it is judged, including ones that matched no
 * transaction and ones whose signature could not be verified — those are the
 * ones worth being able to look at.
 */
export default function Webhooks() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    api.webhooks({ page_size: 50 }).then(setData).catch(setError)
  }, [])

  const items = data?.items ?? []

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Callbacks</h1>
          <p className="subtitle">
            Raw server-to-server callbacks as received. A null signature means
            the gateway's verification scheme is not documented in this build —
            which is different from a signature that failed.
          </p>
        </div>
      </div>

      <ErrorNotice error={error} />

      <div className="card">
        {items.length === 0 ? (
          <Empty>No callbacks received yet.</Empty>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Gateway</th>
                  <th>Received</th>
                  <th>Signature</th>
                  <th>Matched</th>
                  <th>Event ref</th>
                  <th>Duplicate of</th>
                </tr>
              </thead>
              <tbody>
                {items.map((webhook) => (
                  <tr key={webhook.id}>
                    <td>#{webhook.id}</td>
                    <td>{webhook.gateway_code}</td>
                    <td className="small muted">{timestamp(webhook.received_at)}</td>
                    <td>
                      {webhook.signature_valid === null ? (
                        <span className="pill">not verifiable</span>
                      ) : webhook.signature_valid ? (
                        <span className="pill good">valid</span>
                      ) : (
                        <span className="pill bad">invalid</span>
                      )}
                    </td>
                    <td>
                      {webhook.matched_transaction_id ? (
                        <Link to={`/transactions/${webhook.matched_transaction_id}`}>
                          #{webhook.matched_transaction_id}
                        </Link>
                      ) : (
                        <span className="muted">unmatched</span>
                      )}
                    </td>
                    <td className="mono small">{webhook.event_reference ?? '—'}</td>
                    <td className="muted small">
                      {webhook.duplicate_of_id ? `#${webhook.duplicate_of_id}` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {items.map((webhook) => (
        <details className="call" key={`body-${webhook.id}`}>
          <summary>
            <span className="mono">#{webhook.id}</span>
            <span className="muted">{webhook.gateway_code}</span>
            <span className="muted small">{timestamp(webhook.received_at)}</span>
          </summary>
          <div className="call-body">
            <h3>Headers</h3>
            <pre>{JSON.stringify(webhook.headers_json, null, 2)}</pre>
            <h3 style={{ marginTop: 10 }}>Body</h3>
            <pre>{JSON.stringify(webhook.body_json, null, 2)}</pre>
          </div>
        </details>
      ))}
    </>
  )
}
