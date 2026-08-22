/**
 * The audit log (specification section 10).
 *
 * Read-only: there is no route that edits or removes a row, because a trail an
 * operator can rewrite answers no question. Failed sign-ins are here too, including
 * attempts against handles that match no account — that pattern is the point.
 */

import { useCallback, useEffect, useState } from 'react'

import { api } from '../api/client'
import { Card, ErrorNotice, Spinner } from '../components/ui'
import { AuditTable } from './UserDetail'
import type { AuditLogEntry } from '../api/types'

export default function AuditLog() {
  const [events, setEvents] = useState<string[]>([])
  const [event, setEvent] = useState('')
  const [rows, setRows] = useState<AuditLogEntry[] | null>(null)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    api.auditEvents().then(setEvents).catch(() => setEvents([]))
  }, [])

  const load = useCallback(async () => {
    setError(null)
    try {
      const page = await api.auditLogs({ event: event || undefined, limit: 200 })
      setRows(page.items)
      setTotal(page.total)
    } catch (exc) {
      setError(exc)
      setRows([])
    }
  }, [event])

  useEffect(() => { void load() }, [load])

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">Audit log</h1>
        <p className="mt-1 text-sm text-ink-500">
          Authentication and user-management events. Passwords, hashes and tokens are
          never recorded here — only who did what, to whom, and from where.
        </p>
      </header>

      <Card title="Filter"
            action={<span className="text-xs text-ink-500">{total} events</span>}>
        <label className="label" htmlFor="event">Event</label>
        <select id="event" className="input max-w-xs" value={event}
                onChange={(e) => setEvent(e.target.value)}>
          <option value="">All events</option>
          {events.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      </Card>

      {error != null && <ErrorNotice error={error} onRetry={() => void load()} />}

      {rows === null ? <Spinner label="Loading audit log" /> : (
        <Card><AuditTable rows={rows} /></Card>
      )}
    </div>
  )
}
