/**
 * The Create User form (specification section 10).
 *
 * The password rules are shown before the form is submitted rather than only in the
 * rejection, and they are checked again on the backend — this component's copy of them
 * is a courtesy, not the enforcement.
 */

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { Card, ErrorNotice } from '../components/ui'
import type { RoleDescription } from '../api/types'

/** Mirrors ``app.core.security.validate_password``. The backend is the authority. */
export const PASSWORD_RULES: { label: string; test: (value: string) => boolean }[] = [
  { label: 'At least 12 characters', test: (v) => v.length >= 12 },
  { label: 'A lower-case letter', test: (v) => /[a-z]/.test(v) },
  { label: 'An upper-case letter', test: (v) => /[A-Z]/.test(v) },
  { label: 'A digit', test: (v) => /[0-9]/.test(v) },
  { label: 'A symbol', test: (v) => /[^A-Za-z0-9]/.test(v) },
]

export function PasswordRules({ value }: { value: string }) {
  return (
    <ul className="mt-2 space-y-0.5 text-xs">
      {PASSWORD_RULES.map((rule) => {
        const met = rule.test(value)
        return (
          <li key={rule.label} className={met ? 'text-emerald-600' : 'text-ink-500'}>
            {met ? '✓' : '·'} {rule.label}
          </li>
        )
      })}
    </ul>
  )
}

export default function CreateUser() {
  const navigate = useNavigate()
  const [roles, setRoles] = useState<RoleDescription[]>([])
  const [form, setForm] = useState({
    full_name: '', username: '', email: '', role: 'USER',
    password: '', confirm_password: '', status: 'ACTIVE',
  })
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.roles().then(setRoles).catch(() => setRoles([]))
  }, [])

  function update(key: keyof typeof form, value: string) {
    setForm((current) => ({ ...current, [key]: value }))
  }

  const mismatch = Boolean(form.confirm_password) && form.password !== form.confirm_password
  const selected = roles.find((role) => role.value === form.role)

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const created = await api.createUser(form)
      navigate(`/users/${created.id}`, { replace: true })
    } catch (exc) {
      setError(exc instanceof ApiError ? exc : new Error('Could not create the user.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-ink-900">Create user</h1>
        <p className="mt-1 text-sm text-ink-500">
          The password is hashed before it is stored and is never shown again — not
          here, not on the user's own page, not to an administrator.
        </p>
      </header>

      {error != null && <ErrorNotice error={error} />}

      <form onSubmit={handleSubmit} className="grid gap-6 lg:grid-cols-3">
        <Card title="Account" className="lg:col-span-2">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="sm:col-span-2">
              <label className="label" htmlFor="full_name">Full name</label>
              <input id="full_name" className="input" value={form.full_name}
                     placeholder="John Smith"
                     onChange={(e) => update('full_name', e.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor="username">Username</label>
              <input id="username" className="input" required value={form.username}
                     placeholder="john.smith" pattern="[a-zA-Z0-9][a-zA-Z0-9._\-]{2,63}"
                     title="3–64 characters: letters, digits, dot, underscore or hyphen."
                     onChange={(e) => update('username', e.target.value)} />
              <p className="mt-1 text-xs text-ink-500">
                Used to sign in, and the handle the audit log is written against. It
                cannot be changed later.
              </p>
            </div>
            <div>
              <label className="label" htmlFor="email">Email</label>
              <input id="email" type="email" className="input" required value={form.email}
                     placeholder="john@example.com"
                     onChange={(e) => update('email', e.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor="role">Role</label>
              <select id="role" className="input" value={form.role}
                      onChange={(e) => update('role', e.target.value)}>
                {(roles.length ? roles.map((r) => r.value) : ['USER', 'ADMIN']).map((value) => (
                  <option key={value} value={value}>{value}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="status">Status</label>
              <select id="status" className="input" value={form.status}
                      onChange={(e) => update('status', e.target.value)}>
                <option value="ACTIVE">Active</option>
                <option value="INACTIVE">Inactive</option>
              </select>
              <p className="mt-1 text-xs text-ink-500">
                An inactive account cannot sign in.
              </p>
            </div>
            <div>
              <label className="label" htmlFor="password">Password</label>
              <input id="password" type="password" className="input" required
                     autoComplete="new-password" value={form.password}
                     onChange={(e) => update('password', e.target.value)} />
              <PasswordRules value={form.password} />
            </div>
            <div>
              <label className="label" htmlFor="confirm_password">Confirm password</label>
              <input id="confirm_password" type="password" className="input" required
                     autoComplete="new-password" value={form.confirm_password}
                     onChange={(e) => update('confirm_password', e.target.value)} />
              {mismatch && (
                <p className="mt-2 text-xs text-red-600">The passwords do not match.</p>
              )}
            </div>
          </div>

          <div className="mt-6 flex gap-3">
            <button type="submit" className="btn-primary" disabled={busy || mismatch}>
              {busy ? 'Creating…' : 'Create user'}
            </button>
            <button type="button" className="btn-secondary" onClick={() => navigate('/users')}>
              Cancel
            </button>
          </div>
        </Card>

        <Card title={`What ${form.role} may do`}>
          {selected ? (
            <ul className="space-y-1 text-sm text-ink-600">
              {selected.permissions.map((permission) => (
                <li key={permission}>· {permission}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-500">
              Role permissions load from the backend, which is also where they are
              enforced.
            </p>
          )}
        </Card>
      </form>
    </div>
  )
}
