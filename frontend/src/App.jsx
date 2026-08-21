import React, { useCallback, useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './lib/api.js'
import Login from './pages/Login.jsx'
import NewTransaction from './pages/NewTransaction.jsx'
import Transactions from './pages/Transactions.jsx'
import TransactionDetail from './pages/TransactionDetail.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Settings from './pages/Settings.jsx'
import Webhooks from './pages/Webhooks.jsx'

export default function App() {
  const [session, setSession] = useState(null)
  const [flags, setFlags] = useState(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const state = await api.session()
      setSession(state)
      if (state.authenticated) {
        // Flags drive which controls the UI offers. They are a mirror of the
        // server-side gates, never a substitute -- every one of them is
        // enforced again on the request that matters.
        setFlags(await api.flags().catch(() => null))
      }
    } catch {
      setSession({ authenticated: false })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  if (loading) {
    return <div className="empty">Loading…</div>
  }

  if (!session?.authenticated) {
    return <Login onSignedIn={refresh} />
  }

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          BuraPay
          <small>gateway benchmark</small>
        </div>
        <nav className="nav">
          <NavLink to="/dashboard">Benchmark</NavLink>
          <NavLink to="/new">New transaction</NavLink>
          <NavLink to="/transactions">Transactions</NavLink>
          <NavLink to="/webhooks">Callbacks</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <div className="sidebar-foot">
          <div>{session.email}</div>
          {flags?.measurement_location && (
            <div style={{ marginTop: 4 }}>Measured from {flags.measurement_location}</div>
          )}
          <button
            style={{ marginTop: 10, width: '100%' }}
            onClick={async () => {
              await api.logout()
              refresh()
            }}
          >
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/new" element={<NewTransaction flags={flags} />} />
          <Route path="/transactions" element={<Transactions />} />
          <Route path="/transactions/:id" element={<TransactionDetail />} />
          <Route path="/webhooks" element={<Webhooks />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  )
}
