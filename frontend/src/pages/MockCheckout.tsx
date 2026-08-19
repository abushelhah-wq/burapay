/**
 * MockPay's simulated hosted payment page.
 *
 * This is the one hosted page the platform serves itself, and it exists so the whole
 * two-legged HPP flow — redirect out, customer time, return, server-side confirmation,
 * browser metrics — can be exercised end to end before any real sandbox credential
 * exists. Every other gateway's hosted page belongs to that gateway and is never
 * reproduced here.
 *
 * It is deliberately styled to look nothing like a real payment page and says plainly
 * what it is. Nothing typed here goes anywhere: MockPay's outcome comes from its
 * configured scenario, not from this form.
 */

import { useEffect, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import { Card, ErrorNotice, Note, Spinner } from '../components/ui'
import { collectPageMetrics } from '../lib/browserMetrics'
import { money } from '../lib/format'

export default function MockCheckout() {
  const { sessionId = '' } = useParams()
  const [params] = useSearchParams()
  const transactionId = params.get('transaction') ?? ''
  const [handoff, setHandoff] = useState<{ amount: number; currency: string;
    gateway_code: string; return_url: string } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    // Unlike a real gateway's page, this one is served by us, so its load time is
    // actually observable and is recorded as the hosted page load. No real gateway's
    // hosted page can be measured this way, and none is claimed to be.
    if (!transactionId || !handoff) return
    const collected = collectPageMetrics('hosted_page', transactionId)
    if (collected) {
      void api.reportBrowserMetrics(transactionId, collected).catch(() => {})
    }
  }, [transactionId, handoff])

  useEffect(() => {
    if (!transactionId) {
      setError(new Error('This simulated page was opened without a transaction id.'))
      return
    }
    api.hppHandoff(transactionId).then(setHandoff).catch(setError)
  }, [transactionId])

  function finish(result: 'paid' | 'cancelled') {
    setBusy(true)
    // A real gateway sends the browser back to the return URL; so does this one. The
    // server confirms the outcome from there, exactly as it would for any gateway.
    const separator = handoff?.return_url.includes('?') ? '&' : '?'
    window.location.href =
      `${handoff?.return_url}${separator}result=${result}&session=${encodeURIComponent(sessionId)}`
  }

  if (error) return <ErrorNotice error={error} />
  if (!handoff) return <Spinner label="Loading the simulated payment page" />

  return (
    <div className="mx-auto max-w-xl space-y-6">
      <div className="rounded-xl border-2 border-dashed border-amber-400 bg-amber-50 p-4
                      text-center">
        <p className="text-sm font-semibold uppercase tracking-wide text-amber-800">
          Simulated hosted payment page
        </p>
        <p className="mt-1 text-xs text-amber-800">
          Served by BuraPay, not by a payment gateway. It exists so the hosted checkout
          round trip can be measured before any sandbox credentials are configured.
        </p>
      </div>

      <Card title="MockPay checkout">
        <dl className="mb-5 grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-ink-500">Amount</dt>
          <dd className="tabular font-medium">{money(handoff.amount, handoff.currency)}</dd>
          <dt className="text-ink-500">Session</dt>
          <dd className="break-all font-mono text-xs">{sessionId}</dd>
        </dl>

        <div className="space-y-3 opacity-60">
          <div>
            <label className="label" htmlFor="mock-card">Card number</label>
            <input id="mock-card" className="input" disabled
                   placeholder="No card is collected — this form is inert" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="mock-exp">Expiry</label>
              <input id="mock-exp" className="input" disabled placeholder="MM / YY" />
            </div>
            <div>
              <label className="label" htmlFor="mock-cvc">CVC</label>
              <input id="mock-cvc" className="input" disabled placeholder="—" />
            </div>
          </div>
        </div>

        <div className="mt-6 flex gap-3">
          <button type="button" className="btn-primary flex-1" disabled={busy}
                  onClick={() => finish('paid')}>
            {busy ? 'Returning…' : 'Pay now'}
          </button>
          <button type="button" className="btn-secondary" disabled={busy}
                  onClick={() => finish('cancelled')}>
            Cancel
          </button>
        </div>
      </Card>

      <Note>
        However long you spend on this page is recorded as customer interaction time,
        not as gateway latency — which is the distinction the whole platform is built
        around. The outcome comes from MockPay’s configured scenario, so nothing you
        type here changes it.
      </Note>
    </div>
  )
}
