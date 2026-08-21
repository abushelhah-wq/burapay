import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { DocGaps, ErrorNotice, Field, Notice } from '../components/common.jsx'
import { timestamp } from '../lib/format.js'

/**
 * Settings (§7b).
 *
 * Credentials go in and are shown back masked to the last four characters.
 * There is no reveal control, because the API has no endpoint that would serve
 * one — a credential store you can read back is one that leaks through any
 * access-control bug.
 */
export default function Settings() {
  const [gateways, setGateways] = useState([])
  const [selected, setSelected] = useState('')
  const [credentials, setCredentials] = useState(null)
  const [flags, setFlags] = useState(null)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)
  const [testResult, setTestResult] = useState(null)
  const [busy, setBusy] = useState(false)

  const [entry, setEntry] = useState({ key_name: '', value: '', is_production: false })

  useEffect(() => {
    Promise.all([api.gateways(), api.flags()])
      .then(([gatewayList, flagList]) => {
        setGateways(gatewayList)
        setFlags(flagList)
        if (gatewayList.length) setSelected(gatewayList[0].code)
      })
      .catch(setError)
  }, [])

  useEffect(() => {
    if (!selected) return
    setTestResult(null)
    api.credentials(selected).then(setCredentials).catch(setError)
  }, [selected])

  async function save(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      setCredentials(await api.putCredential(selected, entry))
      setEntry({ key_name: '', value: '', is_production: false })
      setMessage(`Saved ${entry.key_name}. Run a connection test to confirm it works.`)
      setGateways(await api.gateways())
    } catch (saveError) {
      setError(saveError)
    } finally {
      setBusy(false)
    }
  }

  async function remove(keyName) {
    setBusy(true)
    try {
      await api.deleteCredential(selected, keyName)
      setCredentials(await api.credentials(selected))
      setGateways(await api.gateways())
      setMessage(`Removed ${keyName}.`)
    } catch (deleteError) {
      setError(deleteError)
    } finally {
      setBusy(false)
    }
  }

  async function test() {
    setBusy(true)
    setTestResult(null)
    setError(null)
    try {
      setTestResult(await api.testConnection(selected))
    } catch (testError) {
      setError(testError)
    } finally {
      setBusy(false)
    }
  }

  const gateway = gateways.find((item) => item.code === selected)
  const suggested = credentials
    ? [...credentials.required_credentials, ...credentials.optional_credentials].filter(
        (key) => !credentials.credentials.some((item) => item.key_name === key),
      )
    : []

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="subtitle">
            Gateway credentials, stored encrypted. Values are never shown back in
            full.
          </p>
        </div>
      </div>

      <ErrorNotice error={error} />
      {message && <Notice tone="good">{message}</Notice>}

      {flags && (
        <div className="card">
          <h3>Runtime configuration</h3>
          <p className="small muted">
            These are the gates the backend enforces. Each one is checked again
            server-side on the request it applies to — the values here are a
            report, not the enforcement.
          </p>
          <div className="table-scroll">
            <table>
              <tbody>
                <tr>
                  <td>Raw card entry</td>
                  <td>
                    <span className={`pill ${flags.allow_direct_card_entry ? 'good' : ''}`}>
                      {flags.allow_direct_card_entry ? 'enabled' : 'disabled'}
                    </span>
                  </td>
                  <td className="small muted">
                    ALLOW_DIRECT_CARD_ENTRY — when off, the Direct API,
                    tokenization and CIT endpoints return 403.
                  </td>
                </tr>
                <tr>
                  <td>Production gateways</td>
                  <td>
                    <span className={`pill ${flags.allow_production_gateways ? 'bad' : 'good'}`}>
                      {flags.allow_production_gateways ? 'allowed' : 'blocked'}
                    </span>
                  </td>
                  <td className="small muted">
                    ALLOW_PRODUCTION_GATEWAYS — blocked at the adapter layer, so
                    calling the API directly does not bypass it.
                  </td>
                </tr>
                <tr>
                  <td>Benchmark limits</td>
                  <td className="mono small">
                    ≤ {flags.benchmark_max_transactions_per_run} per run,{' '}
                    ≥ {flags.benchmark_min_interval_seconds}s apart
                  </td>
                  <td className="small muted">
                    A run over the maximum stops rather than truncating silently.
                  </td>
                </tr>
                <tr>
                  <td>Gateway timeout</td>
                  <td className="mono small">{flags.http_timeout_seconds}s</td>
                  <td className="small muted">
                    HTTP_TIMEOUT_SECONDS — recorded on every logged call.
                  </td>
                </tr>
                <tr>
                  <td>Public base URL</td>
                  <td className="mono small">{flags.public_base_url || '(not set)'}</td>
                  <td className="small muted">
                    {flags.public_base_is_https
                      ? 'Used for every return and callback URL.'
                      : 'Not HTTPS — gateway sandboxes reject non-HTTPS return and callback URLs, so hosted checkout will fail.'}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card">
        <Field label="Gateway">
          <select value={selected} onChange={(event) => setSelected(event.target.value)}>
            {gateways.map((item) => (
              <option key={item.code} value={item.code}>{item.display_name}</option>
            ))}
          </select>
        </Field>

        {credentials && (
          <>
            {credentials.missing.length > 0 && (
              <Notice tone="warn">
                Missing required credentials: {credentials.missing.join(', ')}.
                This gateway cannot be selected for a transaction until they are
                set.
              </Notice>
            )}

            <h3 style={{ marginTop: 16 }}>Stored credentials</h3>
            {credentials.credentials.length === 0 ? (
              <p className="muted small">None stored yet.</p>
            ) : (
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Key</th>
                      <th>Value</th>
                      <th>Environment</th>
                      <th>Updated</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {credentials.credentials.map((credential) => (
                      <tr key={credential.key_name}>
                        <td className="mono">{credential.key_name}</td>
                        <td className="mono">{credential.masked_value}</td>
                        <td>
                          <span className={`pill ${credential.is_production ? 'bad' : 'good'}`}>
                            {credential.is_production ? 'production' : 'sandbox'}
                          </span>
                        </td>
                        <td className="small muted">{timestamp(credential.updated_at)}</td>
                        <td>
                          <button onClick={() => remove(credential.key_name)} disabled={busy}>
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <form onSubmit={save} style={{ marginTop: 18 }}>
              <h3>Add or update a credential</h3>
              <div className="row">
                <Field label="Key name">
                  <input
                    value={entry.key_name}
                    onChange={(event) => setEntry({ ...entry, key_name: event.target.value })}
                    list="credential-keys"
                    required
                  />
                  <datalist id="credential-keys">
                    {suggested.map((key) => (
                      <option key={key} value={key} />
                    ))}
                  </datalist>
                </Field>
                <Field label="Value">
                  <input
                    type="password"
                    value={entry.value}
                    onChange={(event) => setEntry({ ...entry, value: event.target.value })}
                    autoComplete="off"
                    required
                  />
                </Field>
              </div>
              <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  checked={entry.is_production}
                  onChange={(event) =>
                    setEntry({ ...entry, is_production: event.target.checked })
                  }
                />
                <span>
                  This is a production credential
                  {flags && !flags.allow_production_gateways && (
                    <span className="muted">
                      {' '}— it will be refused while ALLOW_PRODUCTION_GATEWAYS is off
                    </span>
                  )}
                </span>
              </label>
              <div className="button-row">
                <button className="primary" type="submit" disabled={busy}>
                  Save credential
                </button>
                <button type="button" onClick={test} disabled={busy || credentials.missing.length > 0}>
                  Test connection
                </button>
              </div>
              <p className="small muted" style={{ marginTop: 8 }}>
                The connection test makes one harmless read-only call. It creates
                nothing in your sandbox.
              </p>
            </form>

            {testResult && (
              <Notice tone={testResult.ok ? 'good' : 'error'}>
                {testResult.message}
                <div className="small muted" style={{ marginTop: 4 }}>
                  Checked: {testResult.checked} · {testResult.duration_ms} ms
                </div>
              </Notice>
            )}
          </>
        )}
      </div>

      {gateway && <DocGaps gaps={gateway.doc_gaps} />}
    </>
  )
}
