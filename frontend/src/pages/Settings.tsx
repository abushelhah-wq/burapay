/**
 * Settings: gateway credentials, benchmark limits and scoring weights.
 *
 * User management has its own screen (specification section 10); this page links to it
 * rather than carrying a second, thinner copy of it.
 *
 * Credential fields are rendered from what the adapter declares, so a gateway added
 * later gets a working form with no frontend change. Secrets arrive masked and are
 * only sent back when the user actually types a new value — submitting the form
 * without retyping leaves the stored secret alone.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import type { AppSettings, Gateway } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import { Badge, Card, ErrorNotice, Note, Spinner } from '../components/ui'

function GatewayCredentials({ gateway, onSaved }: {
  gateway: Gateway
  onSaved: (updated: Gateway) => void
}) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [touched, setTouched] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setTouched({})
    api.credentials(gateway.code)
      .then((result) => setValues(result.values ?? {}))
      .catch((exc) => setError(exc instanceof Error ? exc.message : String(exc)))
      .finally(() => setLoading(false))
  }, [gateway.code])

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setMessage(null)
    setError(null)
    try {
      // Only fields the user actually edited are sent. Everything else keeps its
      // stored value, which is what makes masked display safe.
      const payload: Record<string, string> = {}
      for (const [key, value] of Object.entries(values)) {
        const field = gateway.credential_fields.find((item) => item.key === key)
        if (!field) continue
        if (field.secret && !touched[key]) continue
        payload[key] = value
      }
      await api.saveCredentials(gateway.code, 'sandbox', payload)
      const refreshed = await api.gateway(gateway.code)
      onSaved(refreshed)
      setMessage('Credentials saved. Secrets are stored encrypted and shown masked.')
      setTouched({})
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Could not save credentials.')
    } finally {
      setBusy(false)
    }
  }

  async function handleDelete() {
    if (!confirm(`Remove all stored credentials for ${gateway.name}?`)) return
    setBusy(true)
    try {
      await api.deleteCredentials(gateway.code)
      setValues({})
      onSaved(await api.gateway(gateway.code))
      setMessage('Credentials removed.')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Could not remove credentials.')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <Spinner label={`Loading ${gateway.name} credentials`} />

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={gateway.configured ? 'good' : 'neutral'}>{gateway.status}</Badge>
        {gateway.supports_hpp && <Badge tone="info">HPP</Badge>}
        {gateway.supports_direct && <Badge tone="info">Direct API</Badge>}
        {gateway.docs_url && (
          <a href={gateway.docs_url} target="_blank" rel="noreferrer"
             className="text-xs text-accent-600 hover:underline">
            Provider documentation ↗
          </a>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {gateway.credential_fields.map((field) => (
          <div key={field.key} className={field.help_text ? 'md:col-span-1' : ''}>
            <label className="label" htmlFor={`${gateway.code}-${field.key}`}>
              {field.label}
              {field.required && <span className="ml-1 text-red-500">*</span>}
              {field.secret && (
                <span className="ml-2 text-xs font-normal text-ink-400">encrypted</span>
              )}
            </label>

            {field.choices.length > 0 ? (
              <select id={`${gateway.code}-${field.key}`} className="input"
                      value={values[field.key] ?? field.default ?? ''}
                      onChange={(e) => {
                        setValues((current) => ({ ...current, [field.key]: e.target.value }))
                        setTouched((current) => ({ ...current, [field.key]: true }))
                      }}>
                {!field.required && <option value="">—</option>}
                {field.choices.map((choice) => (
                  <option key={choice} value={choice}>{choice}</option>
                ))}
              </select>
            ) : (
              <input id={`${gateway.code}-${field.key}`} className="input"
                     type={field.secret ? 'password' : 'text'}
                     value={values[field.key] ?? ''}
                     placeholder={field.placeholder || field.default || ''}
                     autoComplete="off"
                     onFocus={() => {
                       // Clear the mask on focus so the user is typing into an empty
                       // box rather than editing a row of asterisks.
                       if (field.secret && !touched[field.key]) {
                         setValues((current) => ({ ...current, [field.key]: '' }))
                         setTouched((current) => ({ ...current, [field.key]: true }))
                       }
                     }}
                     onChange={(e) => {
                       setValues((current) => ({ ...current, [field.key]: e.target.value }))
                       setTouched((current) => ({ ...current, [field.key]: true }))
                     }} />
            )}
            {field.help_text && <p className="hint">{field.help_text}</p>}
          </div>
        ))}
      </div>

      {message && (
        <p className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm
                      text-emerald-700">{message}</p>
      )}
      {error && (
        <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
          {error}
        </p>
      )}

      <div className="flex gap-2">
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save credentials'}
        </button>
        <button type="button" className="btn-danger" disabled={busy} onClick={handleDelete}>
          Remove
        </button>
      </div>
    </form>
  )
}

export default function Settings() {
  const { isAdmin } = useAuth()
  const [params, setParams] = useSearchParams()
  const [gateways, setGateways] = useState<Gateway[] | null>(null)
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [saved, setSaved] = useState<string | null>(null)

  const selectedCode = params.get('gateway') ?? ''

  const load = useCallback(() => {
    Promise.all([api.gateways(), api.settings()])
      .then(([gatewayList, appSettings]) => {
        setGateways(gatewayList)
        setSettings(appSettings)
        if (!params.get('gateway') && gatewayList.length) {
          setParams({ gateway: gatewayList[0].code }, { replace: true })
        }
      })
      .catch(setError)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { load() }, [load])

  const selected = useMemo(
    () => gateways?.find((item) => item.code === selectedCode) ?? gateways?.[0] ?? null,
    [gateways, selectedCode])

  async function saveSetting(key: string, value: Record<string, unknown>) {
    await api.updateSetting(key, value)
    setSettings(await api.settings())
    setSaved(`${key.replace(/_/g, ' ')} saved.`)
  }

  if (error) return <ErrorNotice error={error} onRetry={load} />
  if (!gateways || !settings) return <Spinner label="Loading settings" />

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">Settings</h1>
        <p className="text-sm text-ink-500">
          Gateway credentials, benchmark limits and scoring weights.
        </p>
      </header>

      {!isAdmin && (
        <Note>
          You are signed in as a viewer. Credentials and application settings can only be
          changed by an administrator.
        </Note>
      )}

      {saved && (
        <p className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm
                      text-emerald-700">{saved}</p>
      )}

      {isAdmin && (
        <Card title="Gateway credentials">
          <div className="mb-5 flex flex-wrap gap-2">
            {gateways.map((gateway) => (
              <button key={gateway.code} type="button"
                      onClick={() => setParams({ gateway: gateway.code })}
                      className={selected?.code === gateway.code
                        ? 'btn-primary text-xs' : 'btn-secondary text-xs'}>
                {gateway.name}
                {gateway.configured && <span className="ml-1 text-emerald-500">●</span>}
              </button>
            ))}
          </div>

          {selected && (
            <GatewayCredentials gateway={selected}
                                onSaved={(updated) => setGateways((current) =>
                                  current?.map((item) =>
                                    item.code === updated.code ? updated : item) ?? null)} />
          )}

          <div className="mt-6">
            <Note>
              Credentials are encrypted with the deployment’s ENCRYPTION_KEY before they
              are stored, and no endpoint returns a secret in full — only a mask. Leaving
              a secret field untouched keeps the stored value; typing into it replaces it.
            </Note>
          </div>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Benchmark limits">
          <BenchmarkLimits settings={settings} disabled={!isAdmin}
                           onSave={(value) => saveSetting('benchmark_limits', value)} />
        </Card>

        <Card title="Internal Benchmark Score weights">
          <ScoringWeights settings={settings} disabled={!isAdmin}
                          onSave={(value) => saveSetting('scoring_weights', value)} />
        </Card>
      </div>

      <Card title="Environment">
        <dl className="grid grid-cols-2 gap-y-3 text-sm sm:grid-cols-3">
          <dt className="text-ink-500">Version</dt>
          <dd className="sm:col-span-2">{settings.environment.app_version}</dd>
          <dt className="text-ink-500">Environment</dt>
          <dd className="sm:col-span-2">{settings.environment.app_env}</dd>
          <dt className="text-ink-500">Public base URL</dt>
          <dd className="break-all sm:col-span-2">{settings.environment.public_base_url}</dd>
          <dt className="text-ink-500">Mode</dt>
          <dd className="sm:col-span-2">
            {settings.environment.test_mode
              ? <Badge tone="warn">TEST MODE — sandbox gateways only</Badge>
              : <Badge tone="warn">Production gateways enabled</Badge>}
          </dd>
          <dt className="text-ink-500">Test card</dt>
          <dd className="sm:col-span-2 font-mono text-xs">
            {settings.settings.test_card.number} · exp{' '}
            {settings.settings.test_card.month}/{settings.settings.test_card.year}
          </dd>
        </dl>
        <div className="mt-4">
          <Note>
            The test card is a sandbox card used by Direct API flows. It is never stored
            against a transaction, and no card number, CVV or track data is persisted
            anywhere in this platform.
          </Note>
        </div>
      </Card>

      {isAdmin && (
        <Card title="Users">
          <p className="text-sm text-ink-600">
            Accounts, roles and password resets live on their own screen, which also
            shows the audit trail for each change.
          </p>
          <Link to="/users" className="btn-secondary mt-3 inline-block">
            Open Users
          </Link>
        </Card>
      )}
    </div>
  )
}

function BenchmarkLimits({ settings, disabled, onSave }: {
  settings: AppSettings
  disabled: boolean
  onSave: (value: Record<string, unknown>) => Promise<void>
}) {
  const limits = settings.settings.benchmark_limits
  const [interval, setInterval] = useState(limits.min_interval_seconds)
  const [maxCount, setMaxCount] = useState(limits.max_transactions_per_run)
  const [busy, setBusy] = useState(false)

  return (
    <form className="space-y-4" onSubmit={async (event) => {
      event.preventDefault()
      setBusy(true)
      try {
        await onSave({ min_interval_seconds: interval, max_transactions_per_run: maxCount,
                       max_concurrency: limits.max_concurrency })
      } finally { setBusy(false) }
    }}>
      <div>
        <label className="label" htmlFor="min-interval">
          Minimum seconds between transactions
        </label>
        <input id="min-interval" className="input" type="number" step="0.5" min="0.25"
               value={interval} disabled={disabled}
               onChange={(e) => setInterval(Number(e.target.value))} />
        <p className="hint">
          Applied to every automated run. The floor cannot be removed — respecting each
          provider’s rate limits is not optional.
        </p>
      </div>
      <div>
        <label className="label" htmlFor="max-count">Maximum transactions per run</label>
        <input id="max-count" className="input" type="number" min="1" value={maxCount}
               disabled={disabled} onChange={(e) => setMaxCount(Number(e.target.value))} />
      </div>
      {!disabled && (
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save limits'}
        </button>
      )}
    </form>
  )
}

function ScoringWeights({ settings, disabled, onSave }: {
  settings: AppSettings
  disabled: boolean
  onSave: (value: Record<string, unknown>) => Promise<void>
}) {
  const [weights, setWeights] = useState(settings.settings.scoring_weights)
  const [busy, setBusy] = useState(false)
  const total = Object.values(weights).reduce((sum, value) => sum + Number(value || 0), 0)

  return (
    <form className="space-y-4" onSubmit={async (event) => {
      event.preventDefault()
      setBusy(true)
      try { await onSave(weights as unknown as Record<string, unknown>) }
      finally { setBusy(false) }
    }}>
      {Object.entries(settings.scoring_components).map(([key, label]) => (
        <div key={key}>
          <label className="label" htmlFor={`weight-${key}`}>{label}</label>
          <input id={`weight-${key}`} className="input" type="number" step="0.05" min="0"
                 max="1" value={weights[key] ?? 0} disabled={disabled}
                 onChange={(e) => setWeights((current) => ({
                   ...current, [key]: Number(e.target.value) }))} />
        </div>
      ))}
      <p className="text-xs text-ink-500">
        Current total {total.toFixed(2)} — weights are normalised to 1 when saved.
      </p>
      <Note>
        The Internal Benchmark Score is defined by this platform, not by any standard.
        It is only meaningful within a single comparison set, and gateways without
        enough samples are excluded from it entirely.
      </Note>
      {!disabled && (
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? 'Saving…' : 'Save weights'}
        </button>
      )}
    </form>
  )
}

