import React from 'react'
import { STATUS_TONE, titleCase } from '../lib/format.js'

export function StatusPill({ status }) {
  return <span className={`pill ${STATUS_TONE[status] ?? ''}`}>{status}</span>
}

export function Notice({ tone = '', children }) {
  if (!children) return null
  return <div className={`notice ${tone}`}>{children}</div>
}

/**
 * Renders an ApiError.
 *
 * When the backend says an operation is blocked pending documentation it sends
 * the exact page needed; showing the link is more useful than showing the
 * message, because it is the actual next step.
 */
export function ErrorNotice({ error }) {
  if (!error) return null
  return (
    <div className="notice error">
      <div>{error.message}</div>
      {error.detail?.missing && (
        <div className="small" style={{ marginTop: 6 }}>
          Missing: {error.detail.missing}
        </div>
      )}
      {error.docUrl && (
        <div className="small" style={{ marginTop: 6 }}>
          Documentation needed:{' '}
          <a href={error.docUrl} target="_blank" rel="noreferrer">
            {error.docUrl}
          </a>
        </div>
      )}
      {error.detail?.missing_credentials?.length > 0 && (
        <div className="small" style={{ marginTop: 6 }}>
          Missing credentials: {error.detail.missing_credentials.join(', ')}
        </div>
      )}
    </div>
  )
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function Stat({ label, value, hint }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint && <div className="small muted">{hint}</div>}
    </div>
  )
}

export function Field({ label, children, hint }) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <div className="small muted" style={{ marginTop: 4 }}>{hint}</div>}
    </div>
  )
}

export function OperationLabel({ operation }) {
  return <span>{titleCase(operation)}</span>
}

/**
 * The documentation-gap panel.
 *
 * This is a first-class part of the UI, not a footnote: a measurement taken
 * through an endpoint whose shape was never verified is worth less than one
 * that was, and the operator should be able to see which is which before they
 * trust a number.
 */
export function DocGaps({ gaps }) {
  if (!gaps?.length) return null
  const blocking = gaps.filter((gap) => gap.blocks_operation)
  const unverified = gaps.filter((gap) => !gap.blocks_operation)

  return (
    <div className="card">
      <h3>Documentation status</h3>
      {blocking.length > 0 && (
        <>
          <p className="small muted">
            These operations are disabled. Their request shape could not be
            verified against the gateway's documentation, and an unverified
            shape is never sent to a real payment gateway.
          </p>
          <div className="gap-list" style={{ marginBottom: 14 }}>
            {blocking.map((gap) => (
              <div className="gap blocking" key={gap.step_name}>
                <code>{gap.step_name}</code> — {gap.missing}
                <div className="small" style={{ marginTop: 4 }}>
                  <a href={gap.doc_url} target="_blank" rel="noreferrer">
                    {gap.doc_url}
                  </a>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
      {unverified.length > 0 && (
        <>
          <p className="small muted">
            These are implemented from documentation research recorded in this
            repository, but were not re-verified against the live doc pages
            during the last build.
          </p>
          <div className="gap-list">
            {unverified.map((gap) => (
              <div className="gap" key={gap.step_name}>
                <code>{gap.step_name}</code>
                <span className="muted"> — {gap.provenance}</span>
                {gap.missing && <div className="small muted">{gap.missing}</div>}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
