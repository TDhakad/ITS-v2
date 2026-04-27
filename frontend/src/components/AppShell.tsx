import type { ReactNode } from "react";
import { Bot, LayoutDashboard, LogIn, LogOut, Plus } from "lucide-react";
import { Button } from "./common";
import { cx } from "../lib";
import type { ApiUser } from "../types";

export type ViewKey = "dashboard" | "assistant";

interface AppShellProps {
  activeView: ViewKey;
  user: ApiUser | null;
  onViewChange: (view: ViewKey) => void;
  onNewTicket: () => void;
  onAuth: () => void;
  onLogout: () => void;
  children: ReactNode;
}

export function AppShell({
  activeView,
  user,
  onViewChange,
  onNewTicket,
  onAuth,
  onLogout,
  children
}: AppShellProps) {
  return (
    <div className="app-shell">
      <header className="topnav">
        <nav className="tabs" aria-label="Primary navigation">
          <button
            className={cx("tab", activeView === "dashboard" && "is-active")}
            onClick={() => onViewChange("dashboard")}
          >
            <LayoutDashboard size={15} aria-hidden="true" />
            Dashboard
          </button>
          <button
            className={cx("tab", activeView === "assistant" && "is-active")}
            onClick={() => onViewChange("assistant")}
          >
            <Bot size={15} aria-hidden="true" />
            AI Assistant
          </button>
        </nav>
        <div className="nav-actions">
          <Button variant="primary" icon={<Plus size={15} aria-hidden="true" />} onClick={onNewTicket}>
            New ticket
          </Button>
          {user ? (
            <>
              <div className="user-chip" title={user.email}>
                <span className="user-chip-text">{user.display_name}</span>
                <span className="user-chip-meta">{user.email}</span>
              </div>
              <Button
                className="compact-button"
                icon={<LogOut size={15} aria-hidden="true" />}
                onClick={onLogout}
              >
                Sign out
              </Button>
            </>
          ) : (
            <Button icon={<LogIn size={15} aria-hidden="true" />} onClick={onAuth}>
              Sign in
            </Button>
          )}
        </div>
      </header>
      <main className="app-main">{children}</main>
    </div>
  );
}
