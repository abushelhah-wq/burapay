import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Metric, SampleSize, StatusBadge } from './ui'
import type { StatSummary } from '../api/types'

function stats(overrides: Partial<StatSummary> = {}): StatSummary {
  return {
    count: 0, min: null, max: null, mean: null, median: null, p50: null, p90: null,
    p95: null, p99: null, stdev: null, reliable: false, ...overrides,
  }
}

describe('SampleSize', () => {
  it('flags a sample too small for percentiles to be reliable', () => {
    render(<SampleSize stats={stats({ count: 6 })} />)
    const marker = screen.getByText(/n=6/)
    expect(marker).toBeInTheDocument()
    expect(marker.className).toContain('amber')
  })

  it('does not flag a sample large enough to trust', () => {
    render(<SampleSize stats={stats({ count: 120, reliable: true })} />)
    expect(screen.getByText(/n=120/).className).not.toContain('amber')
  })

  it('says so plainly when there are no samples at all', () => {
    render(<SampleSize stats={stats()} />)
    expect(screen.getByText('no samples')).toBeInTheDocument()
  })
})

describe('Metric', () => {
  it('renders an unmeasurable value as a dash with an explanation', () => {
    render(<Metric value={null} />)
    const element = screen.getByTitle(/not measurable/i)
    expect(element).toHaveTextContent('—')
  })
})

describe('StatusBadge', () => {
  it('renders the status without underscores', () => {
    render(<StatusBadge status="IN_PROGRESS" />)
    expect(screen.getByText('IN PROGRESS')).toBeInTheDocument()
  })
})
