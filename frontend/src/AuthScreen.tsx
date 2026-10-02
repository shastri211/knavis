import React, { useState } from "react";
import { api, setToken } from "./api";

type Props = {
  registrationOpen: boolean;
  passwordReset: boolean;
  onSignedIn: (user: { id: string; email: string }) => void;
};
type Mode = "login" | "register" | "forgot" | "reset";

// A reset link from the e-mail looks like  https://host/#reset=<token>
const tokenFromLink = () => decodeURIComponent((location.hash.match(/^#reset=([\w-]+)/) || [])[1] || "");

export function AuthScreen({ registrationOpen, passwordReset, onSignedIn }: Props) {
  const resetToken = tokenFromLink();
  const [mode, setMode] = useState<Mode>(resetToken ? "reset" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const go = (next: Mode) => { setMode(next); setError(""); setNotice(""); };

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      if (mode === "forgot") {
        const r = await api<{ detail: string }>("/auth/forgot", { method: "POST", json: { email } });
        setNotice(r.detail);
      } else if (mode === "reset") {
        await api("/auth/reset", { method: "POST", json: { token: resetToken, new_password: password } });
        history.replaceState(null, "", location.pathname);   // the link is spent: do not leave it in the address bar
        setPassword(""); setMode("login"); setNotice("Your password was changed. Sign in with the new one.");
      } else {
        const result = await api<{ token: string; user: { id: string; email: string } }>(
          `/auth/${mode}`, { method: "POST", json: { email, password } });
        setToken(result.token);
        onSignedIn(result.user);
      }
    } catch (err: any) {
      setError(err.message || "Something went wrong.");
    } finally {
      setBusy(false);
    }
  }

  const lead = {
    login: "Sign in to your documents.",
    register: "Create an account to keep your documents private.",
    forgot: "Enter your email and we will send a link to choose a new password.",
    reset: "Choose a new password.",
  }[mode];
  const button = { login: "Sign in", register: "Create account", forgot: "Send the link", reset: "Change password" }[mode];

  return <div className="auth-shell">
    <form className="auth-card" onSubmit={submit}>
      <div className="brand">KNAVIS</div>
      <p className="auth-lead">{lead}</p>
      {mode !== "reset" && <label>Email
        <input type="email" autoComplete="email" required value={email} onChange={e => setEmail(e.target.value)} autoFocus />
      </label>}
      {mode !== "forgot" && <label>{mode === "reset" ? "New password" : "Password"}
        <input type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} required
          minLength={mode === "register" || mode === "reset" ? 8 : undefined}
          value={password} onChange={e => setPassword(e.target.value)} autoFocus={mode === "reset"} />
      </label>}
      {(mode === "register" || mode === "reset") && <small className="hint">At least 8 characters.</small>}
      {notice && <div className="notice" role="status">{notice}</div>}
      {error && <div className="request-error" role="alert">{error}</div>}
      <button className="send" type="submit" disabled={busy}>{busy ? "Please wait…" : button}</button>
      {mode === "login" && passwordReset && <button type="button" className="link" onClick={() => go("forgot")}>Forgot your password?</button>}
      {(mode === "login" || mode === "register") && registrationOpen && <button type="button" className="link" onClick={() => go(mode === "login" ? "register" : "login")}>
        {mode === "login" ? "New here? Create an account" : "Have an account? Sign in"}
      </button>}
      {(mode === "forgot" || mode === "reset") && <button type="button" className="link" onClick={() => go("login")}>Back to sign in</button>}
    </form>
  </div>;
}
