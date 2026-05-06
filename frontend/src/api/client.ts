import type {
  ApiUser,
  ChatHistoryResponse,
  ChatResponse,
  ChatThreadsResponse,
  CreateTicketPayload,
  CreateTicketResponse,
  ProjectSummary,
  Ticket,
  TicketComment,
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

export interface ChatStreamHandlers {
  onStart?: (payload: { conversation_id?: string; thread_id?: string }) => void;
  // Thinking stream temporarily disabled.
  // onReasoningToken?: (token: string) => void;
  onMessageToken?: (token: string) => void;
  onFinal?: (payload: ChatResponse) => void;
  onError?: (message: string, status?: number) => void;
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

  const rawBody = await response.text();
  if (!rawBody) {
    return undefined as T;
  }

  try {
    return JSON.parse(rawBody) as T;
  } catch {
    throw new ApiError(nonJsonResponseMessage(rawBody), response.status);
  }
}

async function readError(response: Response): Promise<string> {
  const cloned = response.clone();
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
    const rawBody = await cloned.text();
    if (rawBody) {
      return nonJsonResponseMessage(rawBody);
    }
  }
  return response.statusText || "Request failed.";
}

function nonJsonResponseMessage(rawBody: string): string {
  const normalized = rawBody.trim().toLowerCase();
  if (normalized.startsWith("<!doctype") || normalized.startsWith("<html")) {
    return (
      "Received HTML instead of API JSON. The frontend is likely pointing to the static server "
      + "instead of the backend API. Set VITE_API_BASE_URL to your FastAPI origin (for example "
      + "http://localhost:8000) and rebuild the frontend."
    );
  }

  const compact = rawBody.replace(/\s+/g, " ").trim();
  return compact.slice(0, 220) || "Request failed.";
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

  listTicketComments: (ticketId: number) =>
    request<{ comments: TicketComment[] }>(`/api/tickets/${ticketId}/comments`),

  createTicketComment: (
    ticketId: number,
    payload: { content: string; parent_comment_id?: number | null }
  ) =>
    request<{ comment: TicketComment }>(`/api/tickets/${ticketId}/comments`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),

  updateTicketComment: (ticketId: number, commentId: number, payload: { content: string }) =>
    request<{ comment: TicketComment }>(`/api/tickets/${ticketId}/comments/${commentId}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    }),

  deleteTicketComment: (ticketId: number, commentId: number) =>
    request<void>(`/api/tickets/${ticketId}/comments/${commentId}`, {
      method: "DELETE",
      skipJson: true
    }),

  getTicketInsights: (ticketId: number) =>
    request<TicketInsight>(`/api/tickets/${ticketId}/insights`),

  getChatHistory: (threadId?: string) =>
    request<ChatHistoryResponse>(
      `/api/chat/history${threadId ? `?thread_id=${encodeURIComponent(threadId)}` : ""}`
    ),

  getChatThreads: () => request<ChatThreadsResponse>("/api/chat/threads"),

  deleteChatThread: (threadId: string) =>
    request<void>(`/api/chat/threads/${encodeURIComponent(threadId)}`, {
      method: "DELETE",
      skipJson: true
    }),

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
    }),

  chatStream: async (
    payload: {
      message: string;
      conversation_id?: string;
      thread_id?: string;
      user_id?: string;
      app_name?: string | null;
      environment?: string;
      clearance?: string;
      project_id?: number | null;
    },
    handlers: ChatStreamHandlers,
  ) => {
    const response = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      throw new ApiError(await readError(response), response.status);
    }

    if (!response.body) {
      throw new ApiError("Streaming response body unavailable.", response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });

      let splitIndex = buffer.indexOf("\n\n");
      while (splitIndex !== -1) {
        const rawEvent = buffer.slice(0, splitIndex).trim();
        buffer = buffer.slice(splitIndex + 2);
        if (rawEvent) {
          const parsed = parseSseEvent(rawEvent);
          dispatchChatStreamEvent(parsed.event, parsed.data, handlers);
        }
        splitIndex = buffer.indexOf("\n\n");
      }
    }

    const trailing = buffer.trim();
    if (trailing) {
      const parsed = parseSseEvent(trailing);
      dispatchChatStreamEvent(parsed.event, parsed.data, handlers);
    }
  },
};

function parseSseEvent(rawEvent: string): { event: string; data: unknown } {
  const lines = rawEvent.split(/\r?\n/);
  let event = "message";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  const dataText = dataLines.join("\n");
  if (!dataText) {
    return { event, data: null };
  }
  try {
    return { event, data: JSON.parse(dataText) };
  } catch {
    return { event, data: dataText };
  }
}

function dispatchChatStreamEvent(
  event: string,
  data: unknown,
  handlers: ChatStreamHandlers,
) {
  if (event === "start") {
    handlers.onStart?.(
      (typeof data === "object" && data
        ? (data as { conversation_id?: string; thread_id?: string })
        : {})
    );
    return;
  }

  // Thinking stream temporarily disabled.
  if (event === "reasoning_token") {
    return;
  }

  if (event === "message_token") {
    const token =
      typeof data === "object" && data && "token" in data
        ? String((data as { token?: unknown }).token ?? "")
        : "";
    if (token) {
      handlers.onMessageToken?.(token);
    }
    return;
  }

  if (event === "final") {
    if (typeof data === "object" && data) {
      handlers.onFinal?.(data as ChatResponse);
    }
    return;
  }

  if (event === "error") {
    const detail =
      typeof data === "object" && data && "detail" in data
        ? String((data as { detail?: unknown }).detail ?? "Chat stream failed.")
        : "Chat stream failed.";
    const status =
      typeof data === "object" && data && "status" in data
        ? Number((data as { status?: unknown }).status)
        : undefined;
    handlers.onError?.(detail, Number.isFinite(status) ? status : undefined);
  }
}
