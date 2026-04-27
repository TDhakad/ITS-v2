import { Bot, FileText, Plus, Send, Shield, Ticket as TicketIcon } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { api } from "../api/client";
import { formatTime, generateId, initials } from "../lib";
import type { ApiUser, ChatMessage, LoadState, Ticket } from "../types";
import { Button, EmptyState, LoadingState } from "./common";
import { MarkdownContent } from "./MarkdownContent";

interface AssistantPageProps {
  tickets: Ticket[];
  user: ApiUser | null;
  onTicketCreated: (ticket: Ticket) => void;
  onTicketSelect: (ticketId: number) => void;
}

const suggestions = [
  "Which tickets are at SLA risk?",
  "Summarize open access requests",
  "Draft a reply for the newest infrastructure ticket",
  "Show onboarding gaps"
];

export function AssistantPage({
  tickets,
  user,
  onTicketCreated,
  onTicketSelect
}: AssistantPageProps) {
  const [conversationId, setConversationId] = useState(() => generateId("chat"));
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>(() => [
    {
      id: generateId("msg"),
      role: "assistant",
      content:
        "I have context from the ticket API and knowledge references returned by the assistant endpoint.",
      createdAt: new Date().toISOString()
    }
  ]);
  const [state, setState] = useState<LoadState>("idle");
  const [error, setError] = useState<string | null>(null);

  const recentTickets = useMemo(() => tickets.slice(0, 6), [tickets]);

  async function submitMessage(value: string) {
    const trimmed = value.trim();
    if (!trimmed || state === "loading") {
      return;
    }

    const userMessage: ChatMessage = {
      id: generateId("msg"),
      role: "user",
      content: trimmed,
      createdAt: new Date().toISOString()
    };

    setMessages((current) => [...current, userMessage]);
    setInput("");
    setState("loading");
    setError(null);

    try {
      const response = await api.chat({
        message: trimmed,
        conversation_id: conversationId,
        thread_id: conversationId,
        user_id: user ? String(user.id) : "anonymous",
        environment: "unknown",
        clearance: user?.clearance ?? "public"
      });

      if (response.ticket) {
        onTicketCreated(response.ticket);
      }

      setConversationId(response.thread_id || response.conversation_id || conversationId);
      setMessages((current) => [
        ...current,
        {
          id: generateId("msg"),
          role: "assistant",
          content: response.response || response.message || "No response content returned.",
          createdAt: new Date().toISOString(),
          citations: response.citations ?? response.references,
          ticket: response.ticket
        }
      ]);
      setState("ready");
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Assistant request failed.";
      setError(message);
      setMessages((current) => [
        ...current,
        {
          id: generateId("msg"),
          role: "assistant",
          content: message,
          createdAt: new Date().toISOString()
        }
      ]);
      setState("error");
    }
  }

  return (
    <section className="assistant-page" aria-label="AI assistant">
      <aside className="chat-history">
        <div className="chat-history-top">
          <strong>Conversations</strong>
          <Button
            icon={<Plus size={15} aria-hidden="true" />}
            onClick={() => {
              setConversationId(generateId("chat"));
              setMessages([
                {
                  id: generateId("msg"),
                  role: "assistant",
                  content: "New conversation started.",
                  createdAt: new Date().toISOString()
                }
              ]);
              setError(null);
              setState("idle");
            }}
          >
            New chat
          </Button>
        </div>
        <div className="chat-history-list">
          <p className="side-section">Recent tickets</p>
          {recentTickets.length ? (
            recentTickets.map((ticket) => (
              <button className="history-item" key={ticket.id} onClick={() => onTicketSelect(ticket.id)}>
                <span>{ticket.title}</span>
                <small>#{ticket.id} / {formatTime(ticket.updated_at)}</small>
              </button>
            ))
          ) : (
            <p className="muted-text padded">No tickets loaded.</p>
          )}
        </div>
      </aside>
      <div className="chat-main">
        <header className="chat-topbar">
          <strong>AI Assistant</strong>
          <span className="context-pill">
            <TicketIcon size={14} aria-hidden="true" />
            {tickets.length} tickets in context
          </span>
        </header>
        <div className="message-list" aria-live="polite">
          {messages.map((message) => (
            <article className={`bubble-row ${message.role === "user" ? "mine" : ""}`} key={message.id}>
              <span className={`bubble-avatar ${message.role === "user" ? "mine" : "ai"}`}>
                {message.role === "user" ? initials(user?.display_name, "U") : "AI"}
              </span>
              <div className={`bubble ${message.role === "user" ? "mine" : "ai"}`}>
                <MarkdownContent content={message.content} />
                {message.citations?.length ? (
                  <div className="source-row">
                    {message.citations.slice(0, 4).map((citation) => (
                      <span className="source-chip" key={citation.kb_id ?? citation.title ?? citation.source}>
                        {citation.title ?? citation.source ?? "Reference"}
                      </span>
                    ))}
                  </div>
                ) : null}
                {message.ticket ? (
                  <button className="created-ticket-link" onClick={() => onTicketSelect(message.ticket!.id)}>
                    Created ticket #{message.ticket.id}
                  </button>
                ) : null}
                <time>{formatTime(message.createdAt)}</time>
              </div>
            </article>
          ))}
          {state === "loading" ? <LoadingState label="Assistant is responding" /> : null}
          {error && state === "error" ? <EmptyState title="Assistant error">{error}</EmptyState> : null}
        </div>
        <div className="suggestion-row">
          {suggestions.map((suggestion) => (
            <button key={suggestion} onClick={() => submitMessage(suggestion)}>
              {suggestion}
            </button>
          ))}
        </div>
        <form
          className="chat-composer"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            void submitMessage(input);
          }}
        >
          <label className="composer-box">
            <span className="sr-only">Assistant message</span>
            <textarea
              rows={1}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Ask anything about tickets, runbooks, or access requests"
            />
            <Button
              type="submit"
              variant="primary"
              disabled={!input.trim() || state === "loading"}
              icon={<Send size={15} aria-hidden="true" />}
            >
              Send
            </Button>
          </label>
          <div className="composer-foot">
            <span>
              <FileText size={12} aria-hidden="true" />
              Ticket API
            </span>
            <span>
              <Shield size={12} aria-hidden="true" />
              Session aware
            </span>
            <span>
              <Bot size={12} aria-hidden="true" />
              RAG citations
            </span>
          </div>
        </form>
      </div>
    </section>
  );
}
