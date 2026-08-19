/**
 * Shared presentation pieces.
 *
 * Two conventions run through all of them, both from the specification:
 *  * a metric that could not be measured renders as an em dash, never as zero;
 *  * a statistic is never shown without its sample size close by.
 */

import type { ReactNode } from 'react'

import { NOT_MEASURED, count, ms, percent } from '../lib/format'
import type { StatSummary } from '../api/types'

export function Card({ title, action, children, className = '' }: {
  title?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="card-header">
          <h2 className="card-title">{title}</h2>
          {action}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  )
}

export function StatCard({ label, value, sub, tone = 'default' }: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: 'default' | 'good' | 'warn' | 'bad'
}) {
  const tones = {
    default: 'text-ink-900',
    good: 'text-emerald-600',
    warn: 'text-amber-600',
    bad: 'text-red-600',
  }
  return (
    <div className="card p-5">
      <p className="text-xs font-semibold uppercase tracking-wide text-ink-500">{label}</p>
      <p className={`mt-2 text-3xl font-semibold tabular ${tones[tone]}`}>{value}</p>
      {sub && <p className="mt-1 text-xs text-ink-500">{sub}</p>}
    </div>
  )
}

const STATUS_STYLES: Record<string, string> = {
  SUCCESS: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  DECLINED: 'bg-amber-50 text-amber-700 ring-amber-600/20',
  PENDING: 'bg-sky-50 text-sky-700 ring-sky-600/20',
  IN_PROGRESS: 'bg-sky-50 text-sky-700 ring-sky-600/20',
  FAILED: 'bg-red-50 text-red-700 ring-red-600/20',
  ERROR: 'bg-red-50 text-red-700 ring-red-600/20',
  TIMEOUT: 'bg-orange-50 text-orange-700 ring-orange-600/20',
  CANCELLED: 'bg-ink-100 text-ink-600 ring-ink-500/20',
  COMPLETED: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  RUNNING: 'bg-sky-50 text-sky-700 ring-sky-600/20',
  QUEUED: 'bg-ink-100 text-ink-600 ring-ink-500/20',
}

export function StatusBadge({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? 'bg-ink-100 text-ink-700 ring-ink-500/20'
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-1 text-xs font-medium
                      ring-1 ring-inset ${style}`}>
      {status.replace(/_/g, ' ')}
    </span>
  )
}

export function Badge({ children, tone = 'neutral' }: {
  children: ReactNode
  tone?: 'neutral' | 'info' | 'warn' | 'good'
}) {
  const tones = {
    neutral: 'bg-ink-100 text-ink-700 ring-ink-500/20',
    info: 'bg-accent-50 text-accent-700 ring-accent-500/20',
    warn: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    good: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
  }
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium
                      ring-1 ring-inset ${tones[tone]}`}>
      {children}
    </span>
  )
}

/**
 * A sample-size marker.
 *
 * Shown wherever percentiles are: section 60 requires the sample size to travel with
 * any performance claim, and a thin sample is called out rather than quietly used.
 */
export function SampleSize({ stats }: { stats: StatSummary }) {
  if (!stats.count) return <span className="text-xs text-ink-400">no samples</span>
  return (
    <span className={`text-xs ${stats.reliable ? 'text-ink-500' : 'text-amber-600'}`}
          title={stats.reliable
            ? `${stats.count} samples`
            : `${stats.count} samples — too few for percentiles to be reliable`}>
      n={count(stats.count)}{!stats.reliable && ' ⚠'}
    </span>
  )
}

export function Metric({ value, format = 'ms' }: {
  value: number | null | undefined
  format?: 'ms' | 'percent' | 'count'
}) {
  const rendered = format === 'ms' ? ms(value)
    : format === 'percent' ? percent(value)
    : count(value)
  const missing = rendered === NOT_MEASURED
  return (
    <span className={`tabular font-mono ${missing ? 'text-ink-300' : ''}`}
          title={missing ? 'Not measurable for this gateway or flow' : undefined}>
      {rendered}
    </span>
  )
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-3 py-10 text-sm text-ink-500">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-ink-300
                       border-t-accent-600" />
      {label}…
    </div>
  )
}

export function EmptyState({ title, description, action }: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed
                    border-ink-300 py-12 text-center">
      <p className="text-sm font-medium text-ink-700">{title}</p>
      {description && <p className="max-w-md text-sm text-ink-500">{description}</p>}
      {action}
    </div>
  )
}

export function ErrorNotice({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error)
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <p className="font-medium">Something went wrong</p>
      <p className="mt-1">{message}</p>
      {onRetry && (
        <button type="button" className="btn-secondary mt-3" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}

export function Note({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-ink-200 bg-ink-50 p-3 text-xs leading-relaxed
                  text-ink-600">
      {children}
    </p>
  )
}

/** A horizontal bar for the per-call timeline (section 12's graphical timeline). */
export function DurationBar({ value, max, label }: {
  value: number
  max: number
  label?: string
}) {
  const width = max > 0 ? Math.max(2, (value / max) * 100) : 0
  return (
    <div className="flex items-center gap-3">
      <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-ink-100">
        <div className="h-full rounded-full bg-accent-500" style={{ width: `${width}%` }} />
      </div>
      <span className="w-24 shrink-0 tabular text-right font-mono text-xs text-ink-600">
        {label ?? ms(value)}
      </span>
    </div>
  )
}
