/**
 * Formatting helpers.
 *
 * Consistency matters more than cleverness here: the whole product is people
 * comparing numbers down a column, so a millisecond figure must look the same
 * everywhere (specification section 46).
 *
 *   125 ms      1.25 sec      99.97%
 *
 * A value that could not be measured renders as an em dash, never as 0 — section 9
 * is explicit that an unmeasurable metric must not be presented as a measured one.
 */

export const NOT_MEASURED = '—'

export function ms(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NOT_MEASURED
  if (value >= 1000) return `${(value / 1000).toFixed(2)} sec`
  return `${value.toFixed(digits)} ms`
}

/** Always milliseconds, never promoted to seconds — for columns that must align. */
export function msExact(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NOT_MEASURED
  return `${value.toFixed(digits)} ms`
}

export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NOT_MEASURED
  return `${(value / 1000).toFixed(2)} sec`
}

export function percent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return NOT_MEASURED
  return `${value.toFixed(digits)}%`
}

export function money(amount: number, currency: string): string {
  return `${amount.toFixed(2)} ${currency}`
}

export function count(value: number | null | undefined): string {
  if (value === null || value === undefined) return NOT_MEASURED
  return value.toLocaleString()
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return NOT_MEASURED
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return NOT_MEASURED
  return date.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

export function timeOnly(value: string | null | undefined): string {
  if (!value) return NOT_MEASURED
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return NOT_MEASURED
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return NOT_MEASURED
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return NOT_MEASURED
  const deltaSeconds = Math.round((then - Date.now()) / 1000)
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['second', 60], ['minute', 60], ['hour', 24], ['day', 30], ['month', 12],
  ]
  let value_ = deltaSeconds
  for (const [unit, size] of units) {
    if (Math.abs(value_) < size) {
      return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(
        Math.round(value_), unit)
    }
    value_ /= size
  }
  return new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
    .format(Math.round(value_), 'year')
}

export const INTEGRATION_LABELS: Record<string, string> = {
  hpp: 'HPP / Hosted Checkout',
  direct: 'Direct API',
}

export function integrationLabel(value: string): string {
  return INTEGRATION_LABELS[value] ?? value
}

/**
 * Whether a group has enough samples for its percentiles to mean anything.
 * Rendered next to the figure rather than used to hide it: the reader is told the
 * number is thin, not denied it.
 */
export const RELIABLE_SAMPLE_SIZE = 20
