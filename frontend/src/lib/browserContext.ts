/**
 * What this browser can see about itself, for the gateway's 3-D Secure risk engine.
 *
 * An issuer scores a payment partly on the browser it came from, and one given none of
 * that reaches for a challenge more often — which is why a purely server-to-server
 * Direct API call tends to get challenged. The server cannot know any of this, so the
 * page sends what it has.
 *
 * Nothing here is more identifying than what an ordinary HTTP request already carries.
 * The device id is a random value this browser keeps, not a fingerprint: it is not
 * derived from anything about the machine and is not tied to a person. Anything the
 * browser cannot see is left out rather than filled in with a plausible guess.
 */

const DEVICE_ID_KEY = 'burapay.device'

export interface BrowserContext {
  user_agent?: string
  language?: string
  time_zone_offset_minutes?: number
  device_id?: string
}

function deviceId(): string | undefined {
  try {
    const existing = localStorage.getItem(DEVICE_ID_KEY)
    if (existing) return existing
    const minted = `dev-${crypto.randomUUID().replace(/-/g, '').slice(0, 24)}`
    localStorage.setItem(DEVICE_ID_KEY, minted)
    return minted
  } catch {
    // Private browsing, a blocked storage partition, no crypto — all fine. The
    // gateway gets one fewer field rather than a fabricated one.
    return undefined
  }
}

export function browserContext(): BrowserContext {
  const context: BrowserContext = {}
  if (typeof navigator !== 'undefined') {
    if (navigator.userAgent) context.user_agent = navigator.userAgent.slice(0, 500)
    if (navigator.language) context.language = navigator.language.slice(0, 35)
  }
  const offset = new Date().getTimezoneOffset()
  // getTimezoneOffset is positive west of UTC, which is the opposite of how a
  // timezone is normally written. Sent as the browser reports it; the adapter that
  // needs it decides what to do with the sign.
  if (Number.isFinite(offset)) context.time_zone_offset_minutes = offset
  const id = deviceId()
  if (id) context.device_id = id
  return context
}
