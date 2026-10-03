import React, { useEffect, useState } from "react";
import { api, ApiError, setToken } from "./api";

type Props = {
  registrationOpen: boolean;
  passwordReset: boolean;
  emailVerification: boolean;
  onSignedIn: (user: { id: string; email: string }) => void;
};
type Mode = "login" | "register" | "forgot" | "reset";
type Session = { token: string; user: { id: string; email: string } };

// Links from e-mails look like  https://host/#reset=<token>  and  https://host/#verify=<token>
const tokenFromLink = (kind: string) => decodeURIComponent((location.hash.match(new RegExp(`^#${kind}=([\\w-]+)`)) || [])[1] || "");

export function AuthScreen({ registrationOpen, passwordReset, emailVerification, onSignedIn }: Props) {
  const resetToken = tokenFromLink("reset");
  const verifyToken = tokenFromLink("verify");
  const [mode, setMode] = useState<Mode>(resetToken ? "reset" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(verifyToken ? "Confirming your e-mail address…" : "");
  const [busy, setBusy] = useState(false);
  const [canResend, setCanResend] = useState(false);

  const go = (next: Mode) => { setMode(next); setError(""); setNotice(""); setCanResend(false); };

  // A link opened in a tab where the app is already showing changes only the address fragment, not the page: start over so it is used.
  useEffect(() => {
    const onHash = () => { if (/^#(verify|reset)=/.test(location.hash)) location.reload(); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Opening the confirmation link finishes sign-up and signs the person in.
  useEffect(() => {
    if (!verifyToken) return;
    (async () => {
      try {
        const result = await api<Session>("/auth/verify", { method: "POST", json: { token: verifyToken } });
        history.replaceState(null, "", location.pathname);
        setToken(result.token);
        onSignedIn(result.user);
      } catch (err: any) {
        history.replaceState(null, "", location.pathname);
        setNotice(""); setError(err.message || "This confirmation link could not be used.");
        setCanResend(emailVerification);
      }
    })();
  }, []);

  async function resend() {
    if (!email) { setError("Enter your email above, then ask for the link again."); return; }
    try {
      const r = await api<{ detail: string }>("/auth/resend", { method: "POST", json: { email } });
      setError(""); setNotice(r.detail);
    } catch (err: any) { setError(err.message); }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true); setError(""); setNotice(""); setCanResend(false);
    try {
      if (mode === "forgot") {
        const r = await api<{ detail: string }>("/auth/forgot", { method: "POST", json: { email } });
        setNotice(r.detail);
      } else if (mode === "reset") {
        await api("/auth/reset", { method: "POST", json: { token: resetToken, new_password: password } });
        history.replaceState(null, "", location.pathname);   // the link is spent: do not leave it in the address bar
        setPassword(""); setMode("login"); setNotice("Your password was changed. Sign in with the new one.");
      } else {
        const result = await api<Session & { verification_required?: boolean }>(
          `/auth/${mode}`, { method: "POST", json: { email, password } });
        if (result.verification_required) {   // the account exists but must be confirmed from the e-mail first
          setPassword(""); setMode("login");
          setNotice(`We sent a confirmation link to ${email}. Open it to finish creating your account.`);
        } else {
          setToken(result.token);
          onSignedIn(result.user);
        }
      }
    } catch (err: any) {
      setError(err.message || "Something went wrong.");
      if (mode === "login" && err instanceof ApiError && err.status === 403 && emailVerification) setCanResend(true);
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
      {canResend && <button type="button" className="link" onClick={resend}>Send the confirmation link again</button>}
      {mode === "login" && passwordReset && <button type="button" className="link" onClick={() => go("forgot")}>Forgot your password?</button>}
      {(mode === "login" || mode === "register") && registrationOpen && <button type="button" className="link" onClick={() => go(mode === "login" ? "register" : "login")}>
        {mode === "login" ? "New here? Create an account" : "Have an account? Sign in"}
      </button>}
      {(mode === "forgot" || mode === "reset") && <button type="button" className="link" onClick={() => go("login")}>Back to sign in</button>}
    </form>
  </div>;
}
