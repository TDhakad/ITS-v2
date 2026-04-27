export type LoadState = "idle" | "loading" | "ready" | "error";

export type TicketStatus = "open" | "triage" | "in_progress" | "resolved" | "closed";
export type Priority = "low" | "medium" | "high" | "critical";
export type TicketCategory = "Bug" | "Feature" | "UI" | "Infra" | "Hardware";
export type Environment = "production" | "staging" | "development" | "unknown";
export type UserClearance = "public" | "internal" | "restricted";

export interface ApiUser {
  id: number;
  email: string;
  display_name: string;
  role: "user" | "agent" | "admin";
  clearance: UserClearance;
  is_active: boolean;
  created_at: string;
  last_login_at?: string | null;
}

export interface ProjectSummary {
  id: number;
  name: string;
  slug: string;
  description?: string;
  owner_id?: number;
}

export interface KBReference {
  kb_id?: string;
  title?: string;
  source?: string;
  score?: number;
  relevance_score?: number;
  clearance?: UserClearance;
  summary?: string;
}

export interface ConversationMessage {
  role: "user" | "assistant" | "system";
  content: string;
  created_at?: string;
}

export interface Ticket {
  id: number;
  ticket_id: number;
  title: string;
  summary: string;
  description: string;
  requester: string;
  created_by: string;
  user_id: string;
  thread_id: string;
  status: TicketStatus;
  state: TicketStatus;
  priority: Priority;
  severity: Priority;
  category: TicketCategory;
  type: TicketCategory;
  keywords: string[];
  app_name?: string | null;
  environment: Environment;
  project_id?: number | null;
  project_name?: string | null;
  created_at: string;
  updated_at: string;
  linked_kb_articles: KBReference[];
  duplicate_ticket_ids: number[];
  conversation?: ConversationMessage[];
  messages?: ConversationMessage[];
  raw_context?: {
    description?: string;
    metadata?: Record<string, unknown>;
    [key: string]: unknown;
  };
}

export interface TicketInsight {
  ticket_id: number;
  summary: string;
  recommended_action: string;
  suggested_priority: string;
  signals: string[];
  citations: KBReference[];
  references: KBReference[];
  duplicates: Array<{ ticket_id?: number; score?: number; summary?: string; [key: string]: unknown }>;
  suggested_fixes: string[];
  retrieved_chunks: number;
  retrieval_available: boolean;
}

export interface ChatResponse {
  conversation_id: string;
  thread_id: string;
  message: string;
  response: string;
  route?: string | null;
  ticket_id?: number | null;
  citations?: KBReference[];
  references?: KBReference[];
  ticket?: Ticket;
}

export interface ChatHistoryMessage {
  id?: string;
  role: "user" | "assistant";
  content: string;
  created_at?: string | null;
}

export interface ChatHistoryResponse {
  conversation_id: string;
  thread_id: string;
  messages: ChatHistoryMessage[];
}

export interface ChatThread {
  thread_id: string;
  last_at: string | null;
  message_count: number;
  preview: string;
}

export interface ChatThreadsResponse {
  threads: ChatThread[];
}

export interface CreateTicketPayload {
  description: string;
  user_id?: string;
  thread_id?: string;
  project_id?: number | null;
  title?: string;
  app_name?: string | null;
  environment?: Environment;
  clearance?: UserClearance;
  category?: TicketCategory;
  priority?: "Low" | "Medium" | "High" | "Critical";
  keywords?: string[];
  metadata?: Record<string, unknown>;
}

export interface CreateTicketResponse {
  ticket: Ticket;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  citations?: KBReference[];
  ticket?: Ticket;
}
