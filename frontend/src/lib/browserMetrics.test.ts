/**
 * Browser metrics tests.
 *
 * The important property is restraint: this module reports what the browser actually
 * exposed and nothing else. A metric the browser withheld must be absent, not zero,
 * and time spent on the gateway's origin must be labelled as the composite it is
 * rather than passed off as a page load.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { collectPageMetrics, collectReturnMetrics, markRedirect } from './browserMetrics'

function fakeNavigationEntry(overrides: Record<string, number> = {}) {
  const base = {
    startTime: 0, domainLookupStart: 10, domainLookupEnd: 22,
    connectStart: 22, connectEnd: 48, secureConnectionStart: 30,
    requestStart: 50, responseStart: 180, responseEnd: 210,
    domContentLoadedEventEnd: 320, loadEventEnd: 460, redirectCount: 0,
    redirectStart: 0, redirectEnd: 0,
  }
  return { ...base, ...overrides }
}

function stubPerformance(entry: object | null) {
  vi.stubGlobal('performance', {
    getEntriesByType: (kind: string) => (kind === 'navigation' && entry ? [entry] : []),
  })
}

describe('collectReturnMetrics', () => {
  beforeEach(() => sessionStorage.clear())
  afterEach(() => vi.unstubAllGlobals())

  it('reports nothing when there is nothing to report', () => {
    stubPerformance(null)
    expect(collectReturnMetrics('tx-1')).toBeNull()
  })

  it('derives same-origin timings from the return navigation', () => {
    stubPerformance(fakeNavigationEntry())
    const collected = collectReturnMetrics('tx-1')!
    expect(collected.metrics.return_dns_ms).toBe(12)
    expect(collected.metrics.return_tcp_ms).toBe(26)
    expect(collected.metrics.return_ttfb_ms).toBe(130)
    expect(collected.metrics.return_load_complete_ms).toBe(460)
  })

  it('omits TLS timing when the connection was not secure', () => {
    stubPerformance(fakeNavigationEntry({ secureConnectionStart: 0 }))
    // Absent, not zero: an unencrypted connection has no handshake to measure.
    expect(collectReturnMetrics('tx-1')!.metrics).not.toHaveProperty('return_tls_ms')
  })

  it('measures time away from this origin from the recorded redirect', () => {
    stubPerformance(fakeNavigationEntry())
    markRedirect('tx-2')
    const collected = collectReturnMetrics('tx-2')!
    // Named for what it contains — the hosted page, the customer and any 3DS — rather
    // than being reported as a page load time the browser never saw.
    expect(collected.metrics).toHaveProperty('time_away_from_origin_ms')
    expect(collected.metrics.time_away_from_origin_ms).toBeGreaterThanOrEqual(0)
  })

  it('is labelled cross-origin, because the hosted page was', () => {
    stubPerformance(fakeNavigationEntry())
    expect(collectReturnMetrics('tx-3')!.origin_scope).toBe('cross-origin')
  })

  it('does not report the epoch timestamp as if it were a duration', () => {
    stubPerformance(fakeNavigationEntry())
    markRedirect('tx-epoch')
    const collected = collectReturnMetrics('tx-epoch')!
    expect(collected.metrics).not.toHaveProperty('redirect_initiated_epoch_ms')
    for (const value of Object.values(collected.metrics)) {
      // A wall-clock instant stored in a milliseconds column would wreck any average
      // taken over it.
      expect(value).toBeLessThan(1_000_000_000)
    }
  })

  it('reports once per transaction, so a refresh cannot duplicate rows', () => {
    stubPerformance(fakeNavigationEntry())
    expect(collectReturnMetrics('tx-once')).not.toBeNull()
    expect(collectReturnMetrics('tx-once')).toBeNull()
  })

  it('clears the marker so a refresh cannot double-count', () => {
    stubPerformance(fakeNavigationEntry())
    markRedirect('tx-4')
    expect(collectReturnMetrics('tx-4')!.metrics).toHaveProperty('time_away_from_origin_ms')
    // The second call returns nothing at all, rather than a set without the marker.
    expect(collectReturnMetrics('tx-4')).toBeNull()
  })
})

describe('collectPageMetrics', () => {
  afterEach(() => vi.unstubAllGlobals())

  beforeEach(() => sessionStorage.clear())

  it('reports a same-origin page load under the name of what the page is', () => {
    stubPerformance(fakeNavigationEntry())
    const collected = collectPageMetrics('hosted_page', 'tx-page')!
    expect(collected.origin_scope).toBe('same-origin')
    expect(collected.page_load_time_ms).toBe(460)
    expect(collected.metrics).toHaveProperty('hosted_page_load_ms')
  })

  it('reports once per page and transaction', () => {
    stubPerformance(fakeNavigationEntry())
    expect(collectPageMetrics('widget_page', 'tx-page-2')).not.toBeNull()
    expect(collectPageMetrics('widget_page', 'tx-page-2')).toBeNull()
  })

  it('returns null when the browser exposes no navigation timing', () => {
    stubPerformance(null)
    expect(collectPageMetrics('hosted_page', 'tx-none')).toBeNull()
  })
})
