import { Navigate, Route, Routes } from 'react-router-dom'

import Layout from './components/Layout'
import { EmptyState, Spinner } from './components/ui'
import { useAuth } from './auth/AuthContext'
import Account from './pages/Account'
import AuditLog from './pages/AuditLog'
import Comparison from './pages/Comparison'
import ComparisonTestDetail from './pages/ComparisonTestDetail'
import Dashboard from './pages/Dashboard'
import Gateways from './pages/Gateways'
import HppWidget from './pages/HppWidget'
import Login from './pages/Login'
import Logs from './pages/Logs'
import MockCheckout from './pages/MockCheckout'
import Reports from './pages/Reports'
import RunBenchmark from './pages/RunBenchmark'
import RunDetail from './pages/RunDetail'
import Settings from './pages/Settings'
import ThreeDsChallenge from './pages/ThreeDsChallenge'
import TransactionDetail from './pages/TransactionDetail'
import Transactions from './pages/Transactions'
import CreateUser from './pages/CreateUser'
import UserDetail from './pages/UserDetail'
import Users from './pages/Users'

function RequireAuth({ children }: { children: JSX.Element }) {
  const { user, loading } = useAuth()
  if (loading) return <Spinner label="Signing in" />
  if (!user) return <Navigate to="/login" replace />
  return children
}

/**
 * Administrator-only routes (specification section 10).
 *
 * A convenience, not the boundary: every route behind this also refuses a non-admin on
 * the backend, so removing this component would change what the app *shows* and
 * nothing about what it *permits*.
 */
function RequireAdmin({ children }: { children: JSX.Element }) {
  const { isAdmin, loading } = useAuth()
  if (loading) return <Spinner label="Checking permissions" />
  if (!isAdmin) {
    return (
      <EmptyState
        title="Administrators only"
        description="User management needs the Administrator role. Ask an administrator
                     if you need access." />
    )
  }
  return children
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<Dashboard />} />
        <Route path="run" element={<RunBenchmark />} />
        <Route path="transactions" element={<Transactions />} />
        <Route path="transactions/:id" element={<TransactionDetail />} />
        <Route path="hpp/widget/:id" element={<HppWidget />} />
        <Route path="mock-checkout/:sessionId" element={<MockCheckout />} />
        <Route path="three-ds/:id" element={<ThreeDsChallenge />} />
        <Route path="runs/:id" element={<RunDetail />} />
        <Route path="comparison" element={<Comparison />} />
        <Route path="comparison-tests/:id" element={<ComparisonTestDetail />} />
        <Route path="reports" element={<Reports />} />
        <Route path="logs" element={<Logs />} />
        <Route path="gateways" element={<Gateways />} />
        <Route path="account" element={<Account />} />
        <Route path="users" element={<RequireAdmin><Users /></RequireAdmin>} />
        <Route path="users/new" element={<RequireAdmin><CreateUser /></RequireAdmin>} />
        <Route path="users/audit" element={<RequireAdmin><AuditLog /></RequireAdmin>} />
        <Route path="users/:id" element={<RequireAdmin><UserDetail /></RequireAdmin>} />
        <Route path="settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
