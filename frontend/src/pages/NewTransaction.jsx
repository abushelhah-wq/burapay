import React, { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, newIdempotencyKey } from '../lib/api.js'
import { ErrorNotice, Field, Notice } from '../components/common.jsx'

/**
 * Gateway picker -> integration type -> operation form (§5).
 *
 * Which controls appear is driven entirely by the backend's capability list.
 * An operation the adapter cannot perform is not rendered as a disabled button
 * with a tooltip -- it is explained, with a link to the documentation the
 * backend says it needs, because "why can't I click this" is the actual
 * question an operator has.
 */

const OPERATIONS = [
  {
    key: 'hpp',
    label: 'Hosted checkout',
    integration: 'HPP',
    capability: 'create_hpp_session',
    needsCard: false,
    needsAmount: true,
    help: 'Creates a session and hands you the redirect URL. The payment stays PENDING until it is reconciled by an order query.',
  },
  {
    key: 'sale',
    label: 'Direct API sale',
    integration: 'DIRECT_API',
    capability: 'direct_api_sale',
    needsCard: true,
    needsAmount: true,
    help: 'Authorise and capture in one operation, using a raw test card.',
  },
  {
    key: 'auth',
    label: 'Pre-authorisation',
    integration: 'DIRECT_API',
    capability: 'direct_api_auth',
    needsCard: true,
    needsAmount: true,
    help: 'Authorise without capturing. Geidea documents pre-auth as Saudi-platform-only; a merchant account without it reports that specifically rather than failing generically.',
  },
  {
    key: 'tokenize',
    label: 'Save card (tokenize)',
    integration: 'TOKENIZE',
    capability: 'tokenize',
    needsCard: false,
    needsAmount: false,
    needsCurrency: true,
    help:
      'Saves a card with no purchase. The customer enters the card on the ' +
      "gateway's own page, so this service never sees it, and the gateway " +
      'voids the underlying authorisation itself — nothing is charged. The ' +
      'token arrives later, on the callback.',
  },
  {
    key: 'cit',
    label: 'Charge saved card — customer present (CIT)',
    integration: 'TOKEN_CIT',
    capability: 'charge_with_token_cit',
    needsCard: false,
    needsAmount: true,
    needsToken: true,
    help:
      'One merchant round trip. The customer is present and completes the ' +
      'charge on the hosted page, so the transaction stays pending until the ' +
      'callback settles it.',
  },
  {
    key: 'mit',
    label: 'Charge saved card — merchant initiated (MIT)',
    integration: 'TOKEN_MIT',
    capability: 'charge_with_token_mit',
    needsCard: false,
    needsAmount: true,
    needsToken: true,
    help:
      'Two merchant round trips — session generation then pay-with-token, ' +
      'each signed with a different algorithm. Completes server-to-server ' +
      'with no customer present.',
  },
]

export default function NewTransaction({ flags }) {
  const navigate = useNavigate()
  const [gateways, setGateways] = useState([])
  const [gatewayCode, setGatewayCode] = useState('')
  const [operationKey, setOperationKey] = useState('sale')
  const [tokens, setTokens] = useState([])
  const [testCards, setTestCards] = useState(null)

  const [amount, setAmount] = useState('10.00')
  const [currency, setCurrency] = useState('SAR')
  const [tokenReference, setTokenReference] = useState('')
  const [card, setCard] = useState({
    number: '', exp_month: '', exp_year: '', cvv: '', holder_name: '',
  })

  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [busy, setBusy] = useState(false)
  // Generated once per form, so a double-click cannot create two transactions.
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey)

  useEffect(() => {
    api.gateways().then((list) => {
      setGateways(list)
      const usable = list.find((gateway) => gateway.configured && gateway.enabled)
      if (usable) setGatewayCode(usable.code)
    }).catch(setError)
  }, [])

  useEffect(() => {
    if (!gatewayCode) return
    api.testCards(gatewayCode).then(setTestCards).catch(() => setTestCards(null))
    api.tokens({ gateway: gatewayCode }).then(setTokens).catch(() => setTokens([]))
  }, [gatewayCode])

  const gateway = useMemo(
    () => gateways.find((item) => item.code === gatewayCode),
    [gateways, gatewayCode],
  )
  const operation = OPERATIONS.find((item) => item.key === operationKey)

  const available = useMemo(() => {
    if (!gateway) return []
    return OPERATIONS.filter(
      (item) =>
        gateway.capabilities.includes(item.capability) &&
        gateway.supported_integration_types.includes(item.integration),
    )
  }, [gateway])

  const unavailable = useMemo(() => {
    if (!gateway) return []
    return OPERATIONS.filter((item) => !available.includes(item))
  }, [gateway, available])

  useEffect(() => {
    // If the selected operation is not offered by this gateway, move to one
    // that is, rather than leaving a form that cannot be submitted.
    if (available.length && !available.some((item) => item.key === operationKey)) {
      setOperationKey(available[0].key)
    }
  }, [available, operationKey])

  function amountMinor() {
    // Minor units are computed here only to build the request. The backend is
    // authoritative and rejects anything with more precision than the currency
    // supports.
    const exponent = { KWD: 3, BHD: 3, OMR: 3, JOD: 3, TND: 3, JPY: 0, KRW: 0 }[currency] ?? 2
    const parsed = Number.parseFloat(amount)
    if (!Number.isFinite(parsed)) return NaN
    return Math.round(parsed * 10 ** exponent)
  }

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setResult(null)

    const minor = amountMinor()
    if (operation.needsAmount && !Number.isFinite(minor)) {
      setError(new Error('Enter a valid amount.'))
      setBusy(false)
      return
    }

    const base = { idempotency_key: idempotencyKey }
    const withAmount = { ...base, amount_minor: minor, currency }
    const cardPayload = {
      number: card.number.replace(/\s/g, ''),
      exp_month: Number(card.exp_month),
      exp_year: Number(card.exp_year),
      cvv: card.cvv,
      holder_name: card.holder_name || null,
    }

    try {
      let response
      if (operationKey === 'hpp') response = await api.hppSession(gatewayCode, withAmount)
      else if (operationKey === 'sale')
        response = await api.directSale(gatewayCode, { ...withAmount, card: cardPayload })
      else if (operationKey === 'auth')
        response = await api.directAuth(gatewayCode, { ...withAmount, card: cardPayload })
      else if (operationKey === 'tokenize')
        response = await api.tokenize(gatewayCode, { ...base, currency })
      else if (operationKey === 'cit')
        response = await api.chargeCit(gatewayCode, {
          ...withAmount, token_reference: tokenReference,
        })
      else if (operationKey === 'mit')
        response = await api.chargeMit(gatewayCode, {
          ...withAmount, token_reference: tokenReference,
        })

      setResult(response)
      // A new key for the next submission; the old one stays tied to the
      // transaction just created.
      setIdempotencyKey(newIdempotencyKey())

      // A session-based flow leaves something for the operator to do, so stay
      // on this page and show it. A completed server-to-server charge has
      // nothing left to do, so go straight to its call trail.
      const transaction = response.transaction ?? response
      if (!response.session_id && transaction?.id) {
        navigate(`/transactions/${transaction.id}`)
      }
    } catch (submitError) {
      setError(submitError)
    } finally {
      setBusy(false)
    }
  }

  const cardRequired = operation?.needsCard
  const cardBlocked = cardRequired && flags && !flags.allow_direct_card_entry

  return (
    <>
      <div className="page-head">
        <div>
          <h1>New transaction</h1>
          <p className="subtitle">
            Every call is timed and logged. The round-trip count is what the
            benchmark compares.
          </p>
        </div>
      </div>

      <ErrorNotice error={error} />

      {result?.session_id && (
        <Notice tone="good">
          <div>
            <strong>Session created.</strong>{' '}
            {result.redirect_url ? (
              <>
                <a href={result.redirect_url} target="_blank" rel="noreferrer">
                  Open the payment page
                </a>
                {result.redirect_field && (
                  <span className="small muted">
                    {' '}(redirect URL found under <code>{result.redirect_field}</code>)
                  </span>
                )}
              </>
            ) : (
              <span className="mono small">{result.session_id}</span>
            )}
          </div>
          {result.next_step && (
            <div className="small" style={{ marginTop: 6 }}>
              {result.next_step}
            </div>
          )}
          <div className="small" style={{ marginTop: 6 }}>
            The transaction stays pending until the callback settles it —{' '}
            <a href={`/transactions/${result.transaction.id}`}>view its call trail</a>.
          </div>
        </Notice>
      )}

      <form className="card" onSubmit={submit}>
        <div className="row">
          <Field label="Gateway">
            <select
              value={gatewayCode}
              onChange={(event) => setGatewayCode(event.target.value)}
            >
              <option value="">Select a gateway…</option>
              {gateways.map((item) => (
                <option
                  key={item.code}
                  value={item.code}
                  disabled={!item.configured || !item.enabled}
                >
                  {item.display_name}
                  {!item.configured ? ' — not configured' : ''}
                  {item.production_blocked ? ' — production blocked' : ''}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Operation">
            <select
              value={operationKey}
              onChange={(event) => setOperationKey(event.target.value)}
              disabled={!available.length}
            >
              {available.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </select>
          </Field>
        </div>

        {operation && <p className="small muted">{operation.help}</p>}

        {gateway && !gateway.configured && (
          <Notice tone="warn">
            {gateway.display_name} is missing credentials:{' '}
            {gateway.missing_credentials.join(', ')}. Add them on the{' '}
            <a href="/settings">Settings page</a>.
          </Notice>
        )}

        {gateway?.production_blocked && (
          <Notice tone="error">
            {gateway.display_name} holds production credentials and
            ALLOW_PRODUCTION_GATEWAYS is off. The backend will refuse to
            transact — this is a server-side gate, not a UI hint.
          </Notice>
        )}

        {(operation?.needsAmount || operation?.needsCurrency) && (
          <div className="row">
            {operation?.needsAmount && (
              <Field label="Amount">
                <input
                  value={amount}
                  onChange={(event) => setAmount(event.target.value)}
                  inputMode="decimal"
                />
              </Field>
            )}
            <Field
              label="Currency"
              hint={
                operation?.needsCurrency && !operation?.needsAmount
                  ? 'Nothing is charged, but the currency is part of the signed request.'
                  : undefined
              }
            >
              <select value={currency} onChange={(event) => setCurrency(event.target.value)}>
                {['SAR', 'AED', 'EGP', 'USD', 'EUR', 'KWD', 'BHD', 'QAR'].map((code) => (
                  <option key={code} value={code}>{code}</option>
                ))}
              </select>
            </Field>
          </div>
        )}

        {operation?.needsToken && (
          <Field
            label="Stored token"
            hint={tokens.length ? undefined : 'No tokens stored for this gateway yet.'}
          >
            <select
              value={tokenReference}
              onChange={(event) => setTokenReference(event.target.value)}
            >
              <option value="">Select a token…</option>
              {tokens.map((token) => (
                <option key={token.id} value={token.token_reference}>
                  {token.card_brand ?? 'card'} ···· {token.card_last4} — {token.token_reference}
                </option>
              ))}
            </select>
          </Field>
        )}

        {cardRequired && (
          <>
            <h3 style={{ marginTop: 18 }}>Test card</h3>
            {cardBlocked && (
              <Notice tone="error">
                ALLOW_DIRECT_CARD_ENTRY is off on this deployment, so the
                backend will reject raw-card requests with 403. This is enforced
                server-side; hiding the form would not be enough.
              </Notice>
            )}
            {testCards && testCards.cards.length === 0 && (
              <Notice tone="warn">
                No test cards are available from the gateway's documentation.{' '}
                {testCards.unavailable_reason}{' '}
                <a href={testCards.doc_url} target="_blank" rel="noreferrer">
                  {testCards.doc_url}
                </a>
              </Notice>
            )}
            {testCards && testCards.cards.length > 0 && (
              <div className="button-row" style={{ marginBottom: 12 }}>
                {testCards.cards.map((testCard) => (
                  <button
                    type="button"
                    key={testCard.number}
                    onClick={() =>
                      setCard({
                        number: testCard.number,
                        exp_month: String(testCard.exp_month),
                        exp_year: String(testCard.exp_year),
                        cvv: testCard.cvv,
                        holder_name: 'Benchmark Runner',
                      })
                    }
                  >
                    Use {testCard.brand ?? 'test'} ···· {testCard.number.slice(-4)}
                  </button>
                ))}
              </div>
            )}
            <div className="row">
              <Field label="Card number">
                <input
                  value={card.number}
                  onChange={(event) => setCard({ ...card, number: event.target.value })}
                  inputMode="numeric"
                  autoComplete="off"
                  required
                />
              </Field>
              <Field label="Month">
                <input
                  value={card.exp_month}
                  onChange={(event) => setCard({ ...card, exp_month: event.target.value })}
                  inputMode="numeric"
                  placeholder="12"
                  required
                />
              </Field>
              <Field label="Year">
                <input
                  value={card.exp_year}
                  onChange={(event) => setCard({ ...card, exp_year: event.target.value })}
                  inputMode="numeric"
                  placeholder="2030"
                  required
                />
              </Field>
              <Field label="CVV">
                <input
                  value={card.cvv}
                  onChange={(event) => setCard({ ...card, cvv: event.target.value })}
                  inputMode="numeric"
                  autoComplete="off"
                  required
                />
              </Field>
            </div>
            <Field label="Cardholder name">
              <input
                value={card.holder_name}
                onChange={(event) => setCard({ ...card, holder_name: event.target.value })}
              />
            </Field>
          </>
        )}

        <div className="button-row" style={{ marginTop: 8 }}>
          <button
            className="primary"
            type="submit"
            disabled={busy || !gatewayCode || !available.length || !gateway?.configured}
          >
            {busy ? 'Running…' : 'Run transaction'}
          </button>
          <span className="small muted" style={{ alignSelf: 'center' }}>
            Idempotency key <code className="mono">{idempotencyKey.slice(0, 20)}…</code>
          </span>
        </div>
      </form>

      {gateway && unavailable.length > 0 && (
        <div className="card">
          <h3>Not available for {gateway.display_name}</h3>
          <p className="small muted">
            These operations are hidden rather than offered, because the backend
            would refuse them. Each entry says what is missing.
          </p>
          <div className="gap-list">
            {unavailable.map((item) => {
              const gap = gateway.doc_gaps?.find(
                (candidate) => candidate.blocks_operation,
              )
              return (
                <div className="gap blocking" key={item.key}>
                  <strong>{item.label}</strong>
                  <div className="small muted">
                    The adapter does not implement <code>{item.capability}</code>.
                    {gateway.doc_urls?.[item.capability] && (
                      <>
                        {' '}Documentation:{' '}
                        <a
                          href={gateway.doc_urls[item.capability]}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {gateway.doc_urls[item.capability]}
                        </a>
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </>
  )
}
