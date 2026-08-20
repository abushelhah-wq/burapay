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
import { Link, useNavigate } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import type { AppSettings, Gateway } from '../api/types'
import { Badge, Card, ErrorNotice, Note, Spinner } from '../components/ui'
import { markRedirect } from '../lib/browserMetrics'
import { PAYMENT_MODE_HINTS, PAYMENT_MODE_LABELS, integrationLabel } from '../lib/format'

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
  const [paymentMode, setPaymentMode] = useState('standard')
  const [amount, setAmount] = useState('1.00')
  const [currency, setCurrency] = useState('SAR')
  const [reference, setReference] = useState('')
  const [description, setDescription] = useState('BuraPay benchmark transaction')
  const [methodology, setMethodology] = useState('mixed')
  const [transactionCount, setTransactionCount] = useState(20)
  const [interval, setInterval] = useState(2)
  const [runName, setRunName] = useState('')
  // The payment form. Only ever a sandbox test card, only ever held here in the
  // browser and in one server request — nothing about it is saved anywhere.
  const [cardNumber, setCardNumber] = useState('')
  const [cardMonth, setCardMonth] = useState('')
  const [cardYear, setCardYear] = useState('')
  const [cardCvc, setCardCvc] = useState('')
  const [cardHolder, setCardHolder] = useState('BuraPay Benchmark')
  const [tokenId, setTokenId] = useState('')
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

  // A stored-token payment needs both a token and the agreement it was stored under.
  // Neither is a required credential — the gateway works fine without them — so this
  // is read from which keys are stored rather than from the missing-field list.
  const tokenReady = Boolean(
    selected?.configured_fields?.includes('token_id')
    && selected?.configured_fields?.includes('agreement_id'))

  useEffect(() => {
    // Keep the integration and mode valid when the gateway changes.
    if (!selected) return
    if (integration === 'hpp' && !selected.supports_hpp) setIntegration('direct')
    if (integration === 'direct' && !selected.supports_direct) setIntegration('hpp')
    const modes = selected.supported_payment_modes ?? ['standard']
    if (!modes.includes(paymentMode)) setPaymentMode('standard')
  }, [selected, integration, paymentMode])

  if (error) return <ErrorNotice error={error} />
  if (!gateways || !settings || !limits) return <Spinner label="Loading gateways" />

  const usable = gateways.filter((item) => item.enabled)
  const cardEntryAllowed = settings.environment.allow_direct_card_entry
  const cardEntered = Boolean(cardNumber.trim() && cardMonth.trim() && cardYear.trim()
    && cardCvc.trim())
  // A Store card run has to see a card: there is nothing to store otherwise.
  const cardModeNeedsInput = paymentMode === 'store_card'
  const currencyUnsupported = Boolean(
    selected && selected.supported_currencies.length &&
    !selected.supported_currencies.includes(currency))

  async function startSingle() {
    const result = await api.startTransaction({
      gateway_code: gatewayCode, integration_type: integration,
      amount: Number(amount), currency, description,
      reference: reference || undefined, methodology,
      payment_mode: integration === 'direct' ? paymentMode : 'standard',
      // Card details go to Direct API and nowhere else: a hosted checkout collects
      // them on the provider's own page, and sending them here would be card data
      // this application had no reason to receive.
      card: integration === 'direct' && cardEntered ? {
        number: cardNumber, month: cardMonth, year: cardYear,
        cvc: cardCvc, holder: cardHolder || 'BuraPay Benchmark',
      } : undefined,
      token_id: integration === 'direct' && tokenId.trim() ? tokenId.trim() : undefined,
    })
    if (result.requires_customer_action && result.redirect_url) {
      // The issuer wants the cardholder — a 3DS challenge or an OTP. Everything from
      // here until they come back is their time, and is recorded as such.
      markRedirect(result.transaction_id)
      if (result.redirect_url.startsWith('http')) {
        window.location.href = result.redirect_url
      } else {
        navigate(result.redirect_url)
      }
      return
    }
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
      payment_mode: integration === 'direct' ? paymentMode : 'standard',
    })
    navigate(`/runs/${run.id}`)
  }

  async function startComparison() {
    const test = await api.createComparisonTest({
      name: runName || `${integrationLabel(integration)} comparison — ${currency}`,
      gateway_codes: comparisonCodes, integration_type: integration,
      transactions_per_gateway: transactionCount, amount: Number(amount), currency,
      interval_seconds: interval, methodology,
      payment_mode: integration === 'direct' ? paymentMode : 'standard',
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

  const configuredComparisonCodes = comparisonCodes.filter((code) =>
    gateways.find((item) => item.code === code)?.configured)
  const canSubmit = mode === 'comparison'
    ? configuredComparisonCodes.length >= 2
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
              // An unconfigured gateway stays selectable on purpose. Making the card
              // unclickable leaves someone who wants Geidea with a dead rectangle and
              // no way to find out why; selecting it shows what is missing and links
              // straight to the page that fixes it. The submit button is where the
              // block belongs.
              return (
                <button key={gateway.code} type="button"
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
                          ${gateway.configured ? '' : 'opacity-75'}`}>
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

          {integration === 'direct' && (selected?.supported_payment_modes?.length ?? 1) > 1 && (
            <div className="mt-6 border-t border-ink-200 pt-6">
              <p className="label">Payment mode</p>
              <div className="grid gap-3 sm:grid-cols-3">
                {(selected?.supported_payment_modes ?? ['standard']).map((value) => (
                  <button key={value} type="button" onClick={() => setPaymentMode(value)}
                          className={`rounded-xl border p-3 text-left transition
                            ${paymentMode === value
                              ? 'border-accent-500 bg-accent-50 ring-1 ring-accent-500'
                              : 'border-ink-200 bg-white hover:border-ink-300'}`}>
                    <span className="block text-sm font-semibold text-ink-900">
                      {PAYMENT_MODE_LABELS[value] ?? value}
                    </span>
                    <span className="mt-1 block text-xs text-ink-500">
                      {PAYMENT_MODE_HINTS[value]}
                    </span>
                  </button>
                ))}
              </div>

              {paymentMode === 'token' && !tokenReady && (
                <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3
                              text-xs text-amber-800">
                  {selected?.name} has no stored card configured yet. A stored-token
                  payment needs both a <strong>Card token ID</strong> and an{' '}
                  <strong>Agreement ID</strong> on its settings. Run a{' '}
                  <strong>Store card (tokenize)</strong> payment first — the token it
                  returns is shown on the transaction page, ready to paste in.{' '}
                  <Link to={`/settings?gateway=${selected?.code}`}
                        className="font-medium underline">Open settings</Link>
                </p>
              )}
              {paymentMode === 'store_card' && (
                <p className="mt-3 text-xs text-ink-500">
                  This pays and stores the card. The token and agreement come back on
                  the transaction page; paste them into the gateway’s settings to charge
                  the card again later without card details.
                </p>
              )}
            </div>
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

        {/* Step 3 — payment details, for the integration that needs them */}
        {mode === 'single' && integration === 'direct' && paymentMode !== 'token' && (
          <Card title="3 · Payment details">
            <p className="text-sm text-ink-500">
              Direct API sends the payment server-to-server, so this application has to
              be given something to charge. Either works: a sandbox test card, or a card
              token the gateway already holds.
            </p>

            <div className="mt-5">
              <label className="label" htmlFor="token_id">Card token ID</label>
              <input id="token_id" className="input font-mono" value={tokenId}
                     autoComplete="off"
                     placeholder="Paste a token to pay without entering a card"
                     onChange={(e) => setTokenId(e.target.value)} />
              <p className="hint">
                A token is not card data, so this always works — including on an account
                that is not cleared to receive card numbers. A{' '}
                <strong>Store card (tokenize)</strong> payment mints one, and it is shown
                on that transaction’s page.
              </p>
            </div>

            <div className="mt-6 border-t border-ink-200 pt-6">
              <div className="flex items-center justify-between gap-3">
                <p className="label mb-0">Card details</p>
                <Badge tone={cardEntryAllowed ? 'warn' : 'neutral'}>
                  {cardEntryAllowed ? 'sandbox test cards only' : 'turned off'}
                </Badge>
              </div>

              {!cardEntryAllowed ? (
                <div className="mt-3 rounded-lg border border-ink-200 bg-ink-50 p-4
                                text-sm text-ink-600">
                  <p>
                    Typing a card number into this application is off. A PAN entered here
                    is card data held by this application, which is a decision for whoever
                    runs the deployment rather than a default.
                  </p>
                  <p className="mt-2">
                    To turn it on, set{' '}
                    <span className="font-mono text-xs">ALLOW_DIRECT_CARD_ENTRY=true</span>{' '}
                    in the deployment’s <span className="font-mono text-xs">.env</span> and
                    restart the API. Until then, paste a card token above, or use{' '}
                    <strong>{integrationLabel('hpp')}</strong>, where the card is entered on
                    the provider’s own page and never reaches this application at all.
                  </p>
                </div>
              ) : (
                <>
                  <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                    <div className="sm:col-span-2">
                      <label className="label" htmlFor="card_number">Card number</label>
                      <input id="card_number" className="input font-mono" inputMode="numeric"
                             autoComplete="off" value={cardNumber}
                             placeholder="4111 1111 1111 1111"
                             onChange={(e) => setCardNumber(e.target.value)} />
                    </div>
                    <div>
                      <label className="label" htmlFor="card_expiry">Expiry</label>
                      <div className="flex gap-2">
                        <input id="card_expiry" className="input" inputMode="numeric"
                               autoComplete="off" maxLength={2} placeholder="MM"
                               value={cardMonth}
                               onChange={(e) => setCardMonth(e.target.value)} />
                        <input className="input" inputMode="numeric" autoComplete="off"
                               maxLength={4} placeholder="YYYY" value={cardYear}
                               aria-label="Expiry year"
                               onChange={(e) => setCardYear(e.target.value)} />
                      </div>
                    </div>
                    <div>
                      <label className="label" htmlFor="card_cvc">CVV</label>
                      <input id="card_cvc" className="input" inputMode="numeric"
                             autoComplete="off" maxLength={4} placeholder="123"
                             value={cardCvc}
                             onChange={(e) => setCardCvc(e.target.value)} />
                    </div>
                    <div className="sm:col-span-2">
                      <label className="label" htmlFor="card_holder">Cardholder name</label>
                      <input id="card_holder" className="input" autoComplete="off"
                             value={cardHolder}
                             onChange={(e) => setCardHolder(e.target.value)} />
                    </div>
                  </div>
                  <p className="hint mt-3">
                    Sandbox test cards only. Nothing entered here is stored: the number
                    goes to the gateway on this one request, the CVV is dropped before
                    anything is written down, and any card number that reaches a log is
                    truncated to its last four digits.
                  </p>
                </>
              )}

              {!cardEntered && !tokenId.trim() && (
                <p className="mt-3 text-xs text-ink-500">
                  Leave both blank and the test card from{' '}
                  <Link to="/settings" className="font-medium underline">Settings</Link>{' '}
                  is used, which is what an automated benchmark run does.
                </p>
              )}
              {cardModeNeedsInput && !cardEntered && (
                <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3
                              text-xs text-amber-800">
                  A <strong>Store card (tokenize)</strong> payment stores the card it is
                  given. The configured test card will be used unless you enter one here.
                </p>
              )}
            </div>
          </Card>
        )}

        {/* Step 4 — transaction details */}
        <Card title={mode === 'single' && integration === 'direct' && paymentMode !== 'token'
                       ? '4 · Transaction details' : '3 · Transaction details'}>
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

        {/* A disabled button with no explanation is the single most confusing thing a
            form can do. When this cannot be pressed, the reason and the fix are right
            next to it. */}
        {!canSubmit && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm
                          text-amber-900">
            {mode === 'comparison' ? (
              <span>
                Select at least two <strong>configured</strong> gateways to compare.
                {comparisonCodes.length > configuredComparisonCodes.length && (
                  <> Some of the gateways you picked have no credentials yet.</>
                )}
              </span>
            ) : !selected ? (
              <span>Select a gateway above.</span>
            ) : (
              <>
                <p className="font-medium">
                  {selected.name} has no credentials yet, so nothing can be sent to it.
                </p>
                <p className="mt-1">
                  Add its sandbox credentials on the Settings page
                  {selected.missing_fields.length > 0 && (
                    <> — it still needs{' '}
                      <span className="font-mono text-xs">
                        {selected.missing_fields.join(', ')}
                      </span></>
                  )}. Or pick <strong>MockPay</strong> above to exercise the platform
                  without any gateway account.
                </p>
                <Link to={`/settings?gateway=${selected.code}`}
                      className="btn-secondary mt-3 inline-flex">
                  Configure {selected.name}
                </Link>
              </>
            )}
          </div>
        )}

        <div className="flex items-center gap-3">
          <button type="submit" className="btn-primary" disabled={busy || !canSubmit}>
            {busy ? 'Starting…'
              : mode === 'single' ? 'Start transaction'
              : mode === 'run' ? 'Start benchmark run'
              : 'Start comparison test'}
          </button>
        </div>
      </form>
    </div>
  )
}
