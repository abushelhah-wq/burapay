/**
 * Edit one user, reset their password, and read what has happened to the account
 * (specification section 10).
 *
 * The username is shown but not editable: it is the handle every audit row is written
 * against, and reassigning it would silently re-attribute history. The existing
 * password is never displayed, because it cannot be — only a hash is stored.
 */

import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { Badge, Card, ErrorNotice, Spinner } from '../components/ui'
import { dateTime } from '../lib/format'
import { useAuth } from '../auth/AuthContext'
import { PasswordRules } from './CreateUser'
import type { AuditLogEntry, User } from '../api/types'

export function AuditTable({ rows }: { rows: AuditLogEntry[] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-ink-500">Nothing recorded yet.</p>
  }
  return (
    <div className="overflow-x-auto">
      <table className="table">
        <thead>
          <tr>
            <th>When</th>
            <th>Event</th>
            <th>Subject</th>
            <th>Performed by</th>
            <th>Address</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td className="whitespace-nowrap text-xs text-ink-500">
                {dateTime(row.created_at)}
              </td>
              <td><Badge tone={row.event === 'LOGIN_FAILED' ? 'warn' : 'neutral'}>
                {row.event}
              </Badge></td>
              <td className="font-mono text-xs">
                {row.subject_label ?? <span className="text-ink-400">—</span>}
              </td>
              <td className="font-mono text-xs">
                {row.performed_by_label ?? <span className="text-ink-400">—</span>}
              </td>
              <td className="font-mono text-xs text-ink-500">
                {row.ip_address ?? <span className="text-ink-400">—</span>}
              </td>
              <td className="max-w-md truncate text-xs text-ink-500"
                  title={JSON.stringify(row.detail)}>
                {Object.keys(row.detail ?? {}).length
                  ? JSON.stringify(row.detail)
                  : <span className="text-ink-400">—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function UserDetail() {
  const { id = '' } = useParams()
  const { user: me, refresh } = useAuth()
  const [user, setUser] = useState<User | null>(null)
  const [audit, setAudit] = useState<AuditLogEntry[]>([])
  const [form, setForm] = useState({ full_name: '', email: '', role: 'USER', status: 'ACTIVE' })
  const [passwords, setPasswords] = useState({ new_password: '', confirm_password: '' })
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      const [loaded, history] = await Promise.all([api.user(id), api.userAudit(id)])
      setUser(loaded)
      setAudit(history.items)
      setForm({
        full_name: loaded.full_name ?? '', email: loaded.email,
        role: loaded.role, status: loaded.status,
      })
    } catch (exc) {
      setError(exc)
    }
  }, [id])

  useEffect(() => { void load() }, [load])

  async function saveDetails(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const updated = await api.updateUser(id, form)
      setUser(updated)
      setNotice('Saved. The change is recorded in the audit log.')
      // Editing your own account changes what the sidebar should show.
      if (updated.id === me?.id) await refresh()
      const history = await api.userAudit(id)
      setAudit(history.items)
    } catch (exc) {
      setError(exc)
    } finally {
      setBusy(false)
    }
  }

  async function resetPassword(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await api.resetUserPassword(
        id, passwords.new_password, passwords.confirm_password)
      setPasswords({ new_password: '', confirm_password: '' })
      setNotice(result.message)
      const [loaded, history] = await Promise.all([api.user(id), api.userAudit(id)])
      setUser(loaded)
      setAudit(history.items)
    } catch (exc) {
      setError(exc)
    } finally {
      setBusy(false)
    }
  }

  if (!user) return error != null ? <ErrorNotice error={error} /> : <Spinner label="Loading user" />

  const isSelf = user.id === me?.id
  const mismatch = Boolean(passwords.confirm_password)
    && passwords.new_password !== passwords.confirm_password

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">
            {user.full_name || user.username}
          </h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-ink-500">
            <span className="font-mono">{user.username}</span>
            <Badge tone={user.role === 'ADMIN' ? 'info' : 'neutral'}>{user.role}</Badge>
            <Badge tone={user.status === 'ACTIVE' ? 'good'
                       : user.status === 'LOCKED' ? 'warn' : 'neutral'}>
              {user.status}
            </Badge>
          </p>
        </div>
        <Link to="/users" className="btn-secondary">Back to users</Link>
      </header>

      {notice && (
        <p className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm
                      text-emerald-800">{notice}</p>
      )}
      {error != null && <ErrorNotice error={error} />}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Details">
          <form onSubmit={saveDetails} className="space-y-4">
            <div>
              <label className="label" htmlFor="username">Username</label>
              <input id="username" className="input bg-ink-50" value={user.username}
                     readOnly disabled />
              <p className="mt-1 text-xs text-ink-500">
                Fixed: the audit trail is written against it.
              </p>
            </div>
            <div>
              <label className="label" htmlFor="full_name">Full name</label>
              <input id="full_name" className="input" value={form.full_name}
                     onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
            </div>
            <div>
              <label className="label" htmlFor="email">Email</label>
              <input id="email" type="email" className="input" value={form.email}
                     onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </div>
            <div>
              <label className="label" htmlFor="role">Role</label>
              <select id="role" className="input" value={form.role} disabled={isSelf}
                      onChange={(e) => setForm({ ...form, role: e.target.value })}>
                <option value="USER">USER</option>
                <option value="ADMIN">ADMIN</option>
              </select>
            </div>
            <div>
              <label className="label" htmlFor="status">Status</label>
              <select id="status" className="input" value={form.status} disabled={isSelf}
                      onChange={(e) => setForm({ ...form, status: e.target.value })}>
                <option value="ACTIVE">ACTIVE</option>
                <option value="INACTIVE">INACTIVE</option>
                <option value="LOCKED">LOCKED</option>
              </select>
              {user.status === 'LOCKED' && (
                <p className="mt-1 text-xs text-amber-600">
                  Locked by repeated failed sign-ins. Setting it back to ACTIVE, or
                  resetting the password, clears the lockout immediately.
                </p>
              )}
            </div>
            {isSelf && (
              <p className="text-xs text-ink-500">
                Role and status are fixed on your own account, so an administrator
                cannot lock themselves out. Another administrator can change them.
              </p>
            )}
            <button type="submit" className="btn-primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save changes'}
            </button>
          </form>
        </Card>

        <div className="space-y-6">
          <Card title="Activity">
            <dl className="grid grid-cols-2 gap-3 text-sm">
              <dt className="text-ink-500">Created</dt>
              <dd>{dateTime(user.created_at)}</dd>
              <dt className="text-ink-500">Created by</dt>
              <dd>{user.created_by_username ?? '—'}</dd>
              <dt className="text-ink-500">Last login</dt>
              <dd>{user.last_login_at ? dateTime(user.last_login_at) : 'never'}</dd>
            </dl>
          </Card>

          <Card title="Reset password">
            <p className="mb-3 text-sm text-ink-500">
              The current password cannot be shown — only a hash is stored. Setting a
              new one is recorded as USER_PASSWORD_RESET.
            </p>
            <form onSubmit={resetPassword} className="space-y-4">
              <div>
                <label className="label" htmlFor="new_password">New password</label>
                <input id="new_password" type="password" className="input" required
                       autoComplete="new-password" value={passwords.new_password}
                       onChange={(e) => setPasswords({ ...passwords, new_password: e.target.value })} />
                <PasswordRules value={passwords.new_password} />
              </div>
              <div>
                <label className="label" htmlFor="confirm">Confirm password</label>
                <input id="confirm" type="password" className="input" required
                       autoComplete="new-password" value={passwords.confirm_password}
                       onChange={(e) => setPasswords({ ...passwords, confirm_password: e.target.value })} />
                {mismatch && (
                  <p className="mt-2 text-xs text-red-600">The passwords do not match.</p>
                )}
              </div>
              <button type="submit" className="btn-secondary" disabled={busy || mismatch}>
                Reset password
              </button>
            </form>
          </Card>
        </div>
      </div>

      <Card title="Audit history"
            action={<Link to="/users/audit" className="btn-secondary">Full audit log</Link>}>
        <AuditTable rows={audit} />
      </Card>
    </div>
  )
}
