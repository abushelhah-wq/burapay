/** Gateway catalogue and the operational health view (specification section 36). */

import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import type { Gateway, GatewayHealth } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { Badge, Card, EmptyState, ErrorNotice, Note, Spinner } from '../components/ui'
import { ms, relativeTime } from '../lib/format'

export default function Gateways() {
  const { isAdmin } = useAuth()
  const [gateways, setGateways] = useState<Gateway[] | null>(null)
  const [health, setHealth] = useState<GatewayHealth[]>([])
  const [error, setError] = useState<unknown>(null)
  const [checking, setChecking] = useState(false)

  const load = useCallback(() => {
    Promise.all([api.gateways(), api.health()])
      .then(([gatewayList, healthRows]) => {
        setGateways(gatewayList)
        setHealth(healthRows)
      })
      .catch(setError)
  }, [])

  useEffect(() => { load() }, [load])

  async function runCheck() {
    setChecking(true)
    try {
      setHealth(await api.runHealthCheck())
    } catch (exc) {
      setError(exc)
    } finally {
      setChecking(false)
    }
  }

  async function toggle(gateway: Gateway) {
    const updated = await api.updateGateway(gateway.code, { enabled: !gateway.enabled })
    setGateways((current) =>
      current?.map((item) => (item.code === updated.code ? updated : item)) ?? null)
  }

  if (error) return <ErrorNotice error={error} onRetry={load} />
  if (!gateways) return <Spinner label="Loading gateways" />

  const healthByCode = new Map(health.map((row) => [row.gateway_code, row]))

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">Gateways</h1>
          <p className="text-sm text-ink-500">
            What each gateway supports, whether it is configured, and whether it is
            answering.
          </p>
        </div>
        {isAdmin && (
          <button className="btn-secondary" onClick={runCheck} disabled={checking}>
            {checking ? 'Checking…' : 'Run health check'}
          </button>
        )}
      </header>

      <Card title="Availability">
        {health.length === 0 ? (
          <EmptyState title="No health checks recorded yet"
                      description="Probes run automatically for gateways that have credentials configured." />
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="table">
                <thead>
                  <tr><th>Gateway</th><th>Status</th><th>Last check</th>
                      <th className="text-right">Response</th><th>Detail</th></tr>
                </thead>
                <tbody>
                  {health.map((row) => (
                    <tr key={row.gateway_code}>
                      <td className="font-medium">{row.gateway_code}</td>
                      <td>
                        <span className={`inline-flex items-center gap-2 text-sm
                          ${row.available === true ? 'text-emerald-600'
                            : row.available === false ? 'text-red-600' : 'text-ink-500'}`}>
                          <span className={`h-2 w-2 rounded-full
                            ${row.available === true ? 'bg-emerald-500'
                              : row.available === false ? 'bg-red-500' : 'bg-ink-300'}`} />
                          {row.status}
                        </span>
                      </td>
                      <td className="text-xs text-ink-500">{relativeTime(row.checked_at)}</td>
                      <td className="num">{ms(row.response_time_ms)}</td>
                      <td className="max-w-[24rem] truncate text-xs text-ink-500">
                        {row.detail}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-4">
              <Note>
                Health checks use read-only endpoints only — a status lookup or a listing
                call. No payment is created to test whether a gateway is up.
              </Note>
            </div>
          </>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {gateways.map((gateway) => {
          const status = healthByCode.get(gateway.code)
          return (
            <div key={gateway.code} className="card flex flex-col p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="text-base font-semibold text-ink-900">{gateway.name}</h3>
                  <p className="font-mono text-xs text-ink-400">{gateway.code}</p>
                </div>
                <div className="flex flex-col items-end gap-1">
                  {gateway.is_simulated && <Badge tone="warn">simulator</Badge>}
                  <Badge tone={gateway.configured ? 'good' : 'neutral'}>
                    {gateway.status}
                  </Badge>
                </div>
              </div>

              <div className="mt-3 flex flex-wrap gap-2">
                {gateway.supports_hpp && <Badge tone="info">HPP</Badge>}
                {gateway.supports_direct && <Badge tone="info">Direct API</Badge>}
                {gateway.supported_currencies.map((code) => (
                  <Badge key={code}>{code}</Badge>
                ))}
              </div>

              {Object.entries(gateway.documented_calls).length > 0 && (
                <dl className="mt-4 space-y-1 text-xs text-ink-600">
                  {Object.entries(gateway.documented_calls).map(([key, value]) => (
                    <div key={key} className="flex gap-2">
                      <dt className="w-14 shrink-0 uppercase text-ink-400">{key}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              )}

              {gateway.notes.length > 0 && (
                <ul className="mt-4 space-y-2 text-xs leading-relaxed text-ink-500">
                  {gateway.notes.map((note) => (
                    <li key={note} className="border-l-2 border-ink-200 pl-3">{note}</li>
                  ))}
                </ul>
              )}

              {!gateway.configured && gateway.missing_fields.length > 0 && (
                <p className="mt-4 rounded-lg bg-amber-50 p-2 text-xs text-amber-800">
                  Missing: {gateway.missing_fields.join(', ')}
                </p>
              )}

              <div className="mt-auto flex items-center justify-between gap-2 pt-4">
                <span className="text-xs text-ink-500">
                  {status ? `${status.status} · ${ms(status.response_time_ms)}`
                          : 'Not probed yet'}
                </span>
                <div className="flex gap-2">
                  {gateway.docs_url && (
                    <a href={gateway.docs_url} target="_blank" rel="noreferrer"
                       className="btn-ghost text-xs">Docs</a>
                  )}
                  {isAdmin && (
                    <>
                      <Link to={`/settings?gateway=${gateway.code}`}
                            className="btn-secondary text-xs">Configure</Link>
                      <button className="btn-ghost text-xs" onClick={() => toggle(gateway)}>
                        {gateway.enabled ? 'Disable' : 'Enable'}
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
