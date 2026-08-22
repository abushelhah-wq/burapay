/**
 * The Users screen (specification section 10).
 *
 * Administrators only. The sidebar hides the section from everyone else, but that is
 * presentation: every route this page calls is refused by the backend to a non-admin,
 * which is what actually enforces the boundary.
 *
 * Accounts are disabled, never deleted. Audit history and transaction ownership both
 * point at the user row, and removing it would erase the answer to "who ran this".
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { Badge, Card, EmptyState, ErrorNotice, Spinner } from '../components/ui'
import { dateTime } from '../lib/format'
import { useAuth } from '../auth/AuthContext'
import type { User, UserFilters, UserStatus } from '../api/types'

const STATUS_TONES: Record<UserStatus, 'good' | 'neutral' | 'warn'> = {
  ACTIVE: 'good',
  INACTIVE: 'neutral',
  LOCKED: 'warn',
}

const EMPTY_FILTERS: UserFilters = {
  search: '', name: '', username: '', email: '', role: '', status: '',
}

export default function Users() {
  const { user: me } = useAuth()
  const [filters, setFilters] = useState<UserFilters>(EMPTY_FILTERS)
  const [users, setUsers] = useState<User[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const page = await api.users({ ...filters, limit: 200 })
      setUsers(page.items)
    } catch (exc) {
      setError(exc)
      setUsers([])
    }
  }, [filters])

  useEffect(() => {
    // Debounced: the filters are text inputs, and a request per keystroke is a
    // request per keystroke.
    const timer = setTimeout(() => { void load() }, 250)
    return () => clearTimeout(timer)
  }, [load])

  function update(key: keyof UserFilters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }))
  }

  async function toggleStatus(user: User) {
    setBusyId(user.id)
    setNotice(null)
    try {
      const updated = user.status === 'ACTIVE'
        ? await api.disableUser(user.id)
        : await api.enableUser(user.id)
      setUsers((current) =>
        (current ?? []).map((row) => (row.id === updated.id ? updated : row)))
      setNotice(`${updated.username} is now ${updated.status.toLowerCase()}.`)
    } catch (exc) {
      setError(exc)
    } finally {
      setBusyId(null)
    }
  }

  const filtersApplied = useMemo(
    () => Object.values(filters).some((value) => Boolean(value)), [filters])

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">Users</h1>
          <p className="mt-1 text-sm text-ink-500">
            BuraPay accounts, their roles and their status. Disabling an account keeps
            its audit history and the transactions it owns; nothing here deletes a user.
          </p>
        </div>
        <Link to="/users/new" className="btn-primary">Create user</Link>
      </header>

      <Card title="Filter">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <div>
            <label className="label" htmlFor="search">Search</label>
            <input id="search" className="input" placeholder="Name, username or email"
                   value={filters.search ?? ''}
                   onChange={(e) => update('search', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="filter-name">Name</label>
            <input id="filter-name" className="input" value={filters.name ?? ''}
                   onChange={(e) => update('name', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="filter-username">Username</label>
            <input id="filter-username" className="input" value={filters.username ?? ''}
                   onChange={(e) => update('username', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="filter-email">Email</label>
            <input id="filter-email" className="input" value={filters.email ?? ''}
                   onChange={(e) => update('email', e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="filter-role">Role</label>
            <select id="filter-role" className="input" value={filters.role ?? ''}
                    onChange={(e) => update('role', e.target.value)}>
              <option value="">Any</option>
              <option value="ADMIN">Admin</option>
              <option value="USER">User</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="filter-status">Status</label>
            <select id="filter-status" className="input" value={filters.status ?? ''}
                    onChange={(e) => update('status', e.target.value)}>
              <option value="">Any</option>
              <option value="ACTIVE">Active</option>
              <option value="INACTIVE">Inactive</option>
              <option value="LOCKED">Locked</option>
            </select>
          </div>
        </div>
        {filtersApplied && (
          <button type="button" className="btn-secondary mt-3"
                  onClick={() => setFilters(EMPTY_FILTERS)}>
            Clear filters
          </button>
        )}
      </Card>

      {notice && (
        <p className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm
                      text-emerald-800">{notice}</p>
      )}
      {error != null && <ErrorNotice error={error} onRetry={() => void load()} />}

      {users === null ? (
        <Spinner label="Loading users" />
      ) : users.length === 0 ? (
        <EmptyState title="No users match"
                    description={filtersApplied
                      ? 'Try clearing the filters.'
                      : 'Create the first account to get started.'} />
      ) : (
        <Card className="overflow-x-auto">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Username</th>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th>Created</th>
                <th>Last login</th>
                <th>Created by</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td>{user.full_name ?? <span className="text-ink-400">—</span>}</td>
                  <td className="font-mono text-xs">{user.username}</td>
                  <td>{user.email}</td>
                  <td><Badge tone={user.role === 'ADMIN' ? 'info' : 'neutral'}>
                    {user.role}
                  </Badge></td>
                  <td><Badge tone={STATUS_TONES[user.status]}>{user.status}</Badge></td>
                  <td className="whitespace-nowrap text-xs text-ink-500">
                    {dateTime(user.created_at)}
                  </td>
                  <td className="whitespace-nowrap text-xs text-ink-500">
                    {user.last_login_at
                      ? dateTime(user.last_login_at)
                      : <span className="text-ink-400">never</span>}
                  </td>
                  <td className="text-xs text-ink-500">
                    {user.created_by_username ?? <span className="text-ink-400">—</span>}
                  </td>
                  <td className="whitespace-nowrap text-right">
                    <Link to={`/users/${user.id}`} className="btn-secondary mr-2">Edit</Link>
                    <button type="button" className="btn-secondary"
                            disabled={busyId === user.id || user.id === me?.id}
                            title={user.id === me?.id
                              ? 'You cannot disable the account you are signed in with.'
                              : undefined}
                            onClick={() => void toggleStatus(user)}>
                      {user.status === 'ACTIVE' ? 'Disable' : 'Enable'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}
