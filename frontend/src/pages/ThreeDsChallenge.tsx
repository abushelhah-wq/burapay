/**
 * The 3DS challenge page — where the cardholder answers their issuer.
 *
 * A Direct API payment that stops for 3DS is a two-legged flow, exactly like a hosted
 * checkout: the server got as far as it could, the customer has to do something, and
 * only then can the payment finish. The time spent here belongs to the customer and
 * is recorded as customer interaction time, never as the gateway's API latency.
 *
 * Two shapes arrive, and which one depends on the issuer rather than on the gateway:
 *
 * * a URL to navigate to, which is a plain redirect;
 * * an auto-submitting form, which is markup this application received from somewhere
 *   else. That is rendered inside a sandboxed iframe rather than injected into the
 *   page. A sandboxed frame is its own opaque origin: the form can post itself to the
 *   issuer, and it cannot read this application's DOM, storage or session.
 *
 * Either way the issuer sends the browser back to the transaction's return URL, where
 * the server finishes the payment and redirects to the transaction page.
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import { api } from '../api/client'
import type { ThreeDsChallenge as Challenge } from '../api/types'
import { Card, ErrorNotice, Spinner } from '../components/ui'
import { money } from '../lib/format'

export default function ThreeDsChallenge() {
  const { id = '' } = useParams()
  const [challenge, setChallenge] = useState<Challenge | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    api.threeDsChallenge(id).then(setChallenge).catch(setError)
  }, [id])

  useEffect(() => {
    // A URL is a redirect and nothing more. Replace rather than push, so the back
    // button does not land the cardholder on a challenge they have already answered.
    if (challenge?.mode === 'redirect' && challenge.challenge_url) {
      window.location.replace(challenge.challenge_url)
    }
  }, [challenge])

  if (error) return <ErrorNotice error={error} />
  if (!challenge) return <Spinner label="Loading the challenge" />

  if (challenge.mode === 'redirect') {
    return <Spinner label="Sending you to your bank" />
  }

  if (!challenge.challenge_html) {
    return (
      <Card title="Nothing to show">
        <p className="text-sm text-ink-600">
          This payment is waiting for a 3DS challenge, but the gateway did not return
          one — neither a URL to send you to nor a form to render. The payment cannot
          be completed from here.
        </p>
        <p className="mt-2 text-sm text-ink-600">
          The full gateway response is on the{' '}
          <a className="font-medium underline" href={`/transactions/${id}`}>
            transaction page
          </a>, under the authentication call.
        </p>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">Verify with your bank</h1>
        <p className="text-sm text-ink-500">
          {money(challenge.amount, challenge.currency)} ·{' '}
          {challenge.gateway_code} · {challenge.environment}. Your bank is asking for a
          one-time password. The time you spend here is recorded as customer
          interaction, not as gateway latency.
        </p>
      </header>

      <div className="card overflow-hidden p-0">
        {/*
          sandbox without allow-same-origin: the frame gets an opaque origin, so the
          issuer's markup can submit itself and navigate, and can reach nothing of
          ours. allow-top-navigation-by-user-activation lets the issuer return the
          whole window to our return URL when the cardholder submits.
        */}
        <iframe
          title="3-D Secure challenge"
          className="h-[36rem] w-full border-0"
          sandbox="allow-forms allow-scripts allow-top-navigation-by-user-activation"
          srcDoc={challenge.challenge_html}
        />
      </div>

      <p className="text-xs text-ink-500">
        Not seeing a prompt? Some issuers only accept the challenge in a full window.
        The payment stays pending until it completes, and the transaction page shows
        where it stopped.
      </p>
    </div>
  )
}
