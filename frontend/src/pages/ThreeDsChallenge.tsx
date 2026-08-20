/**
 * The 3DS challenge page — where the cardholder answers their issuer.
 *
 * A Direct API payment that stops for 3DS is a two-legged flow, exactly like a hosted
 * checkout: the server got as far as it could, the customer has to do something, and
 * only then can the payment finish. The time spent here belongs to the customer and
 * is recorded as customer interaction time, never as the gateway's API latency.
 *
 * This page only ever *navigates*. It does not render the issuer's markup, and it does
 * not frame it, for two separate reasons:
 *
 *  * A challenge that runs in an iframe ends by sending the browser back to this
 *    application's return URL — inside the frame. That return is refused by our own
 *    `X-Frame-Options: DENY`, and the cardholder is left looking at an empty box. At
 *    the top level the header never applies and the return is an ordinary navigation.
 *  * The markup came from somebody else. When the issuer gives a form rather than a
 *    URL, the browser goes to a backend route that serves it under
 *    `Content-Security-Policy: sandbox` — an opaque origin, so it can post itself to
 *    the issuer and reach nothing of ours.
 *
 * Either way the issuer returns the browser to the transaction's return leg, where the
 * server finishes the payment and redirects to the transaction page.
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import { api } from '../api/client'
import type { ThreeDsChallenge as Challenge } from '../api/types'
import { Card, ErrorNotice, Spinner } from '../components/ui'

export default function ThreeDsChallenge() {
  const { id = '' } = useParams()
  const [challenge, setChallenge] = useState<Challenge | null>(null)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    api.threeDsChallenge(id).then(setChallenge).catch(setError)
  }, [id])

  useEffect(() => {
    if (!challenge?.challenge_url) return
    // Replace rather than push, so the back button does not land the cardholder on a
    // challenge they have already answered.
    window.location.replace(challenge.challenge_url)
  }, [challenge])

  if (error) return <ErrorNotice error={error} />
  if (!challenge) return <Spinner label="Loading the challenge" />

  if (!challenge.challenge_url) {
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
          </a>{' '}
          and in the{' '}
          <a className="font-medium underline" href={`/logs?transaction=${id}`}>
            call log
          </a>, under the authentication call.
        </p>
      </Card>
    )
  }

  return <Spinner label="Sending you to your bank" />
}
