/**
 * Browser-side performance measurement for hosted checkout (specification section 10).
 *
 * What this can and cannot see is worth being precise about, because the honest
 * answer shapes the whole feature:
 *
 * * **The redirect out** is measurable — the moment the browser leaves for the
 *   gateway is recorded here, in `sessionStorage`, keyed by transaction.
 * * **The hosted page itself is not.** It is another origin. The browser exposes no
 *   navigation timing for a document this script never ran in, and there is no
 *   supported way to obtain it. This module does not try; attempting to would mean
 *   defeating a browser security control, which section 10 rules out explicitly.
 * * **The return leg is measurable** in full: when the browser lands back on our own
 *   origin, Navigation Timing gives DNS, TCP, TLS, TTFB, DOM content loaded and load
 *   complete for that navigation, plus `redirectCount` and `redirectStart`.
 * * **Time away from our origin** is the wall-clock gap between the redirect being
 *   initiated and the return page loading. It contains the hosted page load, the
 *   customer's typing and any 3DS challenge, and it is labelled as that composite
 *   rather than being passed off as a page load time.
 *
 * Every value is reported as what it is. Nothing is inferred, and a metric the
 * browser withholds is simply absent.
 */

const STORAGE_PREFIX = 'burapay.hpp.'
const REPORTED_PREFIX = 'burapay.hpp.reported.'

interface RedirectMark {
  transactionId: string
  /** Epoch milliseconds — this has to survive a cross-origin round trip. */
  leftAt: number
}

export function markRedirect(transactionId: string): void {
  try {
    const mark: RedirectMark = { transactionId, leftAt: Date.now() }
    sessionStorage.setItem(STORAGE_PREFIX + transactionId, JSON.stringify(mark))
  } catch {
    /* private mode, or storage disabled — metrics are optional, the benchmark is not */
  }
}

function readMark(transactionId: string): RedirectMark | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_PREFIX + transactionId)
    return raw ? (JSON.parse(raw) as RedirectMark) : null
  } catch {
    return null
  }
}

function clearMark(transactionId: string): void {
  try {
    sessionStorage.removeItem(STORAGE_PREFIX + transactionId)
  } catch {
    /* nothing to do */
  }
}

function alreadyReported(transactionId: string): boolean {
  try {
    return sessionStorage.getItem(REPORTED_PREFIX + transactionId) !== null
  } catch {
    return false
  }
}

function markReported(transactionId: string): void {
  try {
    sessionStorage.setItem(REPORTED_PREFIX + transactionId, '1')
  } catch {
    /* metrics are optional; the benchmark is not */
  }
}

function navigationEntry(): PerformanceNavigationTiming | null {
  if (typeof performance === 'undefined' || !performance.getEntriesByType) return null
  const entries = performance.getEntriesByType('navigation') as PerformanceNavigationTiming[]
  return entries.length ? entries[0] : null
}

export interface CollectedMetrics {
  metrics: Record<string, number>
  origin_scope: 'same-origin' | 'cross-origin'
  page_load_time_ms?: number
}

/**
 * Collect what the browser can actually report about the return navigation.
 *
 * Returns `null` when there is nothing to report, so the caller sends nothing rather
 * than posting a row of zeros.
 */
export function collectReturnMetrics(transactionId: string): CollectedMetrics | null {
  // Report once per transaction. Without this a refresh — or React's development
  // double-invocation of effects — files the same navigation twice, and duplicate
  // rows would quietly skew any average taken over them.
  if (alreadyReported(transactionId)) return null

  const mark = readMark(transactionId)
  const nav = navigationEntry()
  if (!mark && !nav) return null

  const metrics: Record<string, number> = {}

  if (mark) {
    // Everything that happened while the browser was on the gateway's origin: the
    // hosted page load, the customer, and any 3DS challenge. Named for what it is.
    // The epoch timestamp behind it is not reported — a wall-clock instant is not a
    // duration, and storing one in a milliseconds column would corrupt the averages.
    metrics.time_away_from_origin_ms = Math.max(0, Date.now() - mark.leftAt)
  }

  if (nav) {
    // Timings for the return navigation, which is same-origin and therefore complete.
    const add = (name: string, value: number) => {
      if (Number.isFinite(value) && value >= 0) metrics[name] = Math.round(value * 100) / 100
    }
    add('return_dns_ms', nav.domainLookupEnd - nav.domainLookupStart)
    add('return_tcp_ms', nav.connectEnd - nav.connectStart)
    if (nav.secureConnectionStart > 0) {
      add('return_tls_ms', nav.connectEnd - nav.secureConnectionStart)
    }
    add('return_ttfb_ms', nav.responseStart - nav.requestStart)
    add('return_response_ms', nav.responseEnd - nav.responseStart)
    add('return_dom_content_loaded_ms', nav.domContentLoadedEventEnd - nav.startTime)
    add('return_load_complete_ms', nav.loadEventEnd - nav.startTime)
    if (nav.redirectCount > 0) {
      add('return_redirect_ms', nav.redirectEnd - nav.redirectStart)
      metrics.return_redirect_count = nav.redirectCount
    }
  }

  clearMark(transactionId)
  if (Object.keys(metrics).length === 0) return null
  markReported(transactionId)

  return {
    metrics,
    // The gateway's own page is on another origin and exposes nothing to us. Saying
    // "cross-origin" records that limit rather than implying the set is complete.
    origin_scope: 'cross-origin',
  }
}

/**
 * Same-origin timings for a page this application rendered itself — a mounted gateway
 * component, or MockPay's simulated hosted page.
 *
 * ``prefix`` names what the page is, because the distinction matters: MockPay's page
 * genuinely is the hosted page, while a mounted widget is our page hosting someone
 * else's component. ``key`` makes the report idempotent, so a refresh or React's
 * development double-invocation cannot file the same navigation twice.
 */
export function collectPageMetrics(prefix: 'hosted_page' | 'widget_page',
                                   key: string): CollectedMetrics | null {
  if (alreadyReported(`${prefix}.${key}`)) return null
  const nav = navigationEntry()
  if (!nav) return null
  const load = nav.loadEventEnd > 0 ? nav.loadEventEnd - nav.startTime
    : nav.domContentLoadedEventEnd - nav.startTime
  if (!Number.isFinite(load) || load < 0) return null

  markReported(`${prefix}.${key}`)
  return {
    metrics: {
      [`${prefix}_dom_content_loaded_ms`]:
        Math.round((nav.domContentLoadedEventEnd - nav.startTime) * 100) / 100,
      [`${prefix}_load_ms`]: Math.round(load * 100) / 100,
    },
    origin_scope: 'same-origin',
    page_load_time_ms: Math.round(load * 100) / 100,
  }
}
