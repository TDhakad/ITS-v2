import type { ButtonHTMLAttributes, ReactNode } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import type { Priority, TicketStatus } from "../types";
import { cx, PRIORITY_LABELS, STATUS_LABELS } from "../lib";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  icon?: ReactNode;
}

export function Button({
  className,
  variant = "secondary",
  icon,
  children,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button type={type} className={cx("button", `button-${variant}`, className)} {...props}>
      {icon}
      {children}
    </button>
  );
}

export function IconButton({
  className,
  children,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button type={type} className={cx("icon-button", className)} {...props}>
      {children}
    </button>
  );
}

export function PriorityDot({ priority }: { priority: Priority }) {
  return <span className={cx("priority-dot", `priority-${priority}`)} aria-hidden="true" />;
}

export function PriorityBadge({ priority }: { priority: Priority }) {
  return (
    <span className="meta-inline">
      <PriorityDot priority={priority} />
      {PRIORITY_LABELS[priority] ?? priority}
    </span>
  );
}

export function StatusBadge({ status }: { status: TicketStatus }) {
  return <span className={cx("status-badge", `status-${status}`)}>{STATUS_LABELS[status]}</span>;
}

export function EmptyState({
  title,
  children,
  action
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <AlertCircle size={18} aria-hidden="true" />
      <h2>{title}</h2>
      {children ? <p>{children}</p> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div className="loading-state" aria-live="polite">
      <Loader2 className="spin" size={18} aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function FieldError({ message }: { message?: string | null }) {
  if (!message) {
    return null;
  }
  return (
    <p className="field-error" role="alert">
      {message}
    </p>
  );
}
