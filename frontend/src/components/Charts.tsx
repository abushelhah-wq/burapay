/**
 * Charts (specification section 15).
 *
 * Rendered with Recharts, bundled with the application — no CDN, no external
 * requests. A chart that silently fails to load because a script host is blocked is
 * worse than no chart.
 *
 * Every chart here plots gateway API time unless its title says otherwise. Mixing
 * API latency and end-to-end duration on one axis would invite exactly the comparison
 * section 39 says not to make.
 */

import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts'

import { ms, percent } from '../lib/format'

/** Deliberately distinguishable at a glance, and safe for the common colour blindness types. */
const SERIES_COLORS = ['#4f46e5', '#0891b2', '#059669', '#d97706', '#db2777', '#7c3aed',
                       '#64748b']

export function colorFor(index: number): string {
  return SERIES_COLORS[index % SERIES_COLORS.length]
}

interface BarDatum {
  label: string
  value: number | null
  sampleSize?: number
  simulated?: boolean
}

export function MetricBarChart({ data, unit = 'ms', height = 260 }: {
  data: BarDatum[]
  unit?: 'ms' | 'percent'
  height?: number
}) {
  const usable = data.filter((item) => item.value !== null) as Required<BarDatum>[]
  if (!usable.length) {
    return <p className="py-10 text-center text-sm text-ink-500">Nothing measured yet.</p>
  }
  const format = unit === 'ms' ? (value: number) => ms(value) : (value: number) => percent(value)

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={usable} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
        <XAxis dataKey="label" tick={{ fontSize: 12, fill: '#475569' }} tickLine={false} />
        <YAxis tick={{ fontSize: 12, fill: '#475569' }} tickLine={false} axisLine={false}
               tickFormatter={(value) => (unit === 'ms' ? `${value}` : `${value}%`)} />
        <Tooltip
          formatter={(value: number, _name, item) => [
            format(value),
            item?.payload?.sampleSize ? `n=${item.payload.sampleSize}` : 'value',
          ]}
          contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }} />
        <Bar dataKey="value" radius={[4, 4, 0, 0]}>
          {usable.map((item, index) => (
            // Simulated gateways are greyed so a MockPay bar is never mistaken for a
            // real gateway's performance.
            <Cell key={item.label} fill={item.simulated ? '#94a3b8' : colorFor(index)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

export interface SeriesPoint {
  bucket: string
  [series: string]: string | number | null
}

export function TimeSeriesChart({ data, series, height = 280 }: {
  data: SeriesPoint[]
  series: string[]
  height?: number
}) {
  if (!data.length) {
    return <p className="py-10 text-center text-sm text-ink-500">No data in this period.</p>
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis dataKey="bucket" tick={{ fontSize: 11, fill: '#475569' }} tickLine={false}
               tickFormatter={(value: string) => new Date(value).toLocaleString(undefined, {
                 month: 'short', day: 'numeric', hour: '2-digit' })} />
        <YAxis tick={{ fontSize: 12, fill: '#475569' }} tickLine={false} axisLine={false} />
        <Tooltip formatter={(value: number) => ms(value)}
                 labelFormatter={(value: string) => new Date(value).toLocaleString()}
                 contentStyle={{ fontSize: 12, borderRadius: 8, border: '1px solid #e2e8f0' }} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {series.map((name, index) => (
          <Line key={name} type="monotone" dataKey={name} stroke={colorFor(index)}
                strokeWidth={2} dot={false} connectNulls />
        ))}
      </LineChart>
    </ResponsiveContainer>
  )
}
