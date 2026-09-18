import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import type { CurrentUser } from "../types/api";

/**
 * Username + access token sign-in. The token lives only in this component's state
 * for the duration of the request and is cleared right after; it is never written
 * to localStorage/sessionStorage/IndexedDB. The durable credential is the
 * server's HttpOnly session cookie.
 */
export function Login({ onSignedIn }: { onSignedIn: (user: CurrentUser) => void }) {
  const [username, setUsername] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!username.trim() || !token) return;
    setBusy(true);
    setError(null);
    const secret = token;
    setToken("");
    try {
      onSignedIn(await api.login(username.trim(), secret));
    } catch {
      setError("Invalid credentials");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit} autoComplete="off">
        <span className="eyebrow">Internal engineering tool</span>
        <h1>AI Platform</h1>
        <label htmlFor="login-username">Username</label>
        <input
          id="login-username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoComplete="username"
          autoFocus
        />
        <label htmlFor="login-token">Access token</label>
        <input
          id="login-token"
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          autoComplete="off"
        />
        {error && <div className="composer-error">{error}</div>}
        <button className="control primary" type="submit" disabled={busy || !username.trim() || !token}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted login-hint">
          Tokens are issued by an administrator with <code>ai-platform auth-token create</code>.
        </p>
      </form>
    </div>
  );
}
