import type {
  ApiUser,
  ChatHistoryResponse,
  ChatResponse,
  ChatThreadsResponse,
  CreateTicketPayload,
  CreateTicketResponse,
  ProjectSummary,
  Ticket,
  TicketInsight
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

interface RequestOptions extends RequestInit {
  skipJson?: boolean;
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const hasBody = options.body !== undefined && options.body !== null;

  if (hasBody && !headers.has("Content-Type") && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
    credentials: "include"
  });

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }

  if (options.skipJson || response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown; message?: unknown };
    const detail = payload.detail ?? payload.message;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (typeof item === "object" && item && "msg" in item) {
            return String(item.msg);
          }
          return String(item);
        })
        .join("; ");
    }
  } catch {
    // Fall through to generic status text.
  }
  return response.statusText || "Request failed.";
}

export const api = {
  health: () => request<{ status: string }>("/api/health"),

  getMe: () => request<ApiUser>("/auth/me"),

  login: (email: string, password: string) =>
    request<ApiUser>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password })
    }),

  register: (email: string, displayName: string, password: string) =>
    request<ApiUser>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, display_name: displayName, password })
    }),

  logout: async () => {
    await fetch(`${API_BASE}/auth/logout`, {
      method: "POST",
      credentials: "include"
    });
  },

  listTickets: (limit = 100, projectId?: number | null) =>
    request<{ tickets: Ticket[] }>(
      `/api/tickets?limit=${limit}${projectId !== undefined && projectId !== null ? `&project_id=${projectId}` : ""}`
    ),

  listProjects: () => request<{ projects: ProjectSummary[] }>("/api/projects"),

  getTicket: (ticketId: number) => request<Ticket>(`/api/tickets/${ticketId}`),

  getTicketInsights: (ticketId: number) =>
    request<TicketInsight>(`/api/tickets/${ticketId}/insights`),

  getChatHistory: (threadId?: string) =>
    request<ChatHistoryResponse>(
      `/api/chat/history${threadId ? `?thread_id=${encodeURIComponent(threadId)}` : ""}`
    ),

  getChatThreads: () => request<ChatThreadsResponse>("/api/chat/threads"),

  createTicket: (payload: CreateTicketPayload) =>
    request<CreateTicketResponse>("/api/tickets", {
      method: "POST",
      body: JSON.stringify(payload)
    }),

  chat: (payload: {
    message: string;
    conversation_id?: string;
    thread_id?: string;
    user_id?: string;
    app_name?: string | null;
    environment?: string;
    clearance?: string;
    project_id?: number | null;
  }) =>
    request<ChatResponse>("/api/chat", {
      method: "POST",
      body: JSON.stringify(payload)
    })
};
