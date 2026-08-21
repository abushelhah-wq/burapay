/**
 * Display helpers.
 *
 * Money is never converted here. The backend sends every amount as minor units
 * plus a `display` string it built with the currency's real exponent (§0.4), so
 * the frontend never has to know that SAR has two decimals and KWD has three --
 * and can never get it wrong.
 */

export function money(value) {
  if (!value) return '—'
  return `${value.display} ${value.currency}`
}

export function duration(ms) {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

export function timestamp(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** Milliseconds are the unit of the whole benchmark; never round them away. */
export function ms(value) {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value)} ms`
}

export function percent(value) {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(1)}%`
}

export function decimal(value, places = 2) {
  if (value === null || value === undefined) return '—'
  return Number(value).toFixed(places)
}

export function titleCase(value) {
  if (!value) return ''
  return String(value)
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (character) => character.toUpperCase())
}

export const STATUS_TONE = {
  SUCCESS: 'good',
  FAILED: 'warn',
  ERROR: 'bad',
  TIMEOUT: 'bad',
  PENDING: 'muted',
}
