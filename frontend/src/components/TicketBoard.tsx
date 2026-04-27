import {
  Activity,
  Bot,
  Bug,
  Clock3,
  FolderKanban,
  HardDrive,
  LayoutDashboard,
  List,
  Search,
  Server,
  TrendingUp,
} from "lucide-react";
import type { ReactNode } from "react";
import { EmptyState, LoadingState } from "./common";
import type { ApiUser, LoadState, ProjectSummary, Ticket, TicketCategory } from "../types";
import { cx, formatTime, statusGroup, ticketSearchBlob } from "../lib";

type SideFilter = "all" | "mine" | "Infra" | "Bug" | "UI" | "Hardware" | "Feature";

const columns: Array<{ key: "open" | "pending" | "resolved"; title: string; tone: string }> = [
  { key: "open", title: "Open", tone: "open" },
  { key: "pending", title: "Pending", tone: "pending" },
  { key: "resolved", title: "Resolved", tone: "resolved" },
];

interface TicketBoardProps {
  tickets: Ticket[];
  projects: ProjectSummary[];
  activeProjectId: number | null;
  loadState: LoadState;
  error?: string | null;
  search: string;
  filter: SideFilter;
  user: ApiUser | null;
  onProjectChange: (projectId: number | null) => void;
  onSearchChange: (value: string) => void;
  onFilterChange: (value: SideFilter) => void;
  onTicketSelect: (ticketId: number) => void;
  onRetry: () => void;
  onViewChange: (view: "dashboard" | "assistant") => void;
}

export function TicketBoard({
  tickets,
  projects,
  activeProjectId,
  loadState,
  error,
  search,
  filter,
  user,
  onProjectChange,
  onSearchChange,
  onFilterChange,
  onTicketSelect,
  onRetry,
  onViewChange,
}: TicketBoardProps) {
  const filteredTickets = filterTickets(tickets, search, filter, user);
  const counts = countTickets(tickets, user);
  const grouped = columns.reduce<Record<string, Ticket[]>>((acc, column) => {
    acc[column.key] = filteredTickets.filter((ticket) => statusGroup(ticket.status) === column.key);
    return acc;
  }, {});

  return (
    <section className="ops-dashboard-page" aria-label="Ticket dashboard">
      <aside className="ops-sidebar">
        <div className="ops-logo">
          <div className="ops-logo-icon">IT</div>
          <span className="ops-logo-name">Ops Console</span>
        </div>

        <div className="ops-main-nav">
          <SidebarNavItem
            active
            icon={<LayoutDashboard size={15} aria-hidden="true" />}
            label="Dashboard"
            onClick={() => onViewChange("dashboard")}
          />
          <SidebarNavItem
            active={false}
            icon={<Bot size={15} aria-hidden="true" />}
            label="AI Assistant"
            onClick={() => onViewChange("assistant")}
          />
        </div>

        <div className="ops-divider" />

        <p className="ops-section-label">Projects</p>
        <div className="ops-section-list">
          {user?.role === "admin" ? (
            <ProjectItem
              active={activeProjectId === null}
              label="All projects"
              onClick={() => onProjectChange(null)}
            />
          ) : null}
          {projects.map((project) => (
            <ProjectItem
              key={project.id}
              active={activeProjectId === project.id}
              label={project.name}
              onClick={() => onProjectChange(project.id)}
            />
          ))}
        </div>

        <div className="ops-divider" />

        <p className="ops-section-label">Workspace</p>
        <div className="ops-section-list">
          <SidebarRow
            active={filter === "all"}
            icon={<List size={14} aria-hidden="true" />}
            label="All tickets"
            count={tickets.length}
            onClick={() => onFilterChange("all")}
          />
          <SidebarRow
            active={filter === "mine"}
            icon={<Clock3 size={14} aria-hidden="true" />}
            label="My queue"
            count={counts.mine}
            onClick={() => onFilterChange("mine")}
          />
          <SidebarRow
            active={false}
            icon={<TrendingUp size={14} aria-hidden="true" />}
            label="Reports"
            onClick={() => onFilterChange("all")}
          />
        </div>

        <div className="ops-divider" />

        <p className="ops-section-label">Categories</p>
        <div className="ops-section-list">
          <SidebarRow
            active={filter === "Infra"}
            icon={<Server size={14} aria-hidden="true" />}
            label="Infrastructure"
            count={counts.byCategory.Infra ?? 0}
            onClick={() => onFilterChange("Infra")}
          />
          <SidebarRow
            active={filter === "Bug"}
            icon={<Bug size={14} aria-hidden="true" />}
            label="Bugs"
            count={counts.byCategory.Bug ?? 0}
            onClick={() => onFilterChange("Bug")}
          />
          <SidebarRow
            active={filter === "UI"}
            icon={<Activity size={14} aria-hidden="true" />}
            label="UI"
            count={counts.byCategory.UI ?? 0}
            onClick={() => onFilterChange("UI")}
          />
          <SidebarRow
            active={filter === "Hardware"}
            icon={<HardDrive size={14} aria-hidden="true" />}
            label="Hardware"
            count={counts.byCategory.Hardware ?? 0}
            onClick={() => onFilterChange("Hardware")}
          />
          <SidebarRow
            active={filter === "Feature"}
            icon={<FolderKanban size={14} aria-hidden="true" />}
            label="Features"
            count={counts.byCategory.Feature ?? 0}
            onClick={() => onFilterChange("Feature")}
          />
        </div>

        <div className="ops-user-block">
          <strong>{user?.display_name ?? "User"}</strong>
          <small>{user?.email ?? "Not signed in"}</small>
        </div>
      </aside>

      <div className="ops-dashboard-content">
        <header className="ops-board-header">
          <div className="ops-board-title-wrap">
            <span className="ops-title-dot" />
            <h1>{filterTitle(filter)}</h1>
            <span className="ops-ticket-total">{filteredTickets.length} tickets</span>
          </div>
          <label className="ops-search-box">
            <Search size={14} aria-hidden="true" />
            <span className="sr-only">Search tickets</span>
            <input
              value={search}
              onChange={(event) => onSearchChange(event.target.value)}
              placeholder="Search tickets"
            />
          </label>
        </header>

        {loadState === "loading" ? (
          <LoadingState label="Loading tickets" />
        ) : loadState === "error" ? (
          <EmptyState title="Ticket data did not load" action={<button onClick={onRetry}>Retry</button>}>
            {error}
          </EmptyState>
        ) : filteredTickets.length === 0 ? (
          <EmptyState title="No tickets found" action={<button onClick={onRetry}>Reload</button>}>
            Try a different search or project selection.
          </EmptyState>
        ) : (
          <div className="ops-board-grid">
            {columns.map((column) => (
              <section className={cx("ops-column", `ops-column-${column.key}`)} key={column.key}>
                <header className="ops-column-head">
                  <span className={cx("ops-column-dot", `tone-${column.tone}`)} />
                  <span className="ops-column-label">{column.title}</span>
                  <span className="ops-column-count">{grouped[column.key]?.length ?? 0}</span>
                </header>
                <div className="ops-column-body">
                  {(grouped[column.key] ?? []).map((ticket) => (
                    <TicketCard
                      key={ticket.id}
                      ticket={ticket}
                      resolved={column.key === "resolved"}
                      onClick={() => onTicketSelect(ticket.id)}
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function SidebarNavItem({
  active,
  icon,
  label,
  onClick,
}: {
  active: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button className={cx("ops-main-nav-item", active && "is-active")} onClick={onClick}>
      {icon}
      {label}
    </button>
  );
}

function ProjectItem({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button className={cx("ops-project-item", active && "is-active")} onClick={onClick}>
      <span className="ops-project-dot" aria-hidden="true" />
      <span>{label}</span>
    </button>
  );
}

function SidebarRow({
  active,
  icon,
  label,
  count,
  onClick,
}: {
  active: boolean;
  icon: ReactNode;
  label: string;
  count?: number;
  onClick: () => void;
}) {
  return (
    <button className={cx("ops-side-row", active && "is-active")} onClick={onClick}>
      <span className="ops-side-left">
        {icon}
        <span>{label}</span>
      </span>
      {count !== undefined ? <span className="ops-side-count">{count}</span> : null}
    </button>
  );
}

function TicketCard({
  ticket,
  resolved,
  onClick,
}: {
  ticket: Ticket;
  resolved: boolean;
  onClick: () => void;
}) {
  const priorityClass =
    ticket.priority === "high" || ticket.priority === "critical"
      ? "p-high"
      : ticket.priority === "medium"
      ? "p-med"
      : "p-low";

  return (
    <button className={cx("ops-ticket-card", priorityClass)} onClick={onClick}>
      <span className={cx("ops-ticket-title", resolved && "is-resolved")}>{ticket.title || ticket.summary}</span>
      <span className="ops-ticket-footer">
        <span className={cx("ops-priority-badge", `ops-priority-${ticket.priority}`)}>{ticket.priority}</span>
        <span className="ops-category-badge">{ticket.category}</span>
        <span className="ops-ticket-date">{formatTime(ticket.updated_at)}</span>
      </span>
    </button>
  );
}

function filterTickets(tickets: Ticket[], search: string, filter: SideFilter, user: ApiUser | null): Ticket[] {
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
    {
      mine: 0,
      byCategory: {} as Partial<Record<TicketCategory, number>>,
    }
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
  if (filter === "Bug") {
    return "Bugs";
  }
  if (filter === "Feature") {
    return "Features";
  }
  return filter;
}

export type { SideFilter };