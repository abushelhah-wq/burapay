/**
 * Host page for gateways whose hosted checkout is a component, not a redirect.
 *
 * Adyen's Sessions flow and HyperPay's Copy&Pay both return an identifier for their
 * own browser SDK rather than a URL to send the customer to, so the component is
 * mounted here, in our page, and the customer never leaves the origin until the
 * gateway's own script decides they should.
 *
 * The SDK is loaded from the provider's CDN. That is deliberate: specification
 * section 25 says to use each provider's official client-side components, and
 * reimplementing a payment form would put this application squarely in PCI scope.
 * Only values the gateway mints for client-side use are passed in — a session id, an
 * Adyen `sessionData` blob, a public client key. No secret reaches this page.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'

import { api } from '../api/client'
import { Card, ErrorNotice, Note, Spinner } from '../components/ui'
import { collectPageMetrics } from '../lib/browserMetrics'
import { money } from '../lib/format'

interface Handoff {
  transaction_id: string
  gateway_code: string
  status: string
  mode: string
  redirect_url: string | null
  return_url: string
  environment: string
  amount: number
  currency: string
  widget: Record<string, string>
}

const ADYEN_VERSION = '5.66.1'

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) return resolve()
    const script = document.createElement('script')
    script.src = src
    script.crossOrigin = 'anonymous'
    script.onload = () => resolve()
    script.onerror = () => reject(new Error(`Could not load ${src}`))
    document.head.appendChild(script)
  })
}

function loadStylesheet(href: string): void {
  if (document.querySelector(`link[href="${href}"]`)) return
  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = href
  document.head.appendChild(link)
}

export default function HppWidget() {
  const { id = '' } = useParams()
  const [handoff, setHandoff] = useState<Handoff | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [mounting, setMounting] = useState(true)
  const container = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    api.hppHandoff(id).then(setHandoff as (value: unknown) => void).catch(setError)
  }, [id])

  useEffect(() => { load() }, [load])

  // The widget page is our own origin, so its load timing is fully visible — unlike
  // a hosted page on the gateway's domain.
  useEffect(() => {
    if (!handoff) return
    const collected = collectPageMetrics('widget_page', handoff.transaction_id)
    if (collected) {
      void api.reportBrowserMetrics(handoff.transaction_id, collected).catch(() => {})
    }
  }, [handoff])

  useEffect(() => {
    if (!handoff || !container.current) return
    let cancelled = false

    async function mount(target: HTMLDivElement, data: Handoff) {
      try {
        if (data.gateway_code === 'geidea') {
          await mountGeidea(target, data)
        } else if (data.gateway_code === 'adyen') {
          await mountAdyen(target, data)
        } else if (data.gateway_code === 'hyperpay') {
          await mountHyperPay(target, data)
        } else {
          throw new Error(
            `${data.gateway_code} does not use a mounted component; it should have `
            + 'redirected instead.')
        }
      } catch (exc) {
        if (!cancelled) setError(exc)
      } finally {
        if (!cancelled) setMounting(false)
      }
    }

    void mount(container.current, handoff)
    return () => { cancelled = true }
  }, [handoff])

  if (error) return <ErrorNotice error={error} onRetry={load} />
  if (!handoff) return <Spinner label="Preparing the hosted checkout" />

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">
          {handoff.gateway_code} hosted checkout
        </h1>
        <p className="text-sm text-ink-500">
          {money(handoff.amount, handoff.currency)} · {handoff.environment} · the
          gateway’s own component is mounted below.
        </p>
      </header>

      <Card title="Payment">
        {mounting && <Spinner label="Loading the gateway’s component" />}
        <div ref={container} id="burapay-hpp-widget" />
      </Card>

      <Note>
        This gateway returns an identifier for its own checkout component rather than a
        URL to redirect to, so the component runs here. Card details are handled by the
        gateway’s script and never reach this application. Timing from this point on is
        the customer’s and the gateway’s, and it is recorded separately from the
        server-side API latency.
      </Note>
    </div>
  )
}

/**
 * Geidea's hosted checkout.
 *
 * The session response carries an id, not a URL — Geidea's page is a script that
 * takes over the window once started. So this loads that script and calls
 * `startPayment(sessionId)`, then hands the browser back to the return URL through
 * whichever callback fires.
 *
 * The success callback carries the order, and its id is passed back on the return
 * URL: the server confirms the outcome by fetching that order, rather than trusting
 * what the browser says happened.
 */
async function mountGeidea(target: HTMLDivElement, data: Handoff): Promise<void> {
  const sessionId = data.widget.session_id
  if (!sessionId) throw new Error('Geidea returned no session id to start the checkout with.')

  const scriptUrl = data.widget.script_url
  if (!scriptUrl) {
    throw new Error(
      'No Geidea hosted checkout script URL. Set it on the Settings page under '
      + '“Hosted checkout script URL”.')
  }

  try {
    await loadScript(scriptUrl)
  } catch {
    throw new Error(
      `Geidea's checkout script could not be loaded from ${scriptUrl}. If your account `
      + 'is served from a different host, set the correct URL on the Settings page.')
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const GeideaCheckout = (window as any).GeideaCheckout
  if (!GeideaCheckout) {
    throw new Error('The Geidea checkout script loaded but exposed no GeideaCheckout.')
  }

  const back = (query: string) => {
    const separator = data.return_url.includes('?') ? '&' : '?'
    window.location.href = `${data.return_url}${separator}${query}`
  }

  const payment = new GeideaCheckout(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (response: any) => {
      const orderId = response?.order?.orderId ?? response?.orderId ?? ''
      back(`orderId=${encodeURIComponent(orderId)}`)
    },
    () => back('result=error'),
    () => back('result=cancelled'),
  )
  // Geidea's script renders its own overlay, so the container stays empty; it is kept
  // so a future version that mounts inline has somewhere to go.
  target.dataset.geideaSession = sessionId
  payment.startPayment(sessionId)
}

async function mountAdyen(target: HTMLDivElement, data: Handoff): Promise<void> {
  const clientKey = data.widget.client_key
  if (!clientKey) {
    throw new Error(
      'Adyen’s Drop-in needs the Client Key. Add it to the Adyen configuration on the '
      + 'Settings page — it is a public value, safe to expose to the browser.')
  }
  loadStylesheet(`https://cdn.jsdelivr.net/npm/@adyen/adyen-web@${ADYEN_VERSION}/dist/adyen.css`)
  await loadScript(
    `https://cdn.jsdelivr.net/npm/@adyen/adyen-web@${ADYEN_VERSION}/dist/adyen.js`)

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const AdyenCheckout = (window as any).AdyenCheckout
  if (!AdyenCheckout) throw new Error('The Adyen Web SDK did not load.')

  const checkout = await AdyenCheckout({
    environment: data.environment === 'production' ? 'live' : 'test',
    clientKey,
    session: { id: data.widget.session_id, sessionData: data.widget.session_data },
    onPaymentCompleted: () => { window.location.href = data.return_url },
    onError: () => { window.location.href = `${data.return_url}?result=error` },
  })
  checkout.create('dropin').mount(target)
}

async function mountHyperPay(target: HTMLDivElement, data: Handoff): Promise<void> {
  const checkoutId = data.widget.checkout_id
  if (!checkoutId) throw new Error('HyperPay returned no checkout id to mount.')

  const host = data.environment === 'production'
    ? 'https://oppwa.com' : 'https://eu-test.oppwa.com'

  // Copy&Pay reads its configuration from a global set before the script loads.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(window as any).wpwlOptions = { style: 'card', locale: 'en' }
  await loadScript(`${host}/v1/paymentWidgets.js?checkoutId=${encodeURIComponent(checkoutId)}`)

  const form = document.createElement('form')
  form.action = data.return_url
  form.className = 'paymentWidgets'
  form.setAttribute('data-brands', 'VISA MASTER AMEX')
  target.appendChild(form)
}
