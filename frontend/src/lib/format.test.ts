/**
 * Formatting tests.
 *
 * These matter more than they look: the rule that an unmeasurable metric renders as
 * an em dash rather than 0 ms is a correctness requirement, not a cosmetic one. A
 * gateway that cannot report page load time must not appear to load instantly.
 */

import { describe, expect, it } from 'vitest'

import { NOT_MEASURED, integrationLabel, money, ms, msExact, percent, seconds } from './format'

describe('ms', () => {
  it('renders milliseconds below one second', () => {
    expect(ms(125)).toBe('125 ms')
  })

  it('promotes to seconds at and above one second', () => {
    expect(ms(1250)).toBe('1.25 sec')
    expect(ms(8430)).toBe('8.43 sec')
  })

  it('renders an unmeasured value as an em dash, never as zero', () => {
    expect(ms(null)).toBe(NOT_MEASURED)
    expect(ms(undefined)).toBe(NOT_MEASURED)
  })

  it('keeps a real zero distinct from an unmeasured value', () => {
    expect(ms(0)).toBe('0 ms')
  })
})

describe('msExact', () => {
  it('never promotes to seconds, so a column stays comparable', () => {
    expect(msExact(8430)).toBe('8430 ms')
  })
})

describe('seconds', () => {
  it('formats to two decimals', () => {
    expect(seconds(2340)).toBe('2.34 sec')
  })

  it('passes through the unmeasured case', () => {
    expect(seconds(null)).toBe(NOT_MEASURED)
  })
})

describe('percent', () => {
  it('formats with two decimals by default', () => {
    expect(percent(99.9713)).toBe('99.97%')
  })

  it('does not turn a missing rate into 0%', () => {
    expect(percent(null)).toBe(NOT_MEASURED)
  })
})

describe('money', () => {
  it('always shows two decimals with the currency', () => {
    expect(money(1, 'SAR')).toBe('1.00 SAR')
  })
})

describe('integrationLabel', () => {
  it('uses the labels from the specification', () => {
    expect(integrationLabel('hpp')).toBe('HPP / Hosted Checkout')
    expect(integrationLabel('direct')).toBe('Direct API')
  })

  it('falls back to the raw value for anything unknown', () => {
    expect(integrationLabel('future_mode')).toBe('future_mode')
  })
})
