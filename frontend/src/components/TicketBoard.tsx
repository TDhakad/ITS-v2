import {
  Activity,
  Box,
  Bug,
  Clock3,
  FolderKanban,
  HardDrive,
  Search,
  TrendingUp
} from "lucide-react";
import type { ReactNode } from "react";
import { EmptyState, LoadingState, PriorityBadge } from "./common";
import type { ApiUser, LoadState, Ticket, TicketCategory } from "../types";
import { cx, formatTime, initials, statusGroup, ticketSearchBlob } from "../lib";

type SideFilter = "all" | "mine" | "Infra" | "Bug" | "UI" | "Hardware" | "Feature";

const columns: Array<{ key: "open" | "pending" | "resolved"; title: string; tone: string }> = [
  { key: "open", title: "Open", tone: "pending" },
  { key: "pending", title: "Pending", tone: "neutral" },
  { key: "resolved", title: "Resolved", tone: "resolved" }
];

interface TicketBoardProps {
  tickets: Ticket[];
  loadState: LoadState;
  error?: string | null;
  search: string;
  filter: SideFilter;
  user: ApiUser | null;
  onSearchChange: (value: string) => void;
  onFilterChange: (value: SideFilter) => void;
  onTicketSelect: (ticketId: number) => void;
  onNewTicket: () => void;
  onRetry: () => void;
}

export function TicketBoard({
  tickets,
  loadState,
  error,
  search,
  filter,
  user,
  onSearchChange,
  onFilterChange,
  onTicketSelect,
  onNewTicket,
  onRetry
}: TicketBoardProps) {
  const filteredTickets = filterTickets(tickets, search, filter, user);
  const counts = countTickets(tickets, user);
  const grouped = columns.reduce<Record<string, Ticket[]>>((acc, column) => {
    acc[column.key] = filteredTickets.filter((ticket) => statusGroup(ticket.status) === column.key);
    return acc;
  }, {});

  return (
    <section className="dashboard-page" aria-label="Ticket dashboard">
      <aside className="dashboard-sidebar">
        <div className="side-nav">
          <p className="side-section">Workspace</p>
          <SideItem
            active={filter === "all"}
            icon={<FolderKanban size={15} aria-hidden="true" />}
            label="All tickets"
            count={tickets.length}
            onClick={() => onFilterChange("all")}
          />
          <SideItem
            active={filter === "mine"}
            icon={<Clock3 size={15} aria-hidden="true" />}
            label="My queue"
            count={counts.mine}
            onClick={() => onFilterChange("mine")}
          />
          <SideItem
            active={false}
            icon={<TrendingUp size={15} aria-hidden="true" />}
            label="Reports"
            onClick={() => onFilterChange("all")}
          />
          <p className="side-section">Categories</p>
          <SideItem
            active={filter === "Infra"}
            icon={<Box size={15} aria-hidden="true" />}
            label="Infrastructure"
            count={counts.byCategory.Infra ?? 0}
            onClick={() => onFilterChange("Infra")}
          />
          <SideItem
            active={filter === "Bug"}
            icon={<Bug size={15} aria-hidden="true" />}
            label="Bugs"
            count={counts.byCategory.Bug ?? 0}
            onClick={() => onFilterChange("Bug")}
          />
          <SideItem
            active={filter === "UI"}
            icon={<Activity size={15} aria-hidden="true" />}
            label="UI"
            count={counts.byCategory.UI ?? 0}
            onClick={() => onFilterChange("UI")}
          />
          <SideItem
            active={filter === "Hardware"}
            icon={<HardDrive size={15} aria-hidden="true" />}
            label="Hardware"
            count={counts.byCategory.Hardware ?? 0}
            onClick={() => onFilterChange("Hardware")}
          />
        </div>
        <div className="side-user">
          <span className="avatar">{initials(user?.display_name, "GU")}</span>
          <span>
            <strong>{user?.display_name ?? "Guest user"}</strong>
            <small>{user?.role ?? "Unauthenticated"}</small>
          </span>
        </div>
      </aside>
      <div className="dashboard-content">
        <header className="dashboard-header">
          <div className="dashboard-title-row">
            <h1>{filterTitle(filter)}</h1>
            <label className="search-box">
              <Search size={15} aria-hidden="true" />
              <span className="sr-only">Search tickets</span>
              <input
                value={search}
                onChange={(event) => onSearchChange(event.target.value)}
                placeholder="Search tickets"
              />
            </label>
          </div>
          <div className="column-label-grid" aria-hidden="true">
            {columns.map((column) => (
              <div key={column.key} className="column-label">
                <span className={cx("column-dot", column.tone)} />
                {column.title}
                <span className="count-pill">{grouped[column.key]?.length ?? 0}</span>
              </div>
            ))}
          </div>
        </header>

        {loadState === "loading" ? (
          <LoadingState label="Loading tickets" />
        ) : loadState === "error" ? (
          <EmptyState title="Ticket data did not load" action={<button onClick={onRetry}>Retry</button>}>
            {error}
          </EmptyState>
        ) : filteredTickets.length === 0 ? (
          <EmptyState title="No tickets found" action={<button onClick={onNewTicket}>Create ticket</button>}>
            Try a different search or create a new request.
          </EmptyState>
        ) : (
          <div className="ticket-columns">
            {columns.map((column) => (
              <section className="ticket-column" key={column.key} aria-label={`${column.title} tickets`}>
                <div className="mobile-column-label">
                  {column.title}
                  <span className="count-pill">{grouped[column.key]?.length ?? 0}</span>
                </div>
                {(grouped[column.key] ?? []).map((ticket) => (
                  <TicketCard key={ticket.id} ticket={ticket} onClick={() => onTicketSelect(ticket.id)} />
                ))}
              </section>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function SideItem({
  active,
  icon,
  label,
  count,
  onClick
}: {
  active: boolean;
  icon: ReactNode;
  label: string;
  count?: number;
  onClick: () => void;
}) {
  return (
    <button className={cx("side-item", active && "is-active")} onClick={onClick}>
      {icon}
      <span>{label}</span>
      {count !== undefined ? <span className="side-count">{count}</span> : null}
    </button>
  );
}

function TicketCard({ ticket, onClick }: { ticket: Ticket; onClick: () => void }) {
  return (
    <button className="ticket-card" onClick={onClick}>
      <span className="ticket-card-title">{ticket.title || ticket.summary}</span>
      <span className="ticket-card-footer">
        <PriorityBadge priority={ticket.priority} />
        <span className="category-pill">{ticket.category}</span>
        <span className="ticket-time">{formatTime(ticket.updated_at)}</span>
      </span>
    </button>
  );
}

function filterTickets(
  tickets: Ticket[],
  search: string,
  filter: SideFilter,
  user: ApiUser | null
): Ticket[] {
  const needle = search.trim().toLowerCase();
  return tickets.filter((ticket) => {
    if (filter === "mine") {
      if (!user || ticket.user_id !== String(user.id)) {
        return false;
      }
    }
    if (filter !== "all" && filter !== "mine" && ticket.category !== filter) {
      return false;
    }
    if (!needle) {
      return true;
    }
    return ticketSearchBlob(ticket).includes(needle);
  });
}

function countTickets(tickets: Ticket[], user: ApiUser | null) {
  return tickets.reduce(
    (acc, ticket) => {
      if (user && ticket.user_id === String(user.id)) {
        acc.mine += 1;
      }
      acc.byCategory[ticket.category] = (acc.byCategory[ticket.category] ?? 0) + 1;
      return acc;
    },
    { mine: 0, byCategory: {} as Partial<Record<TicketCategory, number>> }
  );
}

function filterTitle(filter: SideFilter): string {
  if (filter === "all") {
    return "All tickets";
  }
  if (filter === "mine") {
    return "My queue";
  }
  if (filter === "Infra") {
    return "Infrastructure";
  }
  return filter;
}

export type { SideFilter };
