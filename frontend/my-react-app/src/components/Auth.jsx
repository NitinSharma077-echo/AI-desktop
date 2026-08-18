import { useState } from 'react'
import * as api from '../api'

/**
 * Sign in, or create an account.
 *
 * Registering signs you straight in afterwards. Making someone type the same
 * credentials twice in a row serves nothing -- they have just proven they know
 * them, and /auth/register deliberately returns a user rather than a token, so
 * the second call is ours to make, not theirs.
 */
export default function Auth({ onSignedIn }) {
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const registering = mode === 'register'

  function switchTo(next) {
    setMode(next)
    // An error from the other tab is about the other action; leaving it up
    // makes the new form look broken before it has been used.
    setError('')
  }

  async function submit(event) {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (registering) await api.register(username, password)
      await api.login(username, password)
      onSignedIn(await api.me())
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="auth">
      <section className="auth-card">
        <div className="brand">
          <span className="mark">✦</span>
          <span>
            <span className="brand-name">AI Desktop</span>
            <span className="brand-sub">chat · documents · crm</span>
          </span>
        </div>

        <div className="tabs" role="tablist">
          {[
            ['login', 'Sign in'],
            ['register', 'Create account'],
          ].map(([value, text]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={mode === value}
              className={`tab${mode === value ? ' is-active' : ''}`}
              onClick={() => switchTo(value)}
            >
              {text}
            </button>
          ))}
        </div>

        <form onSubmit={submit} noValidate>
          <label className="field">
            <span>Username</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              spellCheck="false"
              autoFocus
              required
              minLength={3}
              placeholder="nitin"
            />
          </label>

          <label className="field">
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              // Tells a password manager to offer a new password rather than
              // autofilling the existing one into a registration form.
              autoComplete={registering ? 'new-password' : 'current-password'}
              required
              minLength={8}
              placeholder={registering ? 'at least 8 characters' : ''}
            />
          </label>

          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}

          <button
            className="primary"
            type="submit"
            disabled={busy || username.length < 3 || password.length < 8}
          >
            {busy ? 'Working…' : registering ? 'Create account' : 'Sign in'}
          </button>
        </form>

        <p className="foot">
          Prefer the raw API? <a href="/docs">Swagger UI</a>
        </p>
      </section>
    </main>
  )
}
