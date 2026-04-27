import { useEffect, useState, type FormEvent } from "react";
import { X } from "lucide-react";
import { api } from "../api/client";
import type { ApiUser, LoadState } from "../types";
import { Button, FieldError, IconButton } from "./common";
import { cx } from "../lib";

interface AuthDialogProps {
  open: boolean;
  onClose: () => void;
  onAuthenticated: (user: ApiUser) => void;
}

export function AuthDialog({ open, onClose, onAuthenticated }: AuthDialogProps) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setError(null);
    }
  }, [open]);

  if (!open) {
    return null;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setState("loading");
    setError(null);

    try {
      if (mode === "register") {
        await api.register(email, displayName, password);
      }
      const user = await api.login(email, password);
      onAuthenticated(user);
      onClose();
      setPassword("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Authentication failed.");
      setState("error");
      return;
    }
    setState("ready");
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="auth-title">
        <header className="modal-header">
          <h2 id="auth-title">{mode === "login" ? "Sign in" : "Create account"}</h2>
          <IconButton aria-label="Close sign in" onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </IconButton>
        </header>
        <div className="segmented" role="tablist" aria-label="Authentication mode">
          <button
            className={cx(mode === "login" && "is-active")}
            onClick={() => setMode("login")}
            role="tab"
            aria-selected={mode === "login"}
          >
            Sign in
          </button>
          <button
            className={cx(mode === "register" && "is-active")}
            onClick={() => setMode("register")}
            role="tab"
            aria-selected={mode === "register"}
          >
            Register
          </button>
        </div>
        <form className="stack-form" onSubmit={submit}>
          {mode === "register" ? (
            <label>
              <span>Display name</span>
              <input
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                minLength={1}
                maxLength={120}
                autoComplete="name"
                required
              />
            </label>
          ) : null}
          <label>
            <span>Email</span>
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              minLength={3}
              maxLength={254}
              autoComplete="email"
              required
            />
          </label>
          <label>
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              minLength={mode === "register" ? 8 : 1}
              maxLength={128}
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              required
            />
          </label>
          <FieldError message={error} />
          <Button type="submit" variant="primary" disabled={state === "loading"}>
            {state === "loading" ? "Working" : mode === "login" ? "Sign in" : "Create account"}
          </Button>
        </form>
      </section>
    </div>
  );
}
