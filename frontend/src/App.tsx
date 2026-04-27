import { useCallback, useEffect, useMemo, useReducer } from "react";
import { api } from "./api/client";
import { AppShell, type ViewKey } from "./components/AppShell";
import { AssistantPage } from "./components/AssistantPage";
import { AuthDialog } from "./components/AuthDialog";
import { NewTicketModal } from "./components/NewTicketModal";
import { TicketBoard, type SideFilter } from "./components/TicketBoard";
import { TicketDetail } from "./components/TicketDetail";
import type { ApiUser, LoadState, Ticket } from "./types";

interface AppState {
  activeView: ViewKey;
  user: ApiUser | null;
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
  | { type: "view"; view: ViewKey }
  | { type: "user-ready"; user: ApiUser | null }
  | { type: "tickets-loading" }
  | { type: "tickets-ready"; tickets: Ticket[] }
  | { type: "tickets-error"; error: string }
  | { type: "ticket-select-start"; ticket: Ticket | null }
  | { type: "ticket-select-ready"; ticket: Ticket }
  | { type: "ticket-select-error"; error: string }
  | { type: "ticket-clear" }
  | { type: "ticket-upsert"; ticket: Ticket; select?: boolean }
  | { type: "search"; value: string }
  | { type: "side-filter"; value: SideFilter }
  | { type: "ai-toggle" }
  | { type: "auth-open"; value: boolean }
  | { type: "new-ticket-open"; value: boolean };

const initialState: AppState = {
  activeView: "dashboard",
  user: null,
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
    case "view":
      return {
        ...state,
        activeView: action.view,
        selectedTicket: action.view === "assistant" ? null : state.selectedTicket,
        aiOpen: action.view === "assistant" ? false : state.aiOpen
      };
    case "user-ready":
      return { ...state, user: action.user };
    case "tickets-loading":
      return { ...state, ticketsState: "loading", ticketsError: null };
    case "tickets-ready":
      return { ...state, tickets: action.tickets, ticketsState: "ready", ticketsError: null };
    case "tickets-error":
      return { ...state, ticketsState: "error", ticketsError: action.error };
    case "ticket-select-start":
      return {
        ...state,
        activeView: "dashboard",
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
        activeView: "dashboard",
        tickets: upsertTicket(state.tickets, action.ticket),
        selectedTicket: action.select ? action.ticket : state.selectedTicket,
        selectedState: action.select ? "ready" : state.selectedState,
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

  const loadTickets = useCallback(async () => {
    dispatch({ type: "tickets-loading" });
    try {
      const response = await api.listTickets(100);
      dispatch({ type: "tickets-ready", tickets: response.tickets });
    } catch (caught) {
      dispatch({ type: "tickets-error", error: errorMessage(caught) });
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    api
      .getMe()
      .then((user) => {
        if (!cancelled) {
          dispatch({ type: "user-ready", user });
        }
      })
      .catch(() => {
        if (!cancelled) {
          dispatch({ type: "user-ready", user: null });
        }
      });

    void loadTickets();

    return () => {
      cancelled = true;
    };
  }, [loadTickets]);

  const selectTicket = useCallback(
    async (ticketId: number) => {
      const currentTicket = state.tickets.find((ticket) => ticket.id === ticketId) ?? null;
      dispatch({ type: "ticket-select-start", ticket: currentTicket });
      try {
        const ticket = await api.getTicket(ticketId);
        dispatch({ type: "ticket-select-ready", ticket });
      } catch (caught) {
        dispatch({ type: "ticket-select-error", error: errorMessage(caught) });
      }
    },
    [state.tickets]
  );

  const activeView = state.selectedTicket ? "dashboard" : state.activeView;
  const dashboardContent = useMemo(() => {
    if (state.selectedTicket) {
      return (
        <TicketDetail
          ticket={state.selectedTicket}
          loadState={state.selectedState}
          error={state.selectedError}
          aiOpen={state.aiOpen}
          onBack={() => dispatch({ type: "ticket-clear" })}
          onToggleAi={() => dispatch({ type: "ai-toggle" })}
        />
      );
    }

    return (
      <TicketBoard
        tickets={state.tickets}
        loadState={state.ticketsState}
        error={state.ticketsError}
        search={state.search}
        filter={state.sideFilter}
        user={state.user}
        onSearchChange={(value) => dispatch({ type: "search", value })}
        onFilterChange={(value) => dispatch({ type: "side-filter", value })}
        onTicketSelect={(ticketId) => void selectTicket(ticketId)}
        onNewTicket={() => dispatch({ type: "new-ticket-open", value: true })}
        onRetry={() => void loadTickets()}
      />
    );
  }, [
    loadTickets,
    selectTicket,
    state.aiOpen,
    state.search,
    state.selectedError,
    state.selectedState,
    state.selectedTicket,
    state.sideFilter,
    state.tickets,
    state.ticketsError,
    state.ticketsState,
    state.user
  ]);

  return (
    <>
      <AppShell
        activeView={activeView}
        user={state.user}
        onViewChange={(view) => dispatch({ type: "view", view })}
        onNewTicket={() => dispatch({ type: "new-ticket-open", value: true })}
        onAuth={() => dispatch({ type: "auth-open", value: true })}
        onLogout={() => {
          void api.logout().finally(() => dispatch({ type: "user-ready", user: null }));
        }}
      >
        {state.activeView === "assistant" && !state.selectedTicket ? (
          <AssistantPage
            tickets={state.tickets}
            user={state.user}
            onTicketCreated={(ticket) => dispatch({ type: "ticket-upsert", ticket })}
            onTicketSelect={(ticketId) => void selectTicket(ticketId)}
          />
        ) : (
          dashboardContent
        )}
      </AppShell>
      <AuthDialog
        open={state.authOpen}
        onClose={() => dispatch({ type: "auth-open", value: false })}
        onAuthenticated={(user) => dispatch({ type: "user-ready", user })}
      />
      <NewTicketModal
        open={state.newTicketOpen}
        user={state.user}
        onClose={() => dispatch({ type: "new-ticket-open", value: false })}
        onCreated={(ticket) => dispatch({ type: "ticket-upsert", ticket, select: true })}
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
