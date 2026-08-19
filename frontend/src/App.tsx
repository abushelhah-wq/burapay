import { Navigate, Route, Routes } from 'react-router-dom'

import Layout from './components/Layout'
import { Spinner } from './components/ui'
import { useAuth } from './auth/AuthContext'
import Comparison from './pages/Comparison'
import ComparisonTestDetail from './pages/ComparisonTestDetail'
import Dashboard from './pages/Dashboard'
import Gateways from './pages/Gateways'
import Login from './pages/Login'
import Reports from './pages/Reports'
import RunBenchmark from './pages/RunBenchmark'
import RunDetail from './pages/RunDetail'
import Settings from './pages/Settings'
import TransactionDetail from './pages/TransactionDetail'
import Transactions from './pages/Transactions'

function RequireAuth({ children }: { children: JSX.Element }) {
  const { user, loading } = useAuth()
  if (loading) return <Spinner label="Signing in" />
  if (!user) return <Navigate to="/login" replace />
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
        <Route path="runs/:id" element={<RunDetail />} />
        <Route path="comparison" element={<Comparison />} />
        <Route path="comparison-tests/:id" element={<ComparisonTestDetail />} />
        <Route path="reports" element={<Reports />} />
        <Route path="gateways" element={<Gateways />} />
        <Route path="settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
