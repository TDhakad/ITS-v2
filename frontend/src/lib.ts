import type { Priority, Ticket, TicketStatus } from "./types";

export const STATUS_LABELS: Record<TicketStatus, string> = {
  open: "Open",
  triage: "Triaged",
  in_progress: "In Progress",
  resolved: "Resolved",
  closed: "Closed"
};

export const PRIORITY_LABELS: Record<Priority, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  critical: "Critical"
};

export function cx(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

export function initials(value: string | undefined | null, fallback = "IT"): string {
  if (!value) {
    return fallback;
  }
  const parts = value
    .replace(/@.*/, "")
    .split(/[\s._-]+/)
    .filter(Boolean);
  const letters = parts.slice(0, 2).map((part) => part[0]?.toUpperCase()).join("");
  return letters || fallback;
}

export function formatTime(value: string | undefined): string {
  if (!value) {
    return "Unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Unknown";
  }

  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startOfDate = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const dayDelta = Math.round((startOfToday - startOfDate) / 86_400_000);

  if (dayDelta === 0) {
    return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
  }
  if (dayDelta === 1) {
    return "Yesterday";
  }
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
}

export function formatDateTime(value: string | undefined): string {
  if (!value) {
    return "Unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  }).format(date);
}

export function statusGroup(status: TicketStatus): "open" | "pending" | "resolved" {
  if (status === "resolved" || status === "closed") {
    return "resolved";
  }
  if (status === "triage" || status === "in_progress") {
    return "pending";
  }
  return "open";
}

export function ticketDescription(ticket: Ticket): string {
  const rawDescription = ticket.raw_context?.description;
  if (typeof rawDescription === "string" && rawDescription.trim()) {
    return rawDescription.trim();
  }
  return ticket.description || ticket.summary || ticket.title;
}

export function ticketSearchBlob(ticket: Ticket): string {
  return [
    ticket.id,
    ticket.title,
    ticket.summary,
    ticket.description,
    ticket.requester,
    ticket.category,
    ticket.priority,
    ticket.environment,
    ticket.app_name,
    ...ticket.keywords
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function generateId(prefix: string): string {
  if (window.crypto?.randomUUID) {
    return `${prefix}-${window.crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
