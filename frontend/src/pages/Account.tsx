/**
 * Your own account (specification section 10: "optionally allow the user to change
 * their own password").
 *
 * The current password is required — an unattended session should not be enough to
 * take an account over — and the existing one is never displayed, because only its
 * hash exists.
 */

import { useState } from 'react'

import { api } from '../api/client'
import { Card, ErrorNotice } from '../components/ui'
import { dateTime } from '../lib/format'
import { useAuth } from '../auth/AuthContext'
import { PasswordRules } from './CreateUser'

export default function Account() {
  const { user } = useAuth()
  const [form, setForm] = useState({
    current_password: '', new_password: '', confirm_password: '',
  })
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const mismatch = Boolean(form.confirm_password)
    && form.new_password !== form.confirm_password

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await api.changePassword(
        form.current_password, form.new_password, form.confirm_password)
      setForm({ current_password: '', new_password: '', confirm_password: '' })
      setNotice(result.message)
    } catch (exc) {
      setError(exc)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">Your account</h1>
      </header>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Signed in as">
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <dt className="text-ink-500">Username</dt>
            <dd className="font-mono">{user?.username}</dd>
            <dt className="text-ink-500">Email</dt>
            <dd>{user?.email}</dd>
            <dt className="text-ink-500">Role</dt>
            <dd>{user?.role}</dd>
            <dt className="text-ink-500">Last login</dt>
            <dd>{user?.last_login_at ? dateTime(user.last_login_at) : '—'}</dd>
          </dl>
        </Card>

        <Card title="Change password">
          {notice && (
            <p className="mb-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3
                          text-sm text-emerald-800">{notice}</p>
          )}
          {error != null && <ErrorNotice error={error} />}
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="label" htmlFor="current_password">Current password</label>
              <input id="current_password" type="password" className="input" required
                     autoComplete="current-password" value={form.current_password}
                     onChange={(e) => setForm({ ...form, current_password: e.target.value })} />
            </div>
            <div>
              <label className="label" htmlFor="new_password">New password</label>
              <input id="new_password" type="password" className="input" required
                     autoComplete="new-password" value={form.new_password}
                     onChange={(e) => setForm({ ...form, new_password: e.target.value })} />
              <PasswordRules value={form.new_password} />
            </div>
            <div>
              <label className="label" htmlFor="confirm_password">Confirm new password</label>
              <input id="confirm_password" type="password" className="input" required
                     autoComplete="new-password" value={form.confirm_password}
                     onChange={(e) => setForm({ ...form, confirm_password: e.target.value })} />
              {mismatch && (
                <p className="mt-2 text-xs text-red-600">The passwords do not match.</p>
              )}
            </div>
            <button type="submit" className="btn-primary" disabled={busy || mismatch}>
              {busy ? 'Changing…' : 'Change password'}
            </button>
          </form>
        </Card>
      </div>
    </div>
  )
}
