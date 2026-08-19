/**
 * The application shell: sidebar, environment banner, content area.
 *
 * The TEST MODE banner is not decoration. Section 26 requires it to be obvious at a
 * glance which environment is being hit, because the difference between a sandbox and
 * a production gateway is the difference between a benchmark and a real charge.
 */

import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'

import { api } from '../api/client'
import type { AppSettings } from '../api/types'
import { useAuth } from '../auth/AuthContext'

const NAV = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/run', label: 'Run Benchmark', adminOnly: true },
  { to: '/transactions', label: 'Transactions' },
  { to: '/comparison', label: 'Comparison' },
  { to: '/reports', label: 'Reports' },
  { to: '/gateways', label: 'Gateways' },
  { to: '/settings', label: 'Settings' },
]

export function EnvironmentBanner({ settings }: { settings: AppSettings | null }) {
  if (!settings) return null
  const { test_mode, insecure_defaults } = settings.environment

  return (
    <div>
      <div className={`flex flex-wrap items-center justify-center gap-3 px-4 py-2 text-center
                       text-sm font-semibold tracking-wide
                       ${test_mode ? 'bg-amber-400 text-amber-950'
                                   : 'bg-red-600 text-white'}`}>
        {test_mode ? (
          <>
            <span className="text-base">TEST MODE</span>
            <span className="font-normal">
              Sandbox gateway environments only — no live charges are possible.
            </span>
          </>
        ) : (
          <>
            <span className="text-base">PRODUCTION GATEWAYS ENABLED</span>
            <span className="font-normal">
              Transactions started here can reach live payment gateways.
            </span>
          </>
        )}
      </div>
      {insecure_defaults.length > 0 && (
        <div className="bg-red-700 px-4 py-1.5 text-center text-xs font-medium text-white">
          Development secrets are still in use ({insecure_defaults.join(', ')}). Generate
          real values before this deployment is used for anything that matters.
        </div>
      )}
    </div>
  )
}

export default function Layout() {
  const { user, isAdmin, signOut } = useAuth()
  const navigate = useNavigate()
  const [settings, setSettings] = useState<AppSettings | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    api.settings().then(setSettings).catch(() => setSettings(null))
  }, [])

  async function handleSignOut() {
    await signOut()
    navigate('/login')
  }

  const items = NAV.filter((item) => !item.adminOnly || isAdmin)

  return (
    <div className="flex min-h-full flex-col">
      <EnvironmentBanner settings={settings} />

      <div className="flex flex-1 flex-col lg:flex-row">
        <aside className="border-b border-ink-800 bg-ink-900 lg:w-64 lg:border-b-0
                          lg:border-r">
          <div className="flex items-center justify-between px-5 py-4">
            <div>
              <p className="text-lg font-semibold text-white">BuraPay</p>
              <p className="text-xs text-ink-400">Gateway Benchmark</p>
            </div>
            <button type="button" className="text-ink-300 lg:hidden"
                    onClick={() => setMenuOpen((open) => !open)}
                    aria-label="Toggle navigation">
              ☰
            </button>
          </div>

          <nav className={`${menuOpen ? 'block' : 'hidden'} px-3 pb-4 lg:block`}>
            {items.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end}
                       onClick={() => setMenuOpen(false)}
                       className={({ isActive }) =>
                         `mb-1 block rounded-lg px-3 py-2 text-sm font-medium transition
                          ${isActive ? 'bg-accent-600 text-white'
                                     : 'text-ink-300 hover:bg-ink-800 hover:text-white'}`}>
                {item.label}
              </NavLink>
            ))}

            <div className="mt-6 border-t border-ink-800 px-3 pt-4">
              <p className="text-xs text-ink-400">Signed in as</p>
              <p className="truncate text-sm text-ink-200">{user?.email}</p>
              <p className="mt-0.5 text-xs uppercase tracking-wide text-accent-300">
                {user?.role}
              </p>
              <button type="button" onClick={handleSignOut}
                      className="mt-3 w-full rounded-lg border border-ink-700 px-3 py-1.5
                                 text-xs text-ink-300 hover:bg-ink-800">
                Sign out
              </button>
            </div>
          </nav>
        </aside>

        <main className="flex-1 bg-ink-50 px-4 py-6 sm:px-6 lg:px-8">
          <Outlet />
        </main>
      </div>

      <footer className="border-t border-ink-200 bg-white px-6 py-3 text-xs text-ink-500">
        BuraPay Gateway Benchmark
        {settings && ` v${settings.environment.app_version}`} — latency figures are
        measured server-side with a monotonic clock. Gateway API time excludes customer
        interaction and redirect time.
      </footer>
    </div>
  )
}
