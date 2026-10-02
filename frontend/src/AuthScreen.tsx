import React, { useState } from "react";
import { api, setToken } from "./api";

type Props = { registrationOpen: boolean; onSignedIn: (user: { id: string; email: string }) => void };

export function AuthScreen({ registrationOpen, onSignedIn }: Props) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true); setError("");
    try {
      const result = await api<{ token: string; user: { id: string; email: string } }>(
        `/auth/${mode}`, { method: "POST", json: { email, password } });
      setToken(result.token);
      onSignedIn(result.user);
    } catch (err: any) {
      setError(err.message || "Could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  return <div className="auth-shell">
    <form className="auth-card" onSubmit={submit}>
      <div className="brand">KNAVIS</div>
      <p className="auth-lead">{mode === "login" ? "Sign in to your documents." : "Create an account to keep your documents private."}</p>
      <label>Email
        <input type="email" autoComplete="email" required value={email} onChange={e => setEmail(e.target.value)} autoFocus />
      </label>
      <label>Password
        <input type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} required minLength={mode === "register" ? 8 : undefined}
          value={password} onChange={e => setPassword(e.target.value)} />
      </label>
      {mode === "register" && <small className="hint">At least 8 characters.</small>}
      {error && <div className="request-error" role="alert">{error}</div>}
      <button className="send" type="submit" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
      {registrationOpen && <button type="button" className="link" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
        {mode === "login" ? "New here? Create an account" : "Have an account? Sign in"}
      </button>}
    </form>
  </div>;
}
