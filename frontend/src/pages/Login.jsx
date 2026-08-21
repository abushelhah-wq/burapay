import React, { useState } from 'react'
import { api } from '../lib/api.js'
import { ErrorNotice, Field } from '../components/common.jsx'

export default function Login({ onSignedIn }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(email, password)
      await onSignedIn()
    } catch (loginError) {
      setError(loginError)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <h1 style={{ marginBottom: 4 }}>BuraPay</h1>
        <p className="subtitle" style={{ marginBottom: 18 }}>
          Payment gateway benchmarking. Sign in to continue.
        </p>
        <ErrorNotice error={error} />
        <Field label="Email">
          <input
            type="email"
            value={email}
            autoComplete="username"
            onChange={(event) => setEmail(event.target.value)}
            required
          />
        </Field>
        <Field label="Password">
          <input
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </Field>
        <button className="primary" type="submit" disabled={busy} style={{ width: '100%' }}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
