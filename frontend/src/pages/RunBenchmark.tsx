/**
 * The user journey from specification section 3: gateway → integration → amount.
 *
 * Three ways to start work, all on one page because they share the same three
 * choices: a single measured transaction, a repeated benchmark run, and a
 * multi-gateway comparison test.
 *
 * Unsupported combinations are disabled rather than hidden, with the reason shown —
 * "Geidea has no credentials yet" is more useful than a gateway that silently is not
 * in the list.
 */

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import type { AppSettings, Gateway } from '../api/types'
import { Badge, Card, ErrorNotice, Note, Spinner } from '../components/ui'
import { markRedirect } from '../lib/browserMetrics'
import { integrationLabel } from '../lib/format'

type Mode = 'single' | 'run' | 'comparison'

const CURRENCIES = ['SAR', 'USD', 'AED', 'EUR', 'GBP']

export default function RunBenchmark() {
  const navigate = useNavigate()
  const [gateways, setGateways] = useState<Gateway[] | null>(null)
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [limits, setLimits] = useState<{ min_interval_seconds: number;
    max_transactions_per_run: number; note: string } | null>(null)
  const [error, setError] = useState<unknown>(null)

  const [mode, setMode] = useState<Mode>('single')
  const [gatewayCode, setGatewayCode] = useState('')
  const [comparisonCodes, setComparisonCodes] = useState<string[]>([])
  const [integration, setIntegration] = useState<'direct' | 'hpp'>('direct')
  const [amount, setAmount] = useState('1.00')
  const [currency, setCurrency] = useState('SAR')
  const [reference, setReference] = useState('')
  const [description, setDescription] = useState('BuraPay benchmark transaction')
  const [methodology, setMethodology] = useState('mixed')
  const [transactionCount, setTransactionCount] = useState(20)
  const [interval, setInterval] = useState(2)
  const [runName, setRunName] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.gateways(), api.settings(), api.limits()])
      .then(([gatewayList, appSettings, runLimits]) => {
        setGateways(gatewayList)
        setSettings(appSettings)
        setLimits(runLimits)
        setInterval(runLimits.min_interval_seconds)
        setAmount(appSettings.settings.test_defaults.amount.toFixed(2))
        setCurrency(appSettings.settings.test_defaults.currency)
        const firstUsable = gatewayList.find((item) => item.configured && item.enabled)
        if (firstUsable) setGatewayCode(firstUsable.code)
      })
      .catch(setError)
  }, [])

  const selected = useMemo(
    () => gateways?.find((item) => item.code === gatewayCode) ?? null,
    [gateways, gatewayCode])

  useEffect(() => {
    // Keep the integration choice valid when the gateway changes.
    if (!selected) return
    if (integration === 'hpp' && !selected.supports_hpp) setIntegration('direct')
    if (integration === 'direct' && !selected.supports_direct) setIntegration('hpp')
  }, [selected, integration])

  if (error) return <ErrorNotice error={error} />
  if (!gateways || !settings || !limits) return <Spinner label="Loading gateways" />

  const usable = gateways.filter((item) => item.enabled)
  const currencyUnsupported = Boolean(
    selected && selected.supported_currencies.length &&
    !selected.supported_currencies.includes(currency))

  async function startSingle() {
    const result = await api.startTransaction({
      gateway_code: gatewayCode, integration_type: integration,
      amount: Number(amount), currency, description,
      reference: reference || undefined, methodology,
    })
    if (result.mode === 'widget') {
      // The gateway returned an identifier for its own browser component rather than
      // a URL. Mount it in our own page; the customer never leaves this origin.
      navigate(`/hpp/widget/${result.transaction_id}`)
      return
    }
    if (result.redirect_url) {
      // HPP: the browser leaves for the gateway's own page. Record when, because the
      // gap between here and the return is time this origin cannot see.
      markRedirect(result.transaction_id)
      if (result.redirect_url.startsWith('http')) {
        window.location.href = result.redirect_url
      } else {
        // A relative target is a page this application serves — only MockPay's
        // simulated hosted page today. It needs the transaction id to find its way
        // back, which a real gateway carries in its own session instead.
        const separator = result.redirect_url.includes('?') ? '&' : '?'
        window.location.href = `${window.location.origin}${result.redirect_url}`
          + `${separator}transaction=${result.transaction_id}`
      }
      return
    }
    navigate(`/transactions/${result.transaction_id}`)
  }

  async function startRun() {
    const run = await api.createRun({
      name: runName || `${selected?.name} ${integrationLabel(integration)}`,
      gateway_code: gatewayCode, integration_type: integration,
      transaction_count: transactionCount, amount: Number(amount), currency,
      interval_seconds: interval, methodology,
    })
    navigate(`/runs/${run.id}`)
  }

  async function startComparison() {
    const test = await api.createComparisonTest({
      name: runName || `${integrationLabel(integration)} comparison — ${currency}`,
      gateway_codes: comparisonCodes, integration_type: integration,
      transactions_per_gateway: transactionCount, amount: Number(amount), currency,
      interval_seconds: interval, methodology,
    })
    navigate(`/comparison-tests/${test.id}`)
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      if (mode === 'single') await startSingle()
      else if (mode === 'run') await startRun()
      else await startComparison()
    } catch (exc) {
      setMessage(exc instanceof ApiError ? exc.message
        : exc instanceof Error ? exc.message : 'Could not start the benchmark.')
    } finally {
      setBusy(false)
    }
  }

  const canSubmit = mode === 'comparison'
    ? comparisonCodes.length >= 2
    : Boolean(selected?.configured)

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">Run Benchmark</h1>
        <p className="text-sm text-ink-500">
          Pick a gateway, an integration model and the transaction details. Every call
          is timed server-side.
        </p>
      </header>

      <div className="flex flex-wrap gap-2">
        {([['single', 'Single transaction'], ['run', 'Benchmark run'],
           ['comparison', 'Gateway comparison test']] as [Mode, string][]).map(
          ([value, label]) => (
            <button key={value} type="button"
                    onClick={() => setMode(value)}
                    className={mode === value ? 'btn-primary' : 'btn-secondary'}>
              {label}
            </button>
          ))}
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Step 1 — gateway */}
        <Card title={mode === 'comparison' ? '1 · Select gateways' : '1 · Select gateway'}>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {usable.map((gateway) => {
              const isSelected = mode === 'comparison'
                ? comparisonCodes.includes(gateway.code)
                : gatewayCode === gateway.code
              const disabled = !gateway.configured
              return (
                <button key={gateway.code} type="button" disabled={disabled}
                        onClick={() => {
                          if (mode === 'comparison') {
                            setComparisonCodes((current) => current.includes(gateway.code)
                              ? current.filter((code) => code !== gateway.code)
                              : [...current, gateway.code])
                          } else {
                            setGatewayCode(gateway.code)
                          }
                        }}
                        className={`rounded-xl border p-4 text-left transition
                          ${isSelected ? 'border-accent-500 bg-accent-50 ring-1 ring-accent-500'
                                       : 'border-ink-200 bg-white hover:border-ink-300'}
                          ${disabled ? 'cursor-not-allowed opacity-60' : ''}`}>
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-semibold text-ink-900">{gateway.name}</span>
                    {gateway.is_simulated && <Badge tone="warn">simulator</Badge>}
                  </div>
                  <p className={`mt-1 text-xs ${gateway.configured ? 'text-emerald-600'
                                                                   : 'text-amber-600'}`}>
                    {gateway.status}
                  </p>
                  <p className="mt-2 text-xs text-ink-500">
                    {[gateway.supports_hpp && 'HPP', gateway.supports_direct && 'Direct API']
                      .filter(Boolean).join(' · ')}
                  </p>
                  {!gateway.configured && gateway.missing_fields.length > 0 && (
                    <p className="mt-1 text-xs text-ink-500">
                      Missing: {gateway.missing_fields.join(', ')}
                    </p>
                  )}
                </button>
              )
            })}
          </div>
        </Card>

        {/* Step 2 — integration */}
        <Card title="2 · Select integration type">
          <div className="flex flex-wrap gap-3">
            {(['hpp', 'direct'] as const).map((value) => {
              const supported = mode === 'comparison'
                ? comparisonCodes.every((code) => {
                    const gateway = gateways.find((item) => item.code === code)
                    return value === 'hpp' ? gateway?.supports_hpp : gateway?.supports_direct
                  })
                : value === 'hpp' ? selected?.supports_hpp : selected?.supports_direct
              return (
                <button key={value} type="button" disabled={!supported}
                        onClick={() => setIntegration(value)}
                        className={`rounded-xl border px-5 py-4 text-left transition
                          ${integration === value
                            ? 'border-accent-500 bg-accent-50 ring-1 ring-accent-500'
                            : 'border-ink-200 bg-white hover:border-ink-300'}
                          ${!supported ? 'cursor-not-allowed opacity-50' : ''}`}>
                  <span className="block text-sm font-semibold text-ink-900">
                    {integrationLabel(value)}
                  </span>
                  <span className="mt-1 block text-xs text-ink-500">
                    {value === 'hpp'
                      ? 'Redirect to the gateway’s own page; two measured legs.'
                      : 'Server-to-server; completes in one call sequence.'}
                  </span>
                </button>
              )
            })}
          </div>

          {selected?.documented_calls?.[integration] && (
            <p className="mt-4 text-xs text-ink-500">
              Documented minimum for {selected.name}:{' '}
              <span className="font-medium text-ink-700">
                {selected.documented_calls[integration]}
              </span>. The measured count is recorded against every transaction, so any
              divergence from the documentation is visible.
            </p>
          )}

          {integration === 'hpp' && mode !== 'single' && (
            <div className="mt-4">
              <Note>
                An automated HPP run measures session creation only — completing a hosted
                payment needs a person on the gateway’s page. Those transactions stay
                pending, and the hosted-page timings come from single transactions run by
                hand.
              </Note>
            </div>
          )}
        </Card>

        {/* Step 3 — transaction details */}
        <Card title="3 · Transaction details">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <div>
              <label className="label" htmlFor="amount">Amount</label>
              <input id="amount" className="input" type="number" step="0.01" min="0.01"
                     value={amount} onChange={(e) => setAmount(e.target.value)} required />
            </div>
            <div>
              <label className="label" htmlFor="currency">Currency</label>
              <select id="currency" className="input" value={currency}
                      onChange={(e) => setCurrency(e.target.value)}>
                {CURRENCIES.map((code) => <option key={code} value={code}>{code}</option>)}
              </select>
              {currencyUnsupported && (
                <p className="hint text-amber-600">
                  {selected?.name} is configured for{' '}
                  {selected?.supported_currencies.join(', ')} — this will be refused.
                </p>
              )}
            </div>
            <div>
              <label className="label" htmlFor="methodology">Test methodology</label>
              <select id="methodology" className="input" value={methodology}
                      onChange={(e) => setMethodology(e.target.value)}>
                <option value="mixed">Mixed</option>
                <option value="cold">Cold</option>
                <option value="warm">Warm</option>
              </select>
              <p className="hint">
                Our own label for how the test was run. It says nothing about the
                gateway’s internal state, which cannot be observed from outside.
              </p>
            </div>

            {mode === 'single' && (
              <div>
                <label className="label" htmlFor="reference">Order / reference ID</label>
                <input id="reference" className="input" value={reference}
                       placeholder="Generated automatically when blank"
                       onChange={(e) => setReference(e.target.value)} />
              </div>
            )}

            <div className="sm:col-span-2">
              <label className="label" htmlFor="description">Description</label>
              <input id="description" className="input" value={description}
                     onChange={(e) => setDescription(e.target.value)} />
            </div>
          </div>

          {mode !== 'single' && (
            <div className="mt-6 grid gap-4 border-t border-ink-200 pt-6 sm:grid-cols-3">
              <div>
                <label className="label" htmlFor="name">
                  {mode === 'run' ? 'Run name' : 'Test name'}
                </label>
                <input id="name" className="input" value={runName}
                       placeholder="Generated from the gateway and integration"
                       onChange={(e) => setRunName(e.target.value)} />
              </div>
              <div>
                <label className="label" htmlFor="count">
                  Transactions {mode === 'comparison' && 'per gateway'}
                </label>
                <input id="count" className="input" type="number" min={1}
                       max={limits.max_transactions_per_run} value={transactionCount}
                       onChange={(e) => setTransactionCount(Number(e.target.value))} />
                <p className="hint">Maximum {limits.max_transactions_per_run} per run.</p>
              </div>
              <div>
                <label className="label" htmlFor="interval">Seconds between transactions</label>
                <input id="interval" className="input" type="number" step="0.5"
                       min={limits.min_interval_seconds} value={interval}
                       onChange={(e) => setInterval(Number(e.target.value))} />
                <p className="hint">
                  Minimum {limits.min_interval_seconds}s. Lower values are raised to the
                  floor automatically.
                </p>
              </div>
              <div className="sm:col-span-3">
                <Note>{limits.note}</Note>
              </div>
            </div>
          )}
        </Card>

        {message && (
          <p className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
            {message}
          </p>
        )}

        <div className="flex items-center gap-3">
          <button type="submit" className="btn-primary" disabled={busy || !canSubmit}>
            {busy ? 'Starting…'
              : mode === 'single' ? 'Start transaction'
              : mode === 'run' ? 'Start benchmark run'
              : 'Start comparison test'}
          </button>
          {mode === 'comparison' && comparisonCodes.length < 2 && (
            <span className="text-sm text-ink-500">
              Select at least two gateways to compare.
            </span>
          )}
          {mode !== 'comparison' && selected && !selected.configured && (
            <span className="text-sm text-amber-600">
              {selected.name} needs credentials before it can be benchmarked.
            </span>
          )}
        </div>
      </form>
    </div>
  )
}
