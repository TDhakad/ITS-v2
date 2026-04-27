import { useCallback, useEffect, useReducer } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api/client";
import { AppShell, type ViewKey } from "./components/AppShell";
import { AssistantPage } from "./components/AssistantPage";
import { AuthDialog } from "./components/AuthDialog";
import { NewTicketModal } from "./components/NewTicketModal";
import { TicketBoard, type SideFilter } from "./components/TicketBoard";
import { TicketDetail } from "./components/TicketDetail";
import { Button, EmptyState, LoadingState } from "./components/common";
import type { ApiUser, LoadState, ProjectSummary, Ticket } from "./types";

const TICKET_PATH_RE = /^\/tickets\/(\d+)\/?$/i;

interface AppState {
  authResolved: boolean;
  user: ApiUser | null;
  projects: ProjectSummary[];
  activeProjectId: number | null;
  tickets: Ticket[];
  ticketsState: LoadState;
  ticketsError: string | null;
  selectedTicket: Ticket | null;
  selectedState: LoadState;
  selectedError: string | null;
  search: string;
  sideFilter: SideFilter;
  aiOpen: boolean;
  authOpen: boolean;
  newTicketOpen: boolean;
}

type Action =
  | { type: "user-ready"; user: ApiUser | null }
  | { type: "projects-ready"; projects: ProjectSummary[] }
  | { type: "project-select"; projectId: number | null }
  | { type: "tickets-loading" }
  | { type: "tickets-ready"; tickets: Ticket[] }
  | { type: "tickets-error"; error: string }
  | { type: "ticket-select-start"; ticket: Ticket | null }
  | { type: "ticket-select-ready"; ticket: Ticket }
  | { type: "ticket-select-error"; error: string }
  | { type: "ticket-clear" }
  | { type: "ticket-upsert"; ticket: Ticket }
  | { type: "search"; value: string }
  | { type: "side-filter"; value: SideFilter }
  | { type: "ai-toggle" }
  | { type: "auth-open"; value: boolean }
  | { type: "new-ticket-open"; value: boolean };

const initialState: AppState = {
  authResolved: false,
  user: null,
  projects: [],
  activeProjectId: null,
  tickets: [],
  ticketsState: "idle",
  ticketsError: null,
  selectedTicket: null,
  selectedState: "idle",
  selectedError: null,
  search: "",
  sideFilter: "all",
  aiOpen: false,
  authOpen: false,
  newTicketOpen: false
};

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "user-ready":
      return {
        ...state,
        authResolved: true,
        user: action.user,
        projects: action.user ? state.projects : [],
        activeProjectId: action.user ? state.activeProjectId : null,
        tickets: action.user ? state.tickets : [],
        selectedTicket: action.user ? state.selectedTicket : null,
      };
    case "projects-ready": {
      const projectIds = new Set(action.projects.map((project) => project.id));
      const previousSelection = state.activeProjectId;
      let nextSelection: number | null = null;
      if (state.user?.role === "admin") {
        nextSelection = previousSelection !== null && projectIds.has(previousSelection)
          ? previousSelection
          : null;
      } else if (previousSelection !== null && projectIds.has(previousSelection)) {
        nextSelection = previousSelection;
      } else {
        nextSelection = action.projects[0]?.id ?? null;
      }
      return {
        ...state,
        projects: action.projects,
        activeProjectId: nextSelection,
      };
    }
    case "project-select":
      return {
        ...state,
        activeProjectId: action.projectId,
        selectedTicket: null,
        selectedState: "idle",
        selectedError: null,
        aiOpen: false,
      };
    case "tickets-loading":
      return { ...state, ticketsState: "loading", ticketsError: null };
    case "tickets-ready":
      return { ...state, tickets: action.tickets, ticketsState: "ready", ticketsError: null };
    case "tickets-error":
      return { ...state, ticketsState: "error", ticketsError: action.error };
    case "ticket-select-start":
      return {
        ...state,
        selectedTicket: action.ticket,
        selectedState: "loading",
        selectedError: null
      };
    case "ticket-select-ready":
      return {
        ...state,
        selectedTicket: action.ticket,
        selectedState: "ready",
        selectedError: null,
        tickets: upsertTicket(state.tickets, action.ticket)
      };
    case "ticket-select-error":
      return { ...state, selectedState: "error", selectedError: action.error };
    case "ticket-clear":
      return { ...state, selectedTicket: null, selectedState: "idle", selectedError: null, aiOpen: false };
    case "ticket-upsert":
      return {
        ...state,
        tickets: upsertTicket(state.tickets, action.ticket),
        newTicketOpen: false
      };
    case "search":
      return { ...state, search: action.value };
    case "side-filter":
      return { ...state, sideFilter: action.value };
    case "ai-toggle":
      return { ...state, aiOpen: !state.aiOpen };
    case "auth-open":
      return { ...state, authOpen: action.value };
    case "new-ticket-open":
      return { ...state, newTicketOpen: action.value };
    default:
      return state;
  }
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const location = useLocation();
  const navigate = useNavigate();

  const activeView: ViewKey = location.pathname.startsWith("/assistant")
    ? "assistant"
    : "dashboard";
  const routeTicketId = ticketIdFromPath(location.pathname);

  const loadProjects = useCallback(async () => {
    const response = await api.listProjects();
    dispatch({ type: "projects-ready", projects: response.projects });
  }, []);

  const loadTickets = useCallback(async (projectId: number | null) => {
    dispatch({ type: "tickets-loading" });
    try {
      const response = await api.listTickets(100, projectId);
      dispatch({ type: "tickets-ready", tickets: response.tickets });
    } catch (caught) {
      dispatch({ type: "tickets-error", error: errorMessage(caught) });
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function bootstrap() {
      try {
        const user = await api.getMe();
        if (cancelled) {
          return;
        }
        dispatch({ type: "user-ready", user });
        await loadProjects();
      } catch {
        if (!cancelled) {
          dispatch({ type: "user-ready", user: null });
        }
      }
    }

    void bootstrap();

    return () => {
      cancelled = true;
    };
  }, [loadProjects]);

  useEffect(() => {
    if (!state.user) {
      return;
    }
    if (state.user.role !== "admin" && state.activeProjectId === null) {
      return;
    }
    void loadTickets(state.activeProjectId);
  }, [loadTickets, state.activeProjectId, state.user]);

  useEffect(() => {
    if (!state.user || routeTicketId === null) {
      dispatch({ type: "ticket-clear" });
      return;
    }

    let cancelled = false;
    dispatch({ type: "ticket-select-start", ticket: null });

    api
      .getTicket(routeTicketId)
      .then((ticket) => {
        if (!cancelled) {
          dispatch({ type: "ticket-select-ready", ticket });
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          dispatch({ type: "ticket-select-error", error: errorMessage(caught) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [routeTicketId, state.user]);

  const selectTicket = useCallback(
    (ticketId: number) => {
      navigate(ticketPath(ticketId));
    },
    [navigate]
  );

  function dashboardBoard() {
    return (
      <TicketBoard
        tickets={state.tickets}
        projects={state.projects}
        activeProjectId={state.activeProjectId}
        loadState={state.ticketsState}
        error={state.ticketsError}
        search={state.search}
        filter={state.sideFilter}
        user={state.user}
        onProjectChange={(projectId) => {
          dispatch({ type: "project-select", projectId });
          navigate("/");
        }}
        onSearchChange={(value) => dispatch({ type: "search", value })}
        onFilterChange={(value) => dispatch({ type: "side-filter", value })}
        onTicketSelect={selectTicket}
        onRetry={() => void loadTickets(state.activeProjectId)}
        onViewChange={(view) => navigate(view === "assistant" ? "/assistant" : "/")}
      />
    );
  }

  function ticketDetailView() {
    if (state.selectedState === "loading" && !state.selectedTicket) {
      return <LoadingState label="Loading ticket" />;
    }
    if (state.selectedState === "error" || !state.selectedTicket) {
      return (
        <EmptyState title="Ticket did not load" action={<button onClick={() => navigate("/")}>Back to dashboard</button>}>
          {state.selectedError ?? "The requested ticket could not be loaded."}
        </EmptyState>
      );
    }
    return (
      <TicketDetail
        ticket={state.selectedTicket}
        loadState={state.selectedState}
        error={state.selectedError}
        aiOpen={state.aiOpen}
        onBack={() => navigate("/")}
        onToggleAi={() => dispatch({ type: "ai-toggle" })}
        onTicketSelect={selectTicket}
      />
    );
  }

  if (!state.authResolved) {
    return <LoadingState label="Checking session" />;
  }

  if (!state.user) {
    return (
      <>
        <section className="dashboard-page" aria-label="Authentication required">
          <div className="dashboard-content">
            <EmptyState
              title="Sign in required"
              action={
                <Button variant="primary" onClick={() => dispatch({ type: "auth-open", value: true })}>
                  Sign in
                </Button>
              }
            >
              Please sign in to access project dashboards and tickets.
            </EmptyState>
          </div>
        </section>
        <AuthDialog
          open={state.authOpen}
          onClose={() => dispatch({ type: "auth-open", value: false })}
          onAuthenticated={(user) => {
            dispatch({ type: "user-ready", user });
            void loadProjects();
          }}
        />
      </>
    );
  }

  return (
    <>
      <AppShell
        activeView={activeView}
        user={state.user}
        onViewChange={(view) => navigate(view === "assistant" ? "/assistant" : "/")}
        onNewTicket={() => dispatch({ type: "new-ticket-open", value: true })}
        onAuth={() => dispatch({ type: "auth-open", value: true })}
        onLogout={() => {
          void api.logout().finally(() => dispatch({ type: "user-ready", user: null }));
        }}
      >
        <Routes>
          <Route path="/" element={dashboardBoard()} />
          <Route
            path="/assistant"
            element={
              <AssistantPage
                tickets={state.tickets}
                user={state.user}
                projectId={state.activeProjectId}
                onTicketCreated={(ticket) => dispatch({ type: "ticket-upsert", ticket })}
                onTicketSelect={selectTicket}
              />
            }
          />
          <Route path="/tickets/:ticketId" element={ticketDetailView()} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AppShell>
      <AuthDialog
        open={state.authOpen}
        onClose={() => dispatch({ type: "auth-open", value: false })}
        onAuthenticated={(user) => {
          dispatch({ type: "user-ready", user });
          void loadProjects();
        }}
      />
      <NewTicketModal
        open={state.newTicketOpen}
        user={state.user}
        projectId={state.activeProjectId}
        onClose={() => dispatch({ type: "new-ticket-open", value: false })}
        onCreated={(ticket) => {
          dispatch({ type: "ticket-upsert", ticket });
          navigate(ticketPath(ticket.id));
        }}
      />
    </>
  );
}

function upsertTicket(tickets: Ticket[], incoming: Ticket): Ticket[] {
  const exists = tickets.some((ticket) => ticket.id === incoming.id);
  if (!exists) {
    return [incoming, ...tickets];
  }
  return tickets.map((ticket) => (ticket.id === incoming.id ? incoming : ticket));
}

function errorMessage(caught: unknown): string {
  return caught instanceof Error ? caught.message : "Request failed.";
}

function ticketIdFromPath(pathname: string): number | null {
  const match = pathname.match(TICKET_PATH_RE);
  if (!match) {
    return null;
  }
  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function ticketPath(ticketId: number): string {
  return `/tickets/${ticketId}`;
}
