import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { DocGaps, Empty, ErrorNotice, Stat } from '../components/common.jsx'
import { decimal, ms, percent, timestamp, titleCase } from '../lib/format.js'

/**
 * The benchmark dashboard (§5).
 *
 * Every figure comes from the /api/analytics/benchmark endpoint, which computes
 * it from stored transactions on each request. Sample sizes are shown next to
 * the percentiles on purpose: a p95 over four runs is not a p95, and the UI
 * should say so rather than let a reader mistake noise for a measurement.
 */
export default function Dashboard() {
  const [summary, setSummary] = useState(null)
  const [gateways, setGateways] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  async function load() {
    setLoading(true)
    try {
      const [data, gatewayList] = await Promise.all([api.benchmark(), api.gateways()])
      setSummary(data)
      setGateways(gatewayList)
      setError(null)
    } catch (loadError) {
      setError(loadError)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const cells = summary?.cells ?? []
  const totals = cells.reduce(
    (accumulator, cell) => ({
      transactions: accumulator.transactions + cell.sample_size,
      succeeded: accumulator.succeeded + cell.succeeded,
      completed: accumulator.completed + cell.completed,
      calls: accumulator.calls + (cell.avg_request_count ?? 0) * cell.sample_size,
    }),
    { transactions: 0, succeeded: 0, completed: 0, calls: 0 },
  )

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Benchmark</h1>
          <p className="subtitle">
            Round trips and latency per gateway, integration type and operation.
            Computed live from stored transactions — nothing here is sampled or
            seeded.
          </p>
        </div>
        <button onClick={load} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      <ErrorNotice error={error} />

      {summary && (
        <p className="small muted" style={{ marginTop: -8, marginBottom: 16 }}>
          Generated {timestamp(summary.generated_at)}
          {summary.measurement_location
            ? ` · measured from ${summary.measurement_location}`
            : ' · MEASUREMENT_LOCATION is not set, so these latencies have no recorded origin'}
        </p>
      )}

      {cells.length === 0 && !loading && (
        <Empty>
          No transactions yet. Run one from <strong>New transaction</strong> and
          the numbers will appear here.
        </Empty>
      )}

      {cells.length > 0 && (
        <>
          <div className="grid four" style={{ marginBottom: 20 }}>
            <Stat label="Transactions" value={totals.transactions} />
            <Stat
              label="Success rate"
              value={totals.completed ? percent(totals.succeeded / totals.completed) : '—'}
              hint={`${totals.succeeded} of ${totals.completed} completed`}
            />
            <Stat label="Gateway calls" value={Math.round(totals.calls)} />
            <Stat label="Cells" value={cells.length} />
          </div>

          <div className="card">
            <h3>Per gateway × integration type × operation</h3>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Gateway</th>
                    <th>Integration</th>
                    <th>Operation</th>
                    <th className="numeric">Runs</th>
                    <th className="numeric">Success</th>
                    <th className="numeric">Avg calls</th>
                    <th className="numeric">Avg</th>
                    <th className="numeric">Median</th>
                    <th className="numeric">p95</th>
                    <th className="numeric">Max</th>
                  </tr>
                </thead>
                <tbody>
                  {cells.map((cell) => (
                    <tr key={`${cell.gateway_code}-${cell.integration_type_code}-${cell.operation}`}>
                      <td>{cell.gateway_name}</td>
                      <td className="muted">{titleCase(cell.integration_type_code)}</td>
                      <td>{titleCase(cell.operation)}</td>
                      <td className="numeric">{cell.sample_size}</td>
                      <td className="numeric">{percent(cell.success_rate)}</td>
                      {/* The headline metric: round trips to complete this op. */}
                      <td className="numeric">
                        <strong>{decimal(cell.avg_request_count, 1)}</strong>
                      </td>
                      <td className="numeric">{ms(cell.avg_duration_ms)}</td>
                      <td className="numeric">{ms(cell.median_duration_ms)}</td>
                      <td className="numeric">
                        {ms(cell.p95_duration_ms)}
                        {cell.latency_sample_size > 0 && cell.latency_sample_size < 20 && (
                          <span
                            className="muted small"
                            title={`Only ${cell.latency_sample_size} timed runs — a p95 needs a few dozen to mean anything.`}
                          >
                            {' '}*
                          </span>
                        )}
                      </td>
                      <td className="numeric">{ms(cell.max_duration_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {cells.some((cell) => cell.latency_sample_size > 0 && cell.latency_sample_size < 20) && (
              <p className="small muted" style={{ marginTop: 10 }}>
                * Fewer than 20 timed runs in this cell. Percentiles are
                nearest-rank, so every figure is a latency that actually
                occurred — but with a handful of samples a p95 is not yet a
                meaningful number.
              </p>
            )}
          </div>

          <div className="card">
            <h3>Failure breakdown</h3>
            <p className="small muted">
              A decline is a completed round trip and counts as reliability;
              an error or a timeout is the gateway failing to answer. Averaging
              them together would hide the difference this benchmark exists to
              measure.
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Gateway</th>
                    <th>Operation</th>
                    <th className="numeric">Succeeded</th>
                    <th className="numeric">Declined</th>
                    <th className="numeric">Errored</th>
                    <th className="numeric">Timed out</th>
                    <th className="numeric">Pending</th>
                  </tr>
                </thead>
                <tbody>
                  {cells.map((cell) => (
                    <tr key={`f-${cell.gateway_code}-${cell.integration_type_code}-${cell.operation}`}>
                      <td>{cell.gateway_name}</td>
                      <td>{titleCase(cell.operation)}</td>
                      <td className="numeric">{cell.succeeded}</td>
                      <td className="numeric">{cell.failed}</td>
                      <td className="numeric">{cell.errored}</td>
                      <td className="numeric">{cell.timed_out}</td>
                      <td className="numeric muted">{cell.pending}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {gateways.map((gateway) => (
        <DocGaps key={gateway.code} gaps={gateway.doc_gaps} />
      ))}
    </>
  )
}
